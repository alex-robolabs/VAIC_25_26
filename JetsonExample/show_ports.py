#!/usr/bin/env python3
"""List all detected serial ports with their USB descriptions.

Used for diagnosing which /dev/ttyACM* endpoint corresponds to what
device. Run standalone on the Jetson:

    python3 ~/VAIC_25_26/JetsonExample/show_ports.py

Expected output with both V5 Brain (running a user program) and GPS
Sensor connected:

    /dev/ttyACM0     | GPS Sensor - Vex Robotics Communications Port
    /dev/ttyACM1     | GPS Sensor - Vex Robotics User Port
    /dev/ttyACM2     | VEX Robotics V5 Brain - <serial> - VEX Robotics Communications Port
    /dev/ttyACM3     | VEX Robotics V5 Brain - <serial> - VEX Robotics User Port

IMPORTANT: the V5 Brain's User Port endpoint only enumerates when a
user program is *running* on the Brain. If you see only GPS endpoints
(or only a V5 Communications Port without a User Port), the V5 is
either powered off or idle — start the ai_demo program and re-run.
"""

from serial.tools.list_ports import comports


def main():
    ports = sorted(comports(), key=lambda p: p.device)
    if not ports:
        print("(no serial ports detected)")
        return
    for p in ports:
        print("{:<16} | {}".format(p.device, p.description))


if __name__ == "__main__":
    main()
