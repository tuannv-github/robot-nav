#!/usr/bin/env python3
"""Publish the Logitech BRIO to MediaMTX path /stream/g1/front.

Selects USB 046d:085e (Bus 002 Device 007 when that address is present)
and publishes H.264 with WebRTC (WHIP). That skips the RTMP buffer.
Viewers use the same path:

    rtsp://10.1.101.173:8554/stream/g1/front
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import signal
import sys
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC

USB_VENDOR = "046d"
USB_PRODUCT = "085e"
USB_BUS = 2
USB_DEVNUM = 7
DEFAULT_HOST = "10.1.101.173"
DEFAULT_PATH = "/stream/g1/front"
DEFAULT_WEBRTC_PORT = 8889
WIDTH = 1280
HEIGHT = 720
FPS = 30
RECONNECT_DELAY_S = 2
# BRIO focus: 0 is far, 255 is near. A sweep of this hallway peaks at 0.
# The 31 ms auto shutter smears the picture while the robot moves, so the
# shutter is held at 10 ms and gain makes up the brightness.
FOCUS_AUTO = 0x009A090C
FOCUS_ABSOLUTE = 0x009A090A
FOCUS_LOCKED = 0
EXPOSURE_AUTO = 0x009A0901
EXPOSURE_ABSOLUTE = 0x009A0902
EXPOSURE_DYNAMIC = 0x009A0903
GAIN = 0x00980913
EXPOSURE_MANUAL = 1
EXPOSURE_LOCKED = 100
GAIN_LOCKED = 32


def _sysfs_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _brio_usb_devices() -> list[tuple[str, int, int]]:
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


def _capture_node(usb_device: str) -> str | None:
    for dirpath, dirnames, _filenames in os.walk(usb_device):
        for dirname in dirnames:
            if not dirname.startswith("video"):
                continue
            node = os.path.join(dirpath, dirname)
            if _sysfs_text(os.path.join(node, "index")) != "0":
                continue
            device = f"/dev/{dirname}"
            if os.path.exists(device):
                return device
    return None


def find_brio() -> tuple[str, int, int]:
    matches = _brio_usb_devices()
    if not matches:
        raise RuntimeError(
            f"Logitech BRIO {USB_VENDOR}:{USB_PRODUCT} is not connected"
        )
    chosen = next(
        (item for item in matches if item[1] == USB_BUS and item[2] == USB_DEVNUM),
        matches[0],
    )
    usb_path, bus, devnum = chosen
    device = _capture_node(usb_path)
    if device is None:
        raise RuntimeError(
            f"BRIO at bus {bus} device {devnum} has no V4L capture node"
        )
    return device, bus, devnum


class _V4l2Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


def lock_focus(device: str, focus: int = FOCUS_LOCKED) -> None:
    """Hold far focus and a short shutter so motion does not smear the frame."""
    request = (3 << 30) | (ctypes.sizeof(_V4l2Control) << 16) | (ord("V") << 8) | 28
    controls = (
        (FOCUS_AUTO, 0),
        (FOCUS_ABSOLUTE, focus),
        (EXPOSURE_AUTO, EXPOSURE_MANUAL),
        (EXPOSURE_ABSOLUTE, EXPOSURE_LOCKED),
        (EXPOSURE_DYNAMIC, 0),
        (GAIN, GAIN_LOCKED),
    )
    fd = os.open(device, os.O_RDWR)
    try:
        for control, value in controls:
            fcntl.ioctl(fd, request, _V4l2Control(control, value))
    finally:
        os.close(fd)


def whip_url(host: str, webrtc_port: int, path: str) -> str:
    return f"http://{host}:{webrtc_port}/{path.strip('/')}/whip"


def build_pipeline(device: str) -> str:
    return " ".join(
        [
            f"v4l2src device={device} io-mode=2 do-timestamp=true",
            f"! image/jpeg,width={WIDTH},height={HEIGHT},framerate={FPS}/1",
            "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream",
            "! jpegdec",
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


def attach_webrtc(pipeline: Gst.Pipeline) -> Gst.Element:
    webrtc = Gst.ElementFactory.make("webrtcbin", "send")
    if webrtc is None:
        raise RuntimeError("webrtcbin is not available")
    webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
    webrtc.set_property("latency", 0)
    pipeline.add(webrtc)
    pay = pipeline.get_by_name("pay")
    sink = webrtc.request_pad_simple("sink_%u")
    if pay is None or sink is None:
        raise RuntimeError("could not request a WebRTC sink pad")
    linked = pay.get_static_pad("src").link(sink)
    if linked != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"could not link video to WebRTC ({linked.value_nick})")
    return webrtc


def _sdp_from_text(text: str):
    if not text.endswith("\r\n"):
        text = text.rstrip("\n") + "\r\n"
    result = GstSdp.SDPMessage.new_from_text(text)
    if isinstance(result, tuple):
        status, message = result
        if status != GstSdp.SDPResult.OK or message is None:
            raise RuntimeError(f"could not parse WHIP answer ({status})")
        return message
    if result is None:
        raise RuntimeError("could not parse WHIP answer")
    return result


class WhipSession:
    def __init__(self, webrtc: Gst.Element, url: str, on_drop) -> None:
        self.webrtc = webrtc
        self.url = url
        self.on_drop = on_drop
        self._posted = False
        webrtc.connect("on-negotiation-needed", self._on_negotiation)
        webrtc.connect("notify::ice-gathering-state", self._on_ice)
        webrtc.connect("notify::connection-state", self._on_connection)

    def _on_negotiation(self, webrtc: Gst.Element) -> None:
        promise = Gst.Promise.new_with_change_func(self._on_offer, webrtc, None)
        webrtc.emit("create-offer", None, promise)

    def _on_offer(self, promise: Gst.Promise, webrtc: Gst.Element, *_extra: object) -> None:
        promise.wait()
        reply = promise.get_reply()
        if reply is None:
            print("WHIP offer was empty", file=sys.stderr)
            return
        offer = reply.get_value("offer")
        webrtc.emit("set-local-description", offer, Gst.Promise.new())

    def _on_connection(self, webrtc: Gst.Element, _pspec: object) -> None:
        state = webrtc.get_property("connection-state")
        failed = (
            GstWebRTC.WebRTCPeerConnectionState.FAILED,
            GstWebRTC.WebRTCPeerConnectionState.CLOSED,
        )
        if state in failed:
            print(f"WebRTC {state.value_nick}, reconnecting", file=sys.stderr)
            self.on_drop()

    def _on_ice(self, webrtc: Gst.Element, _pspec: object) -> None:
        state = webrtc.get_property("ice-gathering-state")
        if state != GstWebRTC.WebRTCICEGatheringState.COMPLETE or self._posted:
            return
        self._posted = True
        description = webrtc.get_property("local-description")
        if description is None:
            print("WHIP local description missing", file=sys.stderr)
            return
        offer = description.sdp.as_text()
        threading.Thread(target=self._post_offer, args=(offer,), daemon=True).start()

    def _post_offer(self, offer: str) -> None:
        request = urllib.request.Request(
            self.url,
            data=offer.encode(),
            method="POST",
            headers={"Content-Type": "application/sdp"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answer = response.read().decode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            print(f"WHIP failed: HTTP {exc.code} {body}", file=sys.stderr)
            GLib.idle_add(self._fail)
            return
        except Exception as exc:
            print(f"WHIP failed: {exc}", file=sys.stderr)
            GLib.idle_add(self._fail)
            return
        GLib.idle_add(self._apply_answer, answer)

    def _fail(self) -> bool:
        self.on_drop()
        return False

    def _apply_answer(self, answer: str) -> bool:
        try:
            description = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER,
                _sdp_from_text(answer),
            )
            self.webrtc.emit("set-remote-description", description, Gst.Promise.new())
            print(f"WHIP connected {self.url}")
        except Exception as exc:
            print(f"WHIP answer rejected: {exc}", file=sys.stderr)
            self.on_drop()
        return False


class Publisher:
    def __init__(self, pipeline: str, whip: str, device: str) -> None:
        self.pipeline_text = pipeline
        self.whip = whip
        self.device = device
        self.loop = GLib.MainLoop()
        self.pipeline: Gst.Pipeline | None = None
        self.session: WhipSession | None = None
        self.stop_requested = False
        self.failed = False

    def _lock_focus(self) -> bool:
        try:
            lock_focus(self.device)
        except OSError as exc:
            print(f"could not lock focus: {exc}", file=sys.stderr)
        else:
            print(f"focus locked at {FOCUS_LOCKED}, shutter {EXPOSURE_LOCKED // 10}ms")
        return False

    def _attach_timestamp(self) -> None:
        stamp = self.pipeline.get_by_name("stamp")
        if stamp is None:
            return
        pad = stamp.get_static_pad("video_sink")
        if pad is None:
            return
        pad.add_probe(Gst.PadProbeType.BUFFER, self._stamp_frame, stamp)

    @staticmethod
    def _stamp_frame(_pad: Gst.Pad, _info: Gst.PadProbeInfo, stamp: Gst.Element) -> Gst.PadProbeReturn:
        now = datetime.now()
        stamp.set_property("text", now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}")
        return Gst.PadProbeReturn.OK

    def _drop(self) -> None:
        self.failed = True
        if self.loop.is_running():
            self.loop.quit()

    def stop(self) -> None:
        self.stop_requested = True
        if self.loop.is_running():
            self.loop.quit()

    def _on_message(self, _bus: Gst.Bus, message: Gst.Message) -> bool:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"pipeline error: {err.message}", file=sys.stderr)
            if debug:
                print(debug, file=sys.stderr)
            self.failed = True
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            print("stream ended", file=sys.stderr)
            self.failed = True
            self.loop.quit()
        return True

    def run_once(self) -> bool:
        """Play until stop or failure. Return True when the caller should retry."""
        self.failed = False
        self.pipeline = Gst.parse_launch(self.pipeline_text)
        self.pipeline.set_latency(0)
        try:
            webrtc = attach_webrtc(self.pipeline)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            self.failed = True
            return False
        self.session = WhipSession(webrtc, self.whip, self._drop)
        self._attach_timestamp()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        print("streaming (Ctrl+C to stop)")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            print("failed to start pipeline", file=sys.stderr)
            self.failed = True
        elif not self.stop_requested:
            self._lock_focus()
            GLib.timeout_add(500, self._lock_focus)
            self.loop.run()
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(2 * Gst.SECOND)
        self.pipeline = None
        return self.failed and not self.stop_requested


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream the Logitech BRIO to MediaMTX /stream/g1/front.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="MediaMTX host")
    parser.add_argument("--webrtc-port", type=int, default=DEFAULT_WEBRTC_PORT)
    parser.add_argument("--path", default=DEFAULT_PATH, help="MediaMTX stream path")
    parser.add_argument(
        "--device",
        default="",
        help="V4L device. Default: BRIO capture node for 046d:085e",
    )
    return parser.parse_args()


def main() -> int:
    Gst.init(None)
    args = parse_args()
    if args.device:
        device, bus, devnum = args.device, USB_BUS, USB_DEVNUM
    else:
        device, bus, devnum = find_brio()
    location = whip_url(args.host, args.webrtc_port, args.path)
    print(
        f"BRIO bus {bus} device {devnum} ({USB_VENDOR}:{USB_PRODUCT}) {device} "
        f"{WIDTH}x{HEIGHT}@{FPS}"
    )
    print(f"publish {location}")
    print(f"watch rtsp://{args.host}:8554/{args.path.strip('/')}")

    publisher = Publisher(build_pipeline(device), location, device)

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
