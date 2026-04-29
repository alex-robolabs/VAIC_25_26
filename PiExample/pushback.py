"""Pi-side VEX AI main loop.

Adapted from the upstream JetsonExample/pushback.py. Differences from
the Nano version:

  - Comms layer is the redesigned V5SerialComms / V5GPS (state-machine
    health, watchdog folded into reader, context-managed lifecycle).
  - V5 + GPS links are managed in an ExitStack so SIGTERM (sent by
    `systemctl stop vexai`) propagates into clean stop() calls.
  - Health log line includes the link state, not just the boolean.

Inference, RealSense, and dashboard plumbing are unchanged from
upstream — those modules (V5Web, V5MapPosition, model, etc.) are
sourced from JetsonExample/ via PYTHONPATH set by Scripts/run.sh.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import time
from glob import glob

import cv2
import numpy as np
import pyrealsense2 as rs

import V5Comm
from V5Comm import V5SerialComms
from V5MapPosition import MapPosition
from V5Position import Position, V5GPS
from V5Web import Statistics, V5WebData
from model import Model, rawDetection
from vexai_logging import configure_logging

log = logging.getLogger("vexai.pushback")


class Camera:
    def __init__(self):
        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
        self.config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)

    def start(self):
        self.profile = self.pipeline.start(self.config)
        depth_sensor = self.profile.get_device().first_depth_sensor()
        self.depth_scale = depth_sensor.get_depth_scale()
        self.profile.get_device().query_sensors()[1].set_option(
            rs.option.auto_exposure_priority, 0.0)

    def get_frames(self):
        return self.pipeline.wait_for_frames()

    def stop(self):
        self.pipeline.stop()


class Processing:
    def __init__(self, depth_scale, profile):
        self.depth_scale = depth_scale
        self.align_to = rs.stream.color
        self.align = rs.align(self.align_to)
        self.model = Model()
        self.HUE = 0
        self.SATURATION = 0
        self.VALUE = 0
        self.depth_intrin = profile.get_stream(rs.stream.depth) \
            .as_video_stream_profile().get_intrinsics()
        self.color_intrin = profile.get_stream(rs.stream.color) \
            .as_video_stream_profile().get_intrinsics()
        self.depth_to_color_extrin = profile.get_stream(rs.stream.depth) \
            .as_video_stream_profile().get_extrinsics_to(
                profile.get_stream(rs.stream.color))
        self.color_to_depth_extrin = profile.get_stream(rs.stream.color) \
            .as_video_stream_profile().get_extrinsics_to(
                profile.get_stream(rs.stream.depth))

    def process_image(self, image):
        if self.HUE == 0 and self.SATURATION == 0 and self.VALUE == 0:
            return image
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        hsv[..., 0] = hsv[..., 0] + self.HUE
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * self.SATURATION, 0, 255)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] * self.VALUE, 0, 255)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

    def updateHSV(self, newHSV):
        self.HUE = newHSV.h
        if self.SATURATION >= 0:
            self.SATURATION = 1 + (newHSV.s) / 100
        else:
            self.SATURATION = (100 - abs(newHSV.s)) / 100
        if self.VALUE >= 0:
            self.VALUE = 1 + (newHSV.v) / 100
        else:
            self.VALUE = (100 - abs(newHSV.v)) / 100

    def project_color_to_depth(self, depth_data, pixel):
        row, col = pixel
        depth_pixel = tuple(map(int, rs.rs2_project_color_pixel_to_depth_pixel(
            depth_data, self.depth_scale, 0.05, 3,
            self.depth_intrin, self.color_intrin,
            self.color_to_depth_extrin, self.depth_to_color_extrin,
            [row, col])))
        return depth_pixel

    def get_depth(self, detection: rawDetection, depth_img):
        height = detection.Height
        width = detection.Width
        low_limit_y, high_limit_y = 45, 55
        low_limit_x, high_limit_x = 45, 55
        top = int(detection.y) + height * low_limit_y // 100
        bottom = int(detection.y) + height * high_limit_y // 100
        left = int(detection.x) + width * low_limit_x // 100
        right = int(detection.x) + width * high_limit_x // 100
        top_left = self.project_color_to_depth(
            self.depth_frame_aligned.get_data(), (top, left))
        bottom_right = self.project_color_to_depth(
            self.depth_frame_aligned.get_data(), (bottom, right))
        r1, c1 = top_left
        r2, c2 = bottom_right
        depth_img = depth_img[r1:r2, c1:c2]
        depth_img = depth_img * self.depth_scale
        depth_img = depth_img[depth_img != 0]
        meanDepth = np.nanmean(depth_img)
        return meanDepth

    def align_frames(self, frames):
        self.depth_frame_aligned = frames.get_depth_frame()
        self.color_frame_aligned = frames.get_color_frame()
        if not self.depth_frame_aligned or not self.color_frame_aligned:
            self.depth_frame_aligned = None
            self.color_frame_aligned = None

    def process_frames(self, frames):
        self.align_frames(frames)
        depth_image = np.asanyarray(self.depth_frame_aligned.get_data())
        color_image = np.asanyarray(self.color_frame_aligned.get_data())
        color_image = self.process_image(color_image)
        depthImage = cv2.normalize(
            depth_image, None, alpha=0.01, beta=255,
            norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        depth_map = cv2.applyColorMap(depthImage, cv2.COLORMAP_JET)
        return depth_image, color_image, depth_map

    def detect_objects(self, color_image):
        output, detections = self.model.inference(color_image)
        return output, detections

    def compute_detections(self, app, detections, depth_image):
        aiRecord = V5Comm.AIRecord(app.get_v5Pos(), [])
        for detection in detections:
            depth = self.get_depth(detection, depth_image)
            imageDet = V5Comm.ImageDetection(
                int(detection.x),
                int(detection.y),
                int(detection.Width),
                int(detection.Height),
            )
            mapPos = app.v5Map.computeMapLocation(
                detection, depth, aiRecord.position)
            mapDet = V5Comm.MapDetection(mapPos[0], mapPos[1], mapPos[2])
            detect = V5Comm.Detection(
                int(detection.ClassID),
                float(detection.Prob),
                float(depth),
                imageDet,
                mapDet,
            )
            aiRecord.detections.append(detect)
        return aiRecord


class Rendering:
    def __init__(self, web_data):
        self.web_data = web_data
        self.cpu_temp_path = "/sys/class/thermal/thermal_zone0/temp"
        for thermal_zone in glob("/sys/class/thermal/thermal_zone*"):
            zone_type_path = thermal_zone + "/type"
            try:
                with open(zone_type_path, "r") as f:
                    zone_type = f.read().rstrip("\n")
            except OSError:
                continue
            if "cpu" in zone_type.lower():
                self.cpu_temp_path = thermal_zone + "/temp"
                break

    def set_images(self, output, depth_image):
        self.web_data.setColorImage(output)
        self.web_data.setDepthImage(depth_image)

    def set_detection_data(self, aiRecord):
        self.web_data.setDetectionData(aiRecord)

    def set_stats(self, stats, v5Pos, start_time, invoke_time, run_time):
        stats.fps = 1.0 / (time.time() - start_time)
        stats.gpsConnected = v5Pos.isConnected()
        stats.invokeTime = invoke_time
        stats.runTime = time.time() - run_time
        try:
            with open(self.cpu_temp_path, "r") as f:
                temp_str = f.readline().rstrip("\n")
            stats.cpuTemp = float(temp_str) / 1000
        except (OSError, ValueError):
            stats.cpuTemp = 0.0
        self.web_data.setStatistics(stats)


class _StopRequested(Exception):
    """Raised by the SIGTERM handler to unwind the ExitStack cleanly."""


def _install_signal_handlers():
    def handle(signum, frame):
        log.info("received signal %d; shutting down", signum)
        raise _StopRequested()
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


class MainApp:
    def __init__(self, v5: V5SerialComms, v5Pos: V5GPS):
        log.info("initializing camera + model")
        self.camera = Camera()
        self.camera.start()
        self.processing = Processing(self.camera.depth_scale, self.camera.profile)

        self.v5 = v5
        self.v5Map = MapPosition()
        self.v5Pos = v5Pos
        self.v5Web = V5WebData(self.v5Map, self.v5Pos, self.processing)
        self.stats = Statistics(0, 0, 0, 640, 480, 0, False)
        self.rendering = Rendering(self.v5Web)
        time.sleep(1)
        log.info("initialized")

    def get_v5Pos(self) -> Position:
        if self.v5Pos is None:
            return Position(0, 0, 0, 0, 0, 0, 0, 0)
        return self.v5Pos.getPosition()

    def set_v5(self, aiRecord) -> None:
        if self.v5 is not None:
            self.v5.setDetectionData(aiRecord)

    def run(self) -> None:
        self.v5Web.start()
        run_time = time.time()
        log.info("entering main loop")
        last_health_log = 0.0
        try:
            while True:
                start_time = time.time()
                frames = self.camera.get_frames()
                depth_image, color_image, depth_map = \
                    self.processing.process_frames(frames)
                invoke_time = time.time()
                output, detections = self.processing.detect_objects(color_image)
                invoke_time = time.time() - invoke_time
                aiRecord = self.processing.compute_detections(
                    self, detections, depth_image)
                self.set_v5(aiRecord)
                self.rendering.set_images(output, depth_map)
                self.rendering.set_detection_data(aiRecord)
                self.rendering.set_stats(
                    self.stats, self.v5Pos, start_time, invoke_time, run_time)

                now = time.time()
                if now - last_health_log > 30:
                    last_health_log = now
                    log.info(
                        "health: data=%s gps=%s data_state=%s gps_state=%s fps=%.1f",
                        self.v5.is_healthy(),
                        self.v5Pos.is_healthy(),
                        self.v5.state().value,
                        self.v5Pos.state().value,
                        self.stats.fps,
                    )
        finally:
            self.camera.stop()


def main() -> int:
    configure_logging()
    _install_signal_handlers()
    try:
        with contextlib.ExitStack() as stack:
            v5 = stack.enter_context(V5SerialComms())
            v5Pos = stack.enter_context(V5GPS())
            app = MainApp(v5, v5Pos)
            app.run()
    except _StopRequested:
        log.info("clean shutdown complete")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
