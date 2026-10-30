#!/usr/bin/env python3
"""Bridge ZMQ lidar, odometry, and control onto ROS 2.

  tcp://127.0.0.1:5600  [lidar, packet] / [odom, json]  -> /livox/lidar, /odom/*, /tf
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
from collections import deque

import numpy as np
import rclpy
import zmq
from geometry_msgs.msg import Quaternion, TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2, PointField
from tf2_ros import TransformBroadcaster

# Match robot_bridge LINEAR_SCALE / YAW_SCALE.
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def _quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _rotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    ix = w * vx + y * vz - z * vy
    iy = w * vy + z * vx - x * vz
    iz = w * vz + x * vy - y * vx
    iw = -x * vx - y * vy - z * vz
    return (
        iw * -x + ix * w + iy * -z - iz * -y,
        iw * -y + iy * w + iz * -x - ix * -z,
        iw * -z + iz * w + ix * -y - iy * -x,
    )


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
    # Torso estimator is the mapping odom. Sport mode stays on its own topic.
    if source_id == "torso":
        return "/odom"
    if source_id == "odom":
        return "/odom/sport"
    return "/odom/" + source_id.replace("-", "_")


def odom_child_frame(source_id: str) -> str:
    if source_id in ("torso", "fusion"):
        return "base_link"
    if source_id == "odom":
        return "sport"
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


def _rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """ROS rpy: R = Rz(yaw) Ry(pitch) Rx(roll). Maps a sensor point into base_link."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]], dtype=np.float64)
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]], dtype=np.float64)
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    return rz @ ry @ rx


def _quat_matrix(quat) -> np.ndarray:
    x, y, z, w = quat
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array(
        [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def _slerp(a, b, u: float):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    if dot > 0.9995:
        out = a + u * (b - a)
        return out / np.linalg.norm(out)
    theta = math.acos(max(-1.0, min(1.0, dot)))
    scale = math.sin(theta)
    return (math.sin((1.0 - u) * theta) * a + math.sin(u * theta) * b) / scale


def _yaw_of(quat) -> float:
    x, y, z, w = quat
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _blend_pose(a, b, u: float):
    xyz = tuple((1.0 - u) * a[i] + u * b[i] for i in range(3))
    quat = tuple(float(v) for v in _slerp(a[3:7], b[3:7], u))
    return xyz + quat


def _advance_pose(pose, twist, dt: float):
    """Step a pose by a body-frame twist. dt may be negative."""
    if abs(dt) < 1e-6:
        return pose[:7]
    rotation = _quat_matrix(pose[3:7])
    velocity = rotation @ np.asarray(twist[:3], dtype=np.float64)
    xyz = tuple(float(pose[i] + velocity[i] * dt) for i in range(3))
    quat = pose[3:7]
    spin = _quat_mul(quat, (twist[3], twist[4], twist[5], 0.0))
    updated = tuple(quat[i] + 0.5 * dt * spin[i] for i in range(4))
    norm = math.sqrt(sum(value * value for value in updated)) or 1.0
    return xyz + tuple(value / norm for value in updated)


def interp_pose(samples, when: float):
    """Pose (x, y, z, qx, qy, qz, qw) at `when`.

    Samples are (time, x, y, z, qx, qy, qz, qw, vx, vy, vz, wx, wy, wz).
    Twist is body frame. Outside the samples the nearest twist is integrated,
    so a cloud newer than the last odom still moves.
    """
    if not samples:
        return None
    if when <= samples[0][0]:
        dt = when - samples[0][0]
        if dt < -0.15:
            return None
        return _advance_pose(samples[0][1:8], samples[0][8:14], dt)
    if when >= samples[-1][0]:
        dt = when - samples[-1][0]
        if dt > 0.15:
            return None
        return _advance_pose(samples[-1][1:8], samples[-1][8:14], dt)
    for index in range(1, len(samples)):
        if samples[index][0] < when:
            continue
        t0 = samples[index - 1][0]
        t1 = samples[index][0]
        span = t1 - t0
        blend = 0.0 if span <= 0.0 else (when - t0) / span
        return _blend_pose(samples[index - 1][1:8], samples[index][1:8], blend)
    return None


def deskew_points(pts: np.ndarray, pose_from, pose_to, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    """Move sensor points from pose_from into the sensor frame of pose_to."""
    if pose_from is None or pose_to is None:
        return pts
    xyz = pts[:, :3].astype(np.float64, copy=False)
    base = xyz @ rotation.T + translation
    world = base @ _quat_matrix(pose_from[3:]).T + np.asarray(pose_from[:3], dtype=np.float64)
    back = (world - np.asarray(pose_to[:3], dtype=np.float64)) @ _quat_matrix(pose_to[3:])
    sensor = (back - translation) @ rotation
    out = pts.copy()
    out[:, :3] = sensor.astype(np.float32)
    return out


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
        # Torso estimator owns /odom and odom -> base_link.
        self._odom_forward = _env_bool("ODOM_FORWARD", True)
        self._publish_tf = _env_bool("PUBLISH_ODOM_TF", True) and self._odom_forward
        # Torso /odom is an absolute pose. The first sample is the TF origin.
        # The /odom message itself stays absolute.
        self._zero_start = _env_bool("ODOM_ZERO_START", True)
        # Estimator z is a datum (several metres) and drifts. Hold TF z at 0.
        self._fixed_z = _env_bool("ODOM_FIXED_Z", True)
        self._odom_origin = None
        self._lidar_period = _env_float("LIDAR_FRAME_SEC", 0.1)
        self._deskew = _env_bool("LIDAR_DESKEW", True)
        mount_rpy = (
            math.radians(_env_float("LIDAR_ROLL", 180.0)),
            math.radians(_env_float("LIDAR_PITCH", 0.0)),
            math.radians(_env_float("LIDAR_YAW", 0.0)),
        )
        self._mount_R = _rpy_matrix(*mount_rpy)
        self._mount_t = np.array(
            [
                _env_float("LIDAR_X", 0.0),
                _env_float("LIDAR_Y", 0.0),
                _env_float("LIDAR_Z", 1.1),
            ],
            dtype=np.float64,
        )
        self._poses: deque = deque(maxlen=200)
        self._deskew_dxy = 0.0
        self._deskew_dyaw = 0.0
        self.declare_parameter("lidar_endpoint", lidar_ep)
        self.declare_parameter("lidar_topic", lidar_topic)

        self._pub = self.create_publisher(Twist, topic, 10)
        self._lidar_pub = self.create_publisher(PointCloud2, lidar_topic, 5)
        self._odom_pubs = {
            source_id: self.create_publisher(Odometry, odom_ros_topic(source_id), 10)
            for source_id in _ODOM_SOURCES
        } if self._odom_forward else {}
        self._tf = TransformBroadcaster(self) if self._publish_tf else None
        self._lidar_chunks: list[tuple[float, np.ndarray]] = []
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
            f"{self._lidar_period:.2f}s, deskew={'on' if self._deskew else 'off'})"
            + (
                ""
                if not self._odom_forward
                else " and /odom (torso), /odom/pelvis, /odom/sport, /odom/fusion, /odom/fusion_torso"
            )
        )
        if not self._odom_forward:
            self.get_logger().info("odom forward off; lidar scan owns odometry")
        elif self._tf is not None:
            self.get_logger().info(
            f"TF {self._odom_frame} -> base_link, torso, base_footprint from the torso estimator; "
            f"{self._odom_frame} -> pelvis  zero_start={self._zero_start} fixed_z={self._fixed_z}"
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
            lidar_batch = []
            while True:
                try:
                    parts = self._lidar_sub.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    break
                except zmq.ZMQError as exc:
                    self.get_logger().error(f"zmq lidar recv failed: {exc}")
                    break
                if len(parts) >= 2 and parts[0] == b"odom":
                    self._on_odom(parts[-1])
                    continue
                lidar_batch.append(parts[-1] if parts else b"")
            if lidar_batch:
                self._buffer_lidar(lidar_batch, time.time())
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

    def _buffer_lidar(self, batch: list[bytes], host_now: float) -> None:
        parsed = []
        for raw in batch:
            cloud = points_from_packet(raw)
            if cloud is None:
                continue
            stamp_ns = struct.unpack_from("<Q", raw, 28)[0] if len(raw) >= 36 else 0
            parsed.append((cloud, stamp_ns))
            self._lidar_pkts += 1
            self._lidar_pts += int(cloud.shape[0])
        if not parsed:
            return
        # time_type is unsynced, so the packet clock only orders points inside the burst.
        newest = max((stamp for _, stamp in parsed if stamp > 0), default=0)
        for cloud, stamp in parsed:
            if newest > 0 and stamp > 0 and 0 <= newest - stamp < 2_000_000_000:
                when = host_now - (newest - stamp) * 1e-9
            else:
                when = host_now
            self._lidar_chunks.append((when, cloud))

    def _note_base_pose(self, when: float, xyz, quat, twist) -> None:
        self._poses.append((
            when,
            xyz[0], xyz[1], xyz[2],
            quat[0], quat[1], quat[2], quat[3],
            twist[0], twist[1], twist[2], twist[3], twist[4], twist[5],
        ))
        cutoff = when - 2.0
        while len(self._poses) > 2 and self._poses[0][0] < cutoff:
            self._poses.popleft()

    def _flush_lidar(self) -> None:
        if not self._lidar_chunks:
            return
        now = time.monotonic()
        if now - self._lidar_window < self._lidar_period:
            return
        chunks = self._lidar_chunks
        self._lidar_chunks = []
        self._lidar_window = now
        t_ref = chunks[-1][0]
        pose_ref = interp_pose(self._poses, t_ref) if self._deskew else None
        pose_old = interp_pose(self._poses, chunks[0][0]) if pose_ref is not None else None
        if pose_ref is not None and pose_old is not None:
            self._deskew_dxy = math.hypot(pose_ref[0] - pose_old[0], pose_ref[1] - pose_old[1])
            self._deskew_dyaw = _yaw_of(pose_ref[3:]) - _yaw_of(pose_old[3:])
        blocks = []
        for when, cloud in chunks:
            if pose_ref is None:
                blocks.append(cloud)
                continue
            pose = interp_pose(self._poses, when)
            blocks.append(deskew_points(cloud, pose, pose_ref, self._mount_R, self._mount_t))
        block = np.concatenate(blocks, axis=0)
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
        if not self._odom_forward:
            return
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
        self._publish_odom_tf(source_id, msg)
        self._odom_n += 1

    def _publish_odom_tf(self, source_id: str, msg: Odometry) -> None:
        # Torso owns the origin and is published as base_link and torso.
        # Pelvis uses that same origin. Sport and fusion stay off /tf.
        if self._tf is None or source_id not in ("torso", "pelvis"):
            return
        pose = msg.pose.pose
        xyz = (pose.position.x, pose.position.y, pose.position.z)
        quat = (
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w,
        )
        if self._zero_start:
            if self._odom_origin is None:
                if source_id != "torso":
                    return
                xyz, quat = self._relative_odom(xyz, quat)
            else:
                xyz, quat = self._apply_origin(xyz, quat)
        if self._fixed_z and source_id == "torso":
            xyz = (xyz[0], xyz[1], 0.0)
        children = ("base_link", "torso") if source_id == "torso" else ("pelvis",)
        stamped = []
        for child in children:
            transform = TransformStamped()
            transform.header = msg.header
            transform.child_frame_id = child
            transform.transform.translation.x = xyz[0]
            transform.transform.translation.y = xyz[1]
            transform.transform.translation.z = xyz[2]
            transform.transform.rotation.x = quat[0]
            transform.transform.rotation.y = quat[1]
            transform.transform.rotation.z = quat[2]
            transform.transform.rotation.w = quat[3]
            stamped.append(transform)
        if source_id == "torso":
            twist = msg.twist.twist
            self._note_base_pose(time.time(), xyz, quat, (
                twist.linear.x, twist.linear.y, twist.linear.z,
                twist.angular.x, twist.angular.y, twist.angular.z,
            ))
            stamped.append(self._footprint_tf(msg.header, xyz, quat))
        self._tf.sendTransform(stamped)

    def _footprint_tf(self, header, xyz, quat) -> TransformStamped:
        """base_link projected onto z=0: same x, y, and yaw, with roll and pitch cleared."""
        yaw = math.atan2(
            2.0 * (quat[3] * quat[2] + quat[0] * quat[1]),
            1.0 - 2.0 * (quat[1] * quat[1] + quat[2] * quat[2]),
        )
        half = 0.5 * yaw
        out = TransformStamped()
        out.header = header
        out.child_frame_id = "base_footprint"
        out.transform.translation.x = xyz[0]
        out.transform.translation.y = xyz[1]
        out.transform.translation.z = 0.0
        out.transform.rotation.z = math.sin(half)
        out.transform.rotation.w = math.cos(half)
        return out

    def _relative_odom(self, position, quat):
        if self._odom_origin is None:
            ox, oy, oz = position
            qx, qy, qz, qw = quat
            self._odom_origin = (
                _rotate((-qx, -qy, -qz, qw), (-ox, -oy, -oz)),
                (-qx, -qy, -qz, qw),
            )
            self.get_logger().info(
                f"odom TF origin captured x={ox:.3f} y={oy:.3f} z={oz:.3f}"
            )
        return self._apply_origin(position, quat)

    def _apply_origin(self, position, quat):
        origin_t, origin_q = self._odom_origin
        rotated = _rotate(origin_q, position)
        return (
            origin_t[0] + rotated[0],
            origin_t[1] + rotated[1],
            origin_t[2] + rotated[2],
        ), _quat_mul(origin_q, quat)

    def _log_rates(self) -> None:
        now = time.monotonic()
        elapsed = now - self._stat_mono
        if elapsed < 5.0:
            return
        self.get_logger().info(
            "lidar %d pkt/s  %d pt/s | odom %d msg/s | deskew shift %.1f cm  %.2f deg"
            % (
                round(self._lidar_pkts / elapsed),
                round(self._lidar_pts / elapsed),
                round(self._odom_n / elapsed),
                self._deskew_dxy * 100.0,
                math.degrees(self._deskew_dyaw),
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
