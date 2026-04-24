# Import necessary libraries
import pyrealsense2 as rs
import numpy as np
import cv2
import time
import os
import logging
from glob import glob

log = logging.getLogger("vexai.pushback")

from V5MapPosition import MapPosition

import V5Comm
from V5Comm import V5SerialComms
from V5Position import Position
from V5Position import V5GPS
from V5Web import V5WebData
from V5Web import Statistics

from model import Model, rawDetection


class Camera:
    # Class handles Camera object instantiation and data requests.
    def __init__(self):
        self.pipeline = rs.pipeline()  # Initialize RealSense pipeline
        self.config = rs.config()
        # Enable depth stream at 640x480 in z16 encoding at 30fps
        self.config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
        # Enable color stream at 640x480 in rgb8 encoding at 30fps
        self.config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)

    def start(self):
        self.profile = self.pipeline.start(self.config)  # Start the pipeline
        # Obtain depth sensor and calculate depth scale
        depth_sensor = self.profile.get_device().first_depth_sensor()
        self.depth_scale = depth_sensor.get_depth_scale()
        self.profile.get_device().query_sensors()[1].set_option(rs.option.auto_exposure_priority, 0.0)

    def get_frames(self):
        return self.pipeline.wait_for_frames()  # Wait and fetch frames from the pipeline

    def stop(self):
        self.pipeline.stop()  # Stop the pipeline when finished


class Processing:
    # Class to handle camera data processing, preparing for inference, and running inference on camera image.
    def __init__(self, depth_scale, profile):
        self.depth_scale = depth_scale
        self.align_to = rs.stream.color
        self.align = rs.align(self.align_to)  # Align depth frames to color stream
        self.model = Model()  # Initialize the object detection model
        self.HUE = 0
        self.SATURATION = 0
        self.VALUE = 0
        self.depth_intrin = profile.get_stream(rs.stream.depth).as_video_stream_profile().get_intrinsics()
        self.color_intrin = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        self.depth_to_color_extrin =  profile.get_stream(rs.stream.depth).as_video_stream_profile().get_extrinsics_to( profile.get_stream(rs.stream.color))
        self.color_to_depth_extrin =  profile.get_stream(rs.stream.color).as_video_stream_profile().get_extrinsics_to( profile.get_stream(rs.stream.depth))

    def process_image(self, image):
        # Enhances the image by shifting the hue and adjusting saturation and brightness.

        if (self.HUE == 0 and self.SATURATION == 0 and self.VALUE == 0):
            return image

        # Convert the image to HSV color space
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)

        # Modify the hue, saturation, and value channels
        hsv[..., 0] = hsv[..., 0] + self.HUE
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * self.SATURATION, 0, 255)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] * self.VALUE, 0, 255)

        # Convert the image back to RGB color space for inferencing
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

    def updateHSV(self, newHSV):
        self.HUE = newHSV.h

        if (self.SATURATION >= 0):
            self.SATURATION = 1 + (newHSV.s) / 100
        else:
            self.SATURATION = (100 - abs(newHSV.s)) / 100

        if (self.VALUE >= 0):
            self.VALUE = 1 + (newHSV.v) / 100
        else:
            self.VALUE = (100 - abs(newHSV.v)) / 100

    def project_color_to_depth(self, depth_data, pixel):
        row, col = pixel
        depth_pixel = tuple(map(int, rs.rs2_project_color_pixel_to_depth_pixel(depth_data, 
                            self.depth_scale, 0.05, 3, self.depth_intrin, self.color_intrin, self.color_to_depth_extrin, 
                            self.depth_to_color_extrin, [row, col])))
        return depth_pixel

    def get_depth(self, detection: rawDetection, depth_img):
        # Compute the bounding box indices for the detection
        height = detection.Height
        width = detection.Width

        low_limit_y = 45
        high_limit_y = 55
        low_limit_x = 45
        high_limit_x = 55
        # Calculate the indices of 10% of the detection.
        top = int(detection.y) + height * low_limit_y // 100
        bottom = int(detection.y) + height * high_limit_y // 100
        left = int(detection.x) + width * low_limit_x // 100
        right = int(detection.x) + width * high_limit_x // 100

        # Extract depth values and scale them
        top_left = self.project_color_to_depth(self.depth_frame_aligned.get_data(), (top, left))
        bottom_right = self.project_color_to_depth(self.depth_frame_aligned.get_data(), (bottom, right))

        r1, c1 = top_left
        r2, c2 = bottom_right
        depth_img = depth_img[r1:r2, c1:c2]
        depth_img = depth_img * self.depth_scale
        # Filter non-zero depth values
        depth_img = depth_img[depth_img != 0]
        # Compute and return mean depth value
        meanDepth = np.nanmean(depth_img)
        return meanDepth

    def align_frames(self, frames):
        # Align depth frames to color frames
        #aligned_frames = self.align.process(frames)
        # Get the aligned frames and validate them
        self.depth_frame_aligned = frames.get_depth_frame()
        self.color_frame_aligned = frames.get_color_frame()

        if not self.depth_frame_aligned or not self.color_frame_aligned:
            self.depth_frame_aligned = None
            self.color_frame_aligned = None

    def process_frames(self, frames):
        # Align frames and extract color and depth images
        # Apply a color map to the depth image
        self.align_frames(frames)
        depth_image = np.asanyarray(self.depth_frame_aligned.get_data())
        color_image = np.asanyarray(self.color_frame_aligned.get_data())
        # apply color correction to image
        color_image = self.process_image(color_image)
        depthImage = cv2.normalize(depth_image, None, alpha=0.01, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        depth_map = cv2.applyColorMap(depthImage, cv2.COLORMAP_JET)

        return depth_image, color_image, depth_map

    def detect_objects(self, color_image):
        # Perform object detection and return results using the Model class in model.py
        output, detections = self.model.inference(color_image)
        return output, detections

    def compute_detections(self, v5, detections, depth_image):
        # Create AIRecord and compute detections with depth and image data.
        # Each AIRecord contains the ClassID, Probablity, and depth information for each detection
        # In addition to the detection's camera image and map position information.
        aiRecord = V5Comm.AIRecord(v5.get_v5Pos(), [])
        for detection in detections:
            depth = self.get_depth(detection, depth_image)
            imageDet = V5Comm.ImageDetection(
                int(detection.x),
                int(detection.y),
                int(detection.Width),
                int(detection.Height),
            )
            mapPos = v5.v5Map.computeMapLocation(detection, depth, aiRecord.position)
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
    # Class to handle rendering camera data and process stat data to the webserver.
    def __init__(self, web_data):
        self.web_data = web_data
        self.cpu_temp_path = "/sys/devices/virtual/thermal/thermal_zone0/temp"

        # loop through available thermal zones to see if any are labeled CPU
        for thermal_zone in glob("/sys/devices/virtual/thermal/thermal_zone*"):
            zone_type = os.popen(f"cat {thermal_zone}/type").read().rstrip("\n")
            # if labeled CPU, use that zone instead
            if "cpu" in zone_type.lower():
                self.cpu_temp_path = thermal_zone + "/temp"

    def set_images(self, output, depth_image):
        # Update web data with color and depth images
        self.web_data.setColorImage(output)
        self.web_data.setDepthImage(depth_image)

    def set_detection_data(self, aiRecord):
        # Update web data with detection information
        self.web_data.setDetectionData(aiRecord)
    
    def set_stats(self, stats, v5Pos, start_time, invoke_time, run_time):
        # Set the statistics for FPS, invoke time, run time, and CPU temp
        stats.fps = 1.0 / (time.time() - start_time)
        stats.gpsConnected = v5Pos.isConnected()
        stats.invokeTime = invoke_time
        stats.runTime = time.time() - run_time
        with open(self.cpu_temp_path, "r") as f:
            temp_str = f.readline().rstrip("\n")
        temp = float(temp_str) / 1000
        stats.cpuTemp = temp
        self.web_data.setStatistics(stats)

    def display_output(self, output):
        # Display the output image in a window
        # Handle window closing with 'q' or 'esc' keys
        cv2.namedWindow("VEX HighStakes", cv2.WINDOW_AUTOSIZE)
        cv2.imshow("VEX HighStakes", output)
        key = cv2.waitKey(1)
        if key & 0xFF == ord("q") or key == 27:
            cv2.destroyAllWindows()


class MainApp:
    def __init__(self):
        # Initialize various components including camera, processing, and rendering
        print("Starting Initialization...")
        self.camera = Camera()
        self.camera.start()
        self.processing = Processing(self.camera.depth_scale, self.camera.profile)

        self.v5 = V5SerialComms()
        self.v5Map = MapPosition()
        self.v5Pos = V5GPS()
        self.v5Web = V5WebData(self.v5Map, self.v5Pos, self.processing)
        self.stats = Statistics(0, 0, 0, 640, 480, 0, False)
        self.rendering = Rendering(self.v5Web)

        time.sleep(1)
        print("Initialized")

    def get_v5Pos(self):
        # Return V5Position object if GPS is connected but default values if not connected
        if self.v5Pos is None:
            return Position(0, 0, 0, 0, 0, 0, 0, 0)
        return self.v5Pos.getPosition()

    def set_v5(self, aiRecord):
        # Set detection data to the Brain if it is connected but does not set any data if None
        if self.v5 is not None:
            self.v5.setDetectionData(aiRecord)

    def run(self):
        # Start main loop: capture frames, process, detect objects, compute detections, render and display
        self.v5.start()
        self.v5Pos.start()
        self.v5Web.start()
        run_time = time.time()
        print("\nStarting Loop")
        last_health_log = 0.0
        try:
            while True:
                start_time = time.time()  # start time of the loop
                frames = self.camera.get_frames()
                depth_image, color_image, depth_map = self.processing.process_frames(frames)
                invoke_time = time.time()
                output, detections = self.processing.detect_objects(color_image)
                invoke_time = time.time() - invoke_time
                aiRecord = self.processing.compute_detections(self, detections, depth_image)
                self.set_v5(aiRecord)
                self.rendering.set_images(output, depth_map)
                self.rendering.set_detection_data(aiRecord)
                self.rendering.set_stats(self.stats, self.v5Pos, start_time, invoke_time, run_time)
                # self.rendering.display_output(output)
                # Periodic health log for post-mortem visibility in journald.
                now = time.time()
                if now - last_health_log > 30:
                    last_health_log = now
                    log.info("health: data=%s gps=%s fps=%.1f",
                             self.v5.is_healthy(),
                             self.v5Pos.is_healthy(),
                             self.stats.fps)
        finally:
            self.camera.stop()


if __name__ == "__main__":
    from vexai_logging import configure_logging
    configure_logging()
    app = MainApp()  # Create the main application
    app.run()  # Run the application
