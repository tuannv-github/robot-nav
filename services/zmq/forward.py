#!/usr/bin/env python3
"""Forward Livox point packets and G1 odometry to the ZMQ broker.

Lidar is one raw Livox Ethernet packet per message.
Odometry is multipart [b"odom", json] on the same PULL socket.
Stats are written to ZMQ_FORWARD_STATUS for the controller console.
"""

from __future__ import annotations

import json
import math
import os
import select
import socket
import struct
import subprocess
import threading
import time

import zmq

ODOM_SOURCES = (
    {"id": "pelvis", "name": "Pelvis", "topic": "rt/state_estimator/odom_pelvis", "kind": "nav"},
    {"id": "torso", "name": "Torso", "topic": "rt/state_estimator/odom_torso", "kind": "nav"},
    {"id": "odom", "name": "Odom mode", "topic": "rt/odommodestate", "kind": "sport"},
    {"id": "fusion", "name": "Fusion", "topic": "rt/state_estimator/fusion_odom", "kind": "nav"},
    {"id": "fusion-torso", "name": "Fusion torso", "topic": "rt/state_estimator/fusion_odom_torso", "kind": "nav"},
)


def _vec3(x, y, z) -> list[float]:
    return [round(float(x), 4), round(float(y), 4), round(float(z), 4)]


def _rpy_from_quat(x: float, y: float, z: float, w: float) -> list[float]:
    sin_roll = 2.0 * (w * x + y * z)
    cos_roll = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sin_roll, cos_roll)
    sin_pitch = 2.0 * (w * y - z * x)
    if abs(sin_pitch) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sin_pitch)
    else:
        pitch = math.asin(sin_pitch)
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(sin_yaw, cos_yaw)
    return _vec3(roll, pitch, yaw)


def _dds_interface() -> str:
    preferred = os.environ.get("G1_DDS_INTERFACE", "auto").strip() or "auto"
    if preferred != "auto":
        return preferred
    try:
        import fcntl

        for iface in os.listdir("/sys/class/net"):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))
                ip = socket.inet_ntoa(packed[20:24])
            except OSError:
                continue
            if ip.startswith("192.168.123."):
                return iface
    except OSError:
        pass
    return "enP8p1s0"


def _parse_odom(kind: str, msg) -> dict:
    if kind == "nav":
        position = msg.pose.pose.position
        quat = msg.pose.pose.orientation
        linear = msg.twist.twist.linear
        angular = msg.twist.twist.angular
        return {
            "position": _vec3(position.x, position.y, position.z),
            "rpy": _rpy_from_quat(quat.x, quat.y, quat.z, quat.w),
            "linear": _vec3(linear.x, linear.y, linear.z),
            "angular": _vec3(angular.x, angular.y, angular.z),
        }
    rpy = msg.imu_state.rpy
    return {
        "position": _vec3(msg.position[0], msg.position[1], msg.position[2]),
        "rpy": _vec3(rpy[0], rpy[1], rpy[2]),
        "linear": _vec3(msg.velocity[0], msg.velocity[1], msg.velocity[2]),
        "angular": _vec3(0.0, 0.0, msg.yaw_speed),
    }


def _iface_for_ip(host: str) -> str:
    try:
        import fcntl

        for iface in os.listdir("/sys/class/net"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))
            except OSError:
                continue
            finally:
                sock.close()
            if socket.inet_ntoa(packed[20:24]) == host:
                return iface
    except OSError:
        pass
    return ""


def _read_lidar_mode(path: str) -> str:
    try:
        text = open(path, encoding="utf-8").read().strip().lower()
    except OSError:
        return "bind"
    return "sniff" if text == "sniff" else "bind"


def _udp_payload(frame: bytes, host: str, port: int) -> bytes:
    if len(frame) < 42:
        return b""
    eth_type = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    if eth_type == 0x8100 and len(frame) >= 46:
        eth_type = struct.unpack_from("!H", frame, 16)[0]
        offset = 18
    if eth_type != 0x0800 or len(frame) < offset + 28:
        return b""
    version = frame[offset] >> 4
    if version != 4 or frame[offset + 9] != 17:
        return b""
    ihl = (frame[offset] & 0x0F) * 4
    udp_off = offset + ihl
    if ihl < 20 or len(frame) < udp_off + 8:
        return b""
    dst_ip = socket.inet_ntoa(frame[offset + 16:offset + 20])
    dst_port = struct.unpack_from("!H", frame, udp_off + 2)[0]
    if dst_ip != host or dst_port != port:
        return b""
    length = struct.unpack_from("!H", frame, udp_off + 4)[0]
    if length < 8:
        return b""
    return frame[udp_off + 8:udp_off + length]


class LidarCapture:
    """Bind the Livox UDP port, or copy those packets with dumpcap."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.mode = ""
        self.iface = ""
        self.error = ""
        self.sock = None
        self.proc = None
        self.buf = b""
        self.link = 1
        self.header = False

    def _alive(self) -> bool:
        if self.sock is not None:
            return True
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        proc = self.proc
        self.proc = None
        self.buf = b""
        self.header = False
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def ensure(self, mode: str) -> None:
        if mode == self.mode and (self._alive() or self.error):
            return
        self.close()
        self.mode = mode
        self.error = ""
        try:
            if mode == "sniff":
                self._open_sniff()
            else:
                self.sock = self._open_bind()
        except OSError as exc:
            self.sock = None
            self.error = str(exc)
            print(f"lidar {mode} failed: {exc}", flush=True)

    def _open_bind(self) -> socket.socket:
        self.iface = ""
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind((self.host, self.port))
        udp.settimeout(0.5)
        print(f"lidar bind {self.host}:{self.port}", flush=True)
        return udp

    def _open_sniff(self) -> None:
        iface = _iface_for_ip(self.host) or _dds_interface()
        self.iface = iface
        dumpcap = "/usr/bin/dumpcap"
        if not os.access(dumpcap, os.X_OK):
            raise OSError(
                "dumpcap is not executable; restart the forwarder in group wireshark"
            )
        cmd = [
            dumpcap, "-P", "-q",
            "-i", iface,
            "-f", f"udp dst port {self.port} and dst host {self.host}",
            "-w", "-",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.4)
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode("utf-8", "replace").strip()
            raise OSError(err or f"dumpcap exited {self.proc.returncode}")
        os.set_blocking(self.proc.stdout.fileno(), False)
        os.set_blocking(self.proc.stderr.fileno(), False)
        print(f"lidar sniff {iface} {self.host}:{self.port} via dumpcap", flush=True)

    def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            self.proc.stderr.read()
        except BlockingIOError:
            pass

    def _read_stdout(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            return
        try:
            chunk = self.proc.stdout.read(65536)
        except BlockingIOError:
            return
        if chunk:
            self.buf += chunk

    def _pop_packet(self) -> bytes:
        if not self.header:
            if len(self.buf) < 24:
                return b""
            magic, _maj, _min, _zone, _sig, _snap, link = struct.unpack_from("<IHHIIII", self.buf, 0)
            if magic != 0xA1B2C3D4:
                raise OSError(f"unexpected pcap magic {magic:#x}")
            self.link = link
            self.buf = self.buf[24:]
            self.header = True
        if len(self.buf) < 16:
            return b""
        _ts, _us, incl, _orig = struct.unpack_from("<IIII", self.buf, 0)
        if incl > 65535 or len(self.buf) < 16 + incl:
            return b""
        frame = self.buf[16:16 + incl]
        self.buf = self.buf[16 + incl:]
        if self.link != 1:
            return b""
        return _udp_payload(frame, self.host, self.port)

    def _recv_pcap(self) -> bytes:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self._drain_stderr()
            if self.proc.poll() is not None:
                raise OSError(f"dumpcap exited {self.proc.returncode}")
            try:
                payload = self._pop_packet()
            except OSError as exc:
                self.error = str(exc)
                self.close()
                print(f"lidar sniff failed: {exc}", flush=True)
                return b""
            if payload:
                return payload
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            select.select([self.proc.stdout], [], [], min(0.2, remain))
            self._read_stdout()
        try:
            return self._pop_packet()
        except OSError as exc:
            self.error = str(exc)
            self.close()
            print(f"lidar sniff failed: {exc}", flush=True)
            return b""

    def recv(self) -> bytes:
        if self.proc is not None:
            return self._recv_pcap()
        if self.sock is None:
            time.sleep(0.5)
            return b""
        try:
            data, _addr = self.sock.recvfrom(65535)
            return data
        except socket.timeout:
            return b""


class ForwardStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lidar_error = ""
        self.lidar_mode = "bind"
        self.lidar_iface = ""
        self.odom_error = ""
        self.lidar_pkts = 0
        self.lidar_pts = 0
        self.lidar_drop = 0
        self.lidar_total = 0
        self.odom_msgs = 0
        self.odom_drop = 0
        self.odom_total = 0
        self.sources = {
            spec["id"]: {"sent": 0, "count": 0, "hz": 0.0, "mono": 0.0, "hz_stamp": time.monotonic(), "hz_count": 0}
            for spec in ODOM_SOURCES
        }

    def note_odom(self, source_id: str) -> None:
        now = time.monotonic()
        slot = self.sources.get(source_id)
        if slot is None:
            return
        slot["sent"] += 1
        slot["count"] += 1
        slot["mono"] = now
        window = now - slot["hz_stamp"]
        if window >= 1.0:
            slot["hz"] = (slot["count"] - slot["hz_count"]) / window
            slot["hz_stamp"] = now
            slot["hz_count"] = slot["count"]


def _start_odom(push, stats: ForwardStats, iface: str) -> None:
    def run() -> None:
        try:
            import unitree_sdk2py.core.channel as channel
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
        except Exception as exc:
            stats.odom_error = str(exc)
            print(f"odom import failed: {exc}", flush=True)
            return
        log_path = os.environ.get(
            "G1_CDDS_LOG_FORWARD",
            os.path.join(os.path.expanduser("~"), ".g1-cdds-forward.log"),
        )
        channel.ChannelConfigHasInterface = channel.ChannelConfigHasInterface.replace(
            "/tmp/cdds.LOG",
            log_path,
        )
        try:
            ChannelFactoryInitialize(0, iface)
        except Exception as exc:
            stats.odom_error = str(exc)
            print(f"odom DDS init failed: {exc}", flush=True)
            return
        print(f"odom DDS {iface}", flush=True)

        def on_sample(msg, source_id, kind, name, topic):
            try:
                sample = _parse_odom(kind, msg)
            except Exception:
                return
            sample.update({"id": source_id, "name": name, "topic": topic, "ts": time.time()})
            payload = json.dumps(sample, separators=(",", ":")).encode("utf-8")
            with stats.lock:
                try:
                    push.send_multipart([b"odom", payload], flags=zmq.NOBLOCK)
                except zmq.Again:
                    stats.odom_drop += 1
                    return
                stats.odom_msgs += 1
                stats.odom_total += 1
                stats.note_odom(source_id)

        for spec in ODOM_SOURCES:
            msg_type = Odometry_ if spec["kind"] == "nav" else SportModeState_
            sub = ChannelSubscriber(spec["topic"], msg_type)

            def handler(msg, source_id=spec["id"], kind=spec["kind"], name=spec["name"], topic=spec["topic"]):
                on_sample(msg, source_id, kind, name, topic)

            sub.Init(handler, 1)
        while True:
            time.sleep(3600)

    threading.Thread(target=run, name="zmq-odom", daemon=True).start()


def _write_status(path: str, body: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> None:
    host = os.environ.get("LIDAR_UDP_HOST", "192.168.123.164").strip()
    port = int(os.environ.get("LIDAR_UDP_PORT", "56301"))
    endpoint = os.environ.get("ZMQ_LIDAR_PUSH", "tcp://10.1.137.133:5601").strip()
    status_path = os.environ.get("ZMQ_FORWARD_STATUS", "/tmp/g1-zmq-forward.json")
    mode_path = os.environ.get("ZMQ_LIDAR_MODE", "/tmp/g1-zmq-lidar.mode")
    pub = os.environ.get("ZMQ_SERVER_PUB", "tcp://10.1.137.133:5600").strip()
    iface = _dds_interface()

    ctx = zmq.Context()
    push = ctx.socket(zmq.PUSH)
    push.setsockopt(zmq.LINGER, 0)
    push.setsockopt(zmq.SNDHWM, 2000)
    push.connect(endpoint)

    stats = ForwardStats()
    _start_odom(push, stats, iface)

    capture = LidarCapture(host, port)
    window = time.monotonic()
    mode_checked = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - mode_checked >= 0.25:
                mode_checked = now
                capture.ensure(_read_lidar_mode(mode_path))
                with stats.lock:
                    stats.lidar_mode = capture.mode or "bind"
                    stats.lidar_iface = capture.iface
                    stats.lidar_error = capture.error
            data = capture.recv()
            if data:
                points = struct.unpack_from("<H", data, 5)[0] if len(data) >= 7 else 0
                with stats.lock:
                    try:
                        push.send(data, flags=zmq.NOBLOCK)
                    except zmq.Again:
                        stats.lidar_drop += 1
                    else:
                        stats.lidar_pkts += 1
                        stats.lidar_pts += points
                        stats.lidar_total += 1
            now = time.monotonic()
            if now - window < 1.0:
                continue
            elapsed = now - window
            with stats.lock:
                lidar_rate = stats.lidar_pkts / elapsed
                point_rate = stats.lidar_pts / elapsed
                odom_rate = stats.odom_msgs / elapsed
                body = _status_body(stats, host, port, endpoint, pub, iface, lidar_rate, point_rate, odom_rate)
                stats.lidar_pkts = stats.lidar_pts = 0
                stats.odom_msgs = 0
            window = now
            _write_status(status_path, body)
            print(
                "forward %s lidar %d pkt/s  %d pt/s | odom %d msg/s"
                % (stats.lidar_mode, round(lidar_rate), round(point_rate), round(odom_rate)),
                flush=True,
            )
    finally:
        capture.close()
        push.close(linger=0)
        ctx.term()


def _status_body(stats, host, port, endpoint, pub, iface, lidar_rate, point_rate, odom_rate) -> dict:
    now = time.time()
    mono = time.monotonic()
    sources = []
    for spec in ODOM_SOURCES:
        slot = stats.sources[spec["id"]]
        age = None if not slot["mono"] else int(max(0.0, mono - slot["mono"]) * 1000)
        sources.append({
            "id": spec["id"],
            "name": spec["name"],
            "topic": spec["topic"],
            "hz": round(float(slot["hz"]), 1),
            "sent": int(slot["sent"]),
            "age_ms": age,
        })
    return {
        "ok": True,
        "updated": now,
        "pub": pub,
        "push": endpoint,
        "lidar": {
            "up": not stats.lidar_error,
            "mode": stats.lidar_mode,
            "bind": f"{host}:{port}",
            "iface": stats.lidar_iface,
            "pkt_s": round(lidar_rate),
            "pt_s": round(point_rate),
            "drops": int(stats.lidar_drop),
            "total": int(stats.lidar_total),
            "error": stats.lidar_error,
        },
        "odom": {
            "up": not stats.odom_error,
            "iface": iface,
            "msg_s": round(odom_rate),
            "drops": int(stats.odom_drop),
            "total": int(stats.odom_total),
            "error": stats.odom_error,
            "sources": sources,
        },
    }


if __name__ == "__main__":
    main()
