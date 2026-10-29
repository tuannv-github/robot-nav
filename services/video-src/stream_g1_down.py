#!/usr/bin/env python3
"""Publish the RealSense D435i color camera to MediaMTX path /stream/g1/down.

Selects USB 8086:0b3a (Bus 002 Device 004 when that address is present).
The color node is the YUYV capture interface, not the depth or infrared nodes.
Viewers use the same path:

    rtsp://10.1.101.173:8554/stream/g1/down
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from stream_g1_front import DEFAULT_HOST, DEFAULT_WEBRTC_PORT, Publisher, whip_url

USB_VENDOR = "8086"
USB_PRODUCT = "0b3a"
USB_BUS = 2
USB_DEVNUM = 4
DEFAULT_PATH = "/stream/g1/down"
WIDTH = 1280
HEIGHT = 720
FPS = 30
RECONNECT_DELAY_S = 2
YUYV = b"YUYV"


def _sysfs_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _realsense_usb_devices() -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    root = "/sys/bus/usb/devices"
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        device = os.path.join(root, name)
        if not os.path.isdir(device):
            continue
        vendor = _sysfs_text(os.path.join(device, "idVendor")).lower()
        product = _sysfs_text(os.path.join(device, "idProduct")).lower()
        if vendor != USB_VENDOR or product != USB_PRODUCT:
            continue
        bus = _sysfs_text(os.path.join(device, "busnum"))
        devnum = _sysfs_text(os.path.join(device, "devnum"))
        if not bus.isdigit() or not devnum.isdigit():
            continue
        found.append((device, int(bus), int(devnum)))
    return found


class _FmtDesc(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


def _is_color_node(device: str) -> bool:
    request = (3 << 30) | (ctypes.sizeof(_FmtDesc) << 16) | (ord("V") << 8) | 2
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return False
    try:
        desc = _FmtDesc()
        desc.type = 1
        while True:
            try:
                fcntl.ioctl(fd, request, desc)
            except OSError:
                return False
            if desc.pixelformat.to_bytes(4, "little") == YUYV:
                return True
            desc.index += 1
    finally:
        os.close(fd)


def _color_node(usb_device: str) -> str | None:
    for dirpath, dirnames, _filenames in os.walk(usb_device):
        for dirname in sorted(dirnames):
            if not dirname.startswith("video"):
                continue
            device = f"/dev/{dirname}"
            if os.path.exists(device) and _is_color_node(device):
                return device
    return None


def find_realsense() -> tuple[str, int, int]:
    matches = _realsense_usb_devices()
    if not matches:
        raise RuntimeError(
            f"RealSense D435i {USB_VENDOR}:{USB_PRODUCT} is not connected"
        )
    chosen = next(
        (item for item in matches if item[1] == USB_BUS and item[2] == USB_DEVNUM),
        matches[0],
    )
    usb_path, bus, devnum = chosen
    device = _color_node(usb_path)
    if device is None:
        raise RuntimeError(
            f"RealSense at bus {bus} device {devnum} has no color capture node"
        )
    return device, bus, devnum


def build_pipeline(device: str) -> str:
    return " ".join(
        [
            f"v4l2src device={device} io-mode=2 do-timestamp=true",
            f"! video/x-raw,format=YUY2,width={WIDTH},height={HEIGHT},framerate={FPS}/1",
            "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream",
            "! textoverlay name=stamp halignment=left valignment=top",
            "shaded-background=true font-desc=\"Sans, 28\"",
            "! nvvidconv",
            "! video/x-raw(memory:NVMM),format=NV12",
            "! nvv4l2h264enc bitrate=2000000 control-rate=1 preset-level=1",
            "maxperf-enable=true iframeinterval=15 idrinterval=15",
            "insert-sps-pps=true num-B-Frames=0 vbv-size=200000",
            "! h264parse config-interval=-1",
            "! rtph264pay name=pay pt=96 config-interval=-1 aggregate-mode=zero-latency",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream the RealSense D435i color camera to MediaMTX /stream/g1/down.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="MediaMTX host")
    parser.add_argument("--webrtc-port", type=int, default=DEFAULT_WEBRTC_PORT)
    parser.add_argument("--path", default=DEFAULT_PATH, help="MediaMTX stream path")
    parser.add_argument(
        "--device",
        default="",
        help="V4L color device. Default: YUYV node for 8086:0b3a",
    )
    return parser.parse_args()


def main() -> int:
    Gst.init(None)
    args = parse_args()
    if args.device:
        device, bus, devnum = args.device, USB_BUS, USB_DEVNUM
    else:
        device, bus, devnum = find_realsense()
    location = whip_url(args.host, args.webrtc_port, args.path)
    print(
        f"RealSense bus {bus} device {devnum} ({USB_VENDOR}:{USB_PRODUCT}) {device} "
        f"{WIDTH}x{HEIGHT}@{FPS}"
    )
    print(f"publish {location}")
    print(f"watch rtsp://{args.host}:8554/{args.path.strip('/')}")

    publisher = Publisher(build_pipeline(device), location, device, tune=None)

    def _stop(_signum: int, _frame: object) -> None:
        publisher.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not publisher.stop_requested:
        if not publisher.run_once():
            break
        print(f"reconnecting in {RECONNECT_DELAY_S:.0f}s", file=sys.stderr)
        time.sleep(RECONNECT_DELAY_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
