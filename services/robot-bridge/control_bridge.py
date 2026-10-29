#!/usr/bin/env python3
"""Bridge ZMQ lidar, odometry, and control onto ROS 2.

  tcp://127.0.0.1:5600  [lidar, packet] / [odom, json]  -> /livox/lidar, /odom/*
  tcp://127.0.0.1:5603  {type: cmd_vel|stick|stop}      -> /cmd_vel

Payloads match the G1 control hub:

  {"type": "cmd_vel", "vx": 0.2, "vy": 0.0, "w": 0.1}
  {"type": "stick", "lx": 0.0, "ly": 0.3, "rx": 0.0}
  {"type": "stop"}

Stick uses the G1 scales: vx = ly * 0.6, vy = -lx * 0.6, w = -rx.
One zero Twist is published when the stream is quiet for idle_timeout_sec.

  ./services/robot-bridge/deploy.sh
"""

from __future__ import annotations

import json
import math
import os
import struct
import time

import numpy as np
import rclpy
import zmq
from geometry_msgs.msg import Quaternion, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2, PointField

# Match g1_controller_bridge LINEAR_SCALE / YAW_SCALE.
DEFAULT_LINEAR_SCALE = 0.6
DEFAULT_YAW_SCALE = 1.0

# Livox Mid-360 Ethernet packet. data_type 1 is Cartesian, millimetres, 14 bytes/point.
_LIDAR_HEADER = 36
_POINT_HIGH = np.dtype([
    ("x", "<i4"),
    ("y", "<i4"),
    ("z", "<i4"),
    ("reflectivity", "u1"),
    ("tag", "u1"),
])
_POINT_LOW = np.dtype([
    ("x", "<i2"),
    ("y", "<i2"),
    ("z", "<i2"),
    ("reflectivity", "u1"),
    ("tag", "u1"),
])
_ODOM_SOURCES = ("odom", "pelvis", "torso", "fusion", "fusion-torso")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("control fields must be numbers") from exc
    if not math.isfinite(number):
        raise ValueError("control fields must be finite")
    return number


def velocity_from_control(body: dict, linear_scale: float, yaw_scale: float) -> tuple[float, float, float]:
    """Return vx, vy, yaw rate. Raises ValueError on a payload the hub would reject."""
    kind = str(body.get("type") or body.get("op") or "").strip().lower()
    if kind in ("stop", "halt"):
        return 0.0, 0.0, 0.0
    if kind in ("cmd_vel", "sport", "velocity"):
        return (
            _num(body.get("vx")),
            _num(body.get("vy")),
            _num(body.get("w", body.get("wz"))),
        )
    if kind in ("stick", "wireless"):
        lx = _num(body.get("lx"))
        ly = _num(body.get("ly"))
        rx = _num(body.get("rx"))
        return ly * linear_scale, -lx * linear_scale, -rx * yaw_scale
    raise ValueError("type must be stick, cmd_vel, or stop")


def odom_ros_topic(source_id: str) -> str:
    if source_id == "odom":
        return "/odom"
    return "/odom/" + source_id.replace("-", "_")


def odom_child_frame(source_id: str) -> str:
    if source_id in ("odom", "fusion"):
        return "base_link"
    if source_id == "fusion-torso":
        return "torso"
    return source_id.replace("-", "_")


def _vec3(value) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return 0.0, 0.0, 0.0
    return _num(value[0]), _num(value[1]), _num(value[2])


def _quat(roll: float, pitch: float, yaw: float) -> Quaternion:
    half_roll, half_pitch, half_yaw = roll * 0.5, pitch * 0.5, yaw * 0.5
    cr, sr = math.cos(half_roll), math.sin(half_roll)
    cp, sp = math.cos(half_pitch), math.sin(half_pitch)
    cy, sy = math.cos(half_yaw), math.sin(half_yaw)
    quat = Quaternion()
    quat.w = cr * cp * cy + sr * sp * sy
    quat.x = sr * cp * cy - cr * sp * sy
    quat.y = cr * sp * cy + sr * cp * sy
    quat.z = cr * cp * sy - sr * sp * cy
    return quat


def points_from_packet(raw: bytes) -> np.ndarray | None:
    """Cartesian Livox points as float32 columns x, y, z (metres) and intensity."""
    if len(raw) < _LIDAR_HEADER:
        return None
    dot_num = struct.unpack_from("<H", raw, 5)[0]
    if dot_num <= 0:
        return None
    data_type = raw[10]
    if data_type == 1:
        dtype = _POINT_HIGH
        scale = 0.001
    elif data_type == 2:
        dtype = _POINT_LOW
        scale = 0.01
    else:
        return None
    need = _LIDAR_HEADER + dot_num * dtype.itemsize
    if len(raw) < need:
        return None
    pts = np.frombuffer(raw, dtype=dtype, count=dot_num, offset=_LIDAR_HEADER)
    out = np.empty((dot_num, 4), np.float32)
    out[:, 0] = pts["x"] * scale
    out[:, 1] = pts["y"] * scale
    out[:, 2] = pts["z"] * scale
    out[:, 3] = pts["reflectivity"]
    return out


def _twist(vx: float, vy: float, wz: float) -> Twist:
    msg = Twist()
    msg.linear.x = float(vx)
    msg.linear.y = float(vy)
    msg.angular.z = float(wz)
    return msg


class ZmqControlBridge(Node):
    def __init__(self) -> None:
        super().__init__("zmq_control_bridge")
        default_ep = os.environ.get("ZMQ_CONTROL_SUB", "tcp://127.0.0.1:5603").strip()
        default_topic = os.environ.get("CMD_VEL_TOPIC", "/cmd_vel").strip() or "/cmd_vel"
        self.declare_parameter("zmq_endpoint", default_ep or "tcp://127.0.0.1:5603")
        self.declare_parameter("cmd_vel_topic", default_topic)
        self.declare_parameter("idle_timeout_sec", _env_float("IDLE_TIMEOUT_SEC", 0.5))
        self.declare_parameter("stick_linear_scale", _env_float("STICK_LINEAR_SCALE", DEFAULT_LINEAR_SCALE))
        self.declare_parameter("stick_yaw_scale", _env_float("STICK_YAW_SCALE", DEFAULT_YAW_SCALE))

        self._endpoint = self.get_parameter("zmq_endpoint").get_parameter_value().string_value.strip()
        topic = self.get_parameter("cmd_vel_topic").get_parameter_value().string_value.strip() or "/cmd_vel"
        self._idle = float(self.get_parameter("idle_timeout_sec").get_parameter_value().double_value)
        self._linear_scale = float(self.get_parameter("stick_linear_scale").get_parameter_value().double_value)
        self._yaw_scale = float(self.get_parameter("stick_yaw_scale").get_parameter_value().double_value)
        if not self._endpoint:
            raise ValueError("zmq_endpoint is empty")

        lidar_ep = os.environ.get("ZMQ_LIDAR_SUB", "tcp://127.0.0.1:5600").strip()
        lidar_topic = os.environ.get("LIDAR_TOPIC", "/livox/lidar").strip() or "/livox/lidar"
        self._lidar_frame = os.environ.get("LIDAR_FRAME", "livox_frame").strip() or "livox_frame"
        self._odom_frame = os.environ.get("ODOM_FRAME", "odom").strip() or "odom"
        self._lidar_period = _env_float("LIDAR_FRAME_SEC", 0.1)
        self.declare_parameter("lidar_endpoint", lidar_ep)
        self.declare_parameter("lidar_topic", lidar_topic)

        self._pub = self.create_publisher(Twist, topic, 10)
        self._lidar_pub = self.create_publisher(PointCloud2, lidar_topic, 5)
        self._odom_pubs = {
            source_id: self.create_publisher(Odometry, odom_ros_topic(source_id), 10)
            for source_id in _ODOM_SOURCES
        }
        self._lidar_chunks: list[np.ndarray] = []
        self._lidar_window = time.monotonic()
        self._lidar_pkts = 0
        self._lidar_pts = 0
        self._odom_n = 0
        self._stat_mono = time.monotonic()
        self._lidar_logged = False

        self._ctx = zmq.Context()
        self._sub = self._open_sub(self._endpoint, 200)
        self._lidar_sub = None
        lidar_endpoint = self.get_parameter("lidar_endpoint").get_parameter_value().string_value.strip()
        if lidar_endpoint.lower() not in ("", "off", "none"):
            self._lidar_sub = self._open_sub(lidar_endpoint, 4000)
        else:
            lidar_endpoint = ""
        health = os.environ.get("ROBOT_BRIDGE_HEALTH", "/tmp/robot-bridge-healthy").strip()
        self._health = health
        if health:
            with open(health, "w", encoding="utf-8") as handle:
                handle.write("ok\n")

        self._held = False
        self._last_mono = 0.0
        self._last_sig: tuple | None = None
        self._bad = 0
        self._accepted = 0
        self.create_timer(0.02, self._poll)
        self.get_logger().info(
            f"ZMQ {self._endpoint} -> {topic}  "
            f"stick scale linear={self._linear_scale} yaw={self._yaw_scale}  "
            f"idle={self._idle:.2f}s"
        )
        self.get_logger().info(
            f"ZMQ {lidar_endpoint or 'off'} -> {lidar_topic} ({self._lidar_frame}, "
            f"{self._lidar_period:.2f}s) and /odom, /odom/pelvis, /odom/torso, "
            f"/odom/fusion, /odom/fusion_torso"
        )

    def _poll(self) -> None:
        now = self._now()
        while True:
            try:
                parts = self._sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            except zmq.ZMQError as exc:
                self.get_logger().error(f"zmq recv failed: {exc}")
                return
            self._on_frame(parts[-1] if parts else b"")
            now = self._now()
        if self._lidar_sub is not None:
            while True:
                try:
                    parts = self._lidar_sub.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    break
                except zmq.ZMQError as exc:
                    self.get_logger().error(f"zmq lidar recv failed: {exc}")
                    break
                self._on_sensor(parts)
        self._flush_lidar()
        self._log_rates()
        if self._held and self._idle > 0.0 and (now - self._last_mono) >= self._idle:
            self._publish(0.0, 0.0, 0.0, "idle")
            self._held = False

    def _open_sub(self, endpoint: str, hwm: int):
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVHWM, hwm)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.connect(endpoint)
        return sock

    def _on_sensor(self, parts: list[bytes]) -> None:
        if len(parts) >= 2 and parts[0] in (b"lidar", b"odom"):
            topic, raw = parts[0], parts[-1]
        else:
            topic, raw = b"lidar", parts[-1] if parts else b""
        if topic == b"odom":
            self._on_odom(raw)
            return
        cloud = points_from_packet(raw)
        if cloud is None:
            return
        self._lidar_chunks.append(cloud)
        self._lidar_pkts += 1
        self._lidar_pts += int(cloud.shape[0])

    def _flush_lidar(self) -> None:
        if not self._lidar_chunks:
            return
        now = time.monotonic()
        if now - self._lidar_window < self._lidar_period:
            return
        block = np.concatenate(self._lidar_chunks, axis=0)
        self._lidar_chunks.clear()
        self._lidar_window = now
        msg = PointCloud2()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self._lidar_frame
        msg.height = 1
        msg.width = int(block.shape[0])
        msg.fields = [
            PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
            PointField(name="intensity", offset=12, datatype=PointField.FLOAT32, count=1),
        ]
        msg.is_bigendian = False
        msg.point_step = 16
        msg.row_step = 16 * msg.width
        msg.data = block.tobytes()
        msg.is_dense = True
        self._lidar_pub.publish(msg)
        if not self._lidar_logged:
            self._lidar_logged = True
            self.get_logger().info(f"lidar {msg.width} points  frame {self._lidar_frame}")

    def _on_odom(self, raw: bytes) -> None:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(body, dict):
            return
        source_id = str(body.get("id") or "odom")
        pub = self._odom_pubs.get(source_id)
        if pub is None:
            pub = self.create_publisher(Odometry, odom_ros_topic(source_id), 10)
            self._odom_pubs[source_id] = pub
        try:
            x, y, z = _vec3(body.get("position"))
            roll, pitch, yaw = _vec3(body.get("rpy"))
            vx, vy, vz = _vec3(body.get("linear"))
            wx, wy, wz = _vec3(body.get("angular"))
        except ValueError:
            return
        msg = Odometry()
        msg.header.frame_id = self._odom_frame
        msg.child_frame_id = odom_child_frame(source_id)
        ts = body.get("ts")
        if isinstance(ts, (int, float)) and math.isfinite(float(ts)):
            sec = int(ts)
            nsec = int(round((float(ts) - sec) * 1e9))
            if nsec >= 1_000_000_000:
                sec += 1
                nsec -= 1_000_000_000
            msg.header.stamp.sec = sec
            msg.header.stamp.nanosec = nsec
        else:
            msg.header.stamp = self.get_clock().now().to_msg()
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.position.z = z
        msg.pose.pose.orientation = _quat(roll, pitch, yaw)
        msg.twist.twist.linear.x = vx
        msg.twist.twist.linear.y = vy
        msg.twist.twist.linear.z = vz
        msg.twist.twist.angular.x = wx
        msg.twist.twist.angular.y = wy
        msg.twist.twist.angular.z = wz
        pub.publish(msg)
        self._odom_n += 1

    def _log_rates(self) -> None:
        now = time.monotonic()
        elapsed = now - self._stat_mono
        if elapsed < 5.0:
            return
        self.get_logger().info(
            "lidar %d pkt/s  %d pt/s | odom %d msg/s"
            % (
                round(self._lidar_pkts / elapsed),
                round(self._lidar_pts / elapsed),
                round(self._odom_n / elapsed),
            )
        )
        self._lidar_pkts = 0
        self._lidar_pts = 0
        self._odom_n = 0
        self._stat_mono = now

    def _on_frame(self, raw: bytes) -> None:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._note_bad("invalid JSON")
            return
        if not isinstance(body, dict):
            self._note_bad("control message must be an object")
            return
        try:
            vx, vy, wz = velocity_from_control(body, self._linear_scale, self._yaw_scale)
        except ValueError as exc:
            self._note_bad(str(exc))
            return
        kind = str(body.get("type") or body.get("op") or "").strip().lower()
        self._accepted += 1
        self._last_mono = self._now()
        self._publish(vx, vy, wz, kind or "control")
        self._held = not _zero(vx, vy, wz)

    def _publish(self, vx: float, vy: float, wz: float, kind: str) -> None:
        self._pub.publish(_twist(vx, vy, wz))
        sig = (kind, round(vx, 3), round(vy, 3), round(wz, 3))
        if sig == self._last_sig:
            return
        self._last_sig = sig
        self.get_logger().info(f"{kind}  vx={vx:+.3f} vy={vy:+.3f} wz={wz:+.3f}")

    def _note_bad(self, detail: str) -> None:
        self._bad += 1
        if self._bad == 1 or self._bad % 50 == 0:
            self.get_logger().warn(f"dropped control message ({self._bad}): {detail}")

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def destroy_node(self) -> bool:
        if self._held:
            try:
                self._publish(0.0, 0.0, 0.0, "shutdown")
            except Exception:
                pass
            self._held = False
        socks = [self._sub, getattr(self, "_lidar_sub", None)]
        ctx = self._ctx
        self._sub = None
        self._lidar_sub = None
        self._ctx = None
        for sock in socks:
            if sock is None:
                continue
            try:
                sock.close(linger=0)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.term()
            except Exception:
                pass
        health = getattr(self, "_health", "")
        if health:
            try:
                os.remove(health)
            except OSError:
                pass
        return super().destroy_node()


def _zero(vx: float, vy: float, wz: float) -> bool:
    return vx == 0.0 and vy == 0.0 and wz == 0.0


def main() -> None:
    rclpy.init()
    node = None
    try:
        node = ZmqControlBridge()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    except Exception:
        if rclpy.ok():
            raise
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
