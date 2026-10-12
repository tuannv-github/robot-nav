#!/usr/bin/env python3
"""Subscribe to Unitree ``/utlidar/robot_odom`` and publish ROS default ``/odom``.

Also broadcasts TF ``odom`` -> ``base_link`` (Go2 does not publish this TF).
First pose is the local origin (same idea as ``scripts/utlidar_odom.py``).
Touch ``/tmp/go2_odom.reset`` or call ``/odom_ext_relay/reset`` to re-zero.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import serialize_message
from tf2_ros import TransformBroadcaster

from odom.fifo_ipc import FifoWriter

# Match Unitree writer exactly (RELIABLE / KEEP_LAST 1). Depth 10 can stall the stream.
_SUB_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)
_PUB_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
)


def yaw_from_quat(q) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def quat_from_yaw(yaw: float) -> tuple[float, float, float, float]:
    half = 0.5 * yaw
    return 0.0, 0.0, math.sin(half), math.cos(half)


def wrap_pi(rad: float) -> float:
    return (rad + math.pi) % (2.0 * math.pi) - math.pi


class UtlidarOdom(Node):
    def __init__(self) -> None:
        super().__init__('utlidar_odom')

        self.declare_parameter('input_topic', '/utlidar/robot_odom')
        self.declare_parameter('output_topic', '/odom')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('zero_at_start', True)
        self.declare_parameter('publish_tf', True)
        self.declare_parameter('use_ros_time', True)
        self.declare_parameter('relay_pipe', '/tmp/go2_odom.fifo')
        self.declare_parameter('reset_flag', '/tmp/go2_odom.reset')
        # Lidar odom arrives at several hundred Hz. One 20 Hz publish feeds every
        # TF listener; the fifos carry that same sample to the other buses.
        self.declare_parameter('output_rate_hz', 20.0)
        self.declare_parameter('tf_lo_pipe', '/tmp/go2_tf_lo.fifo')

        self.input_topic = self.get_parameter('input_topic').get_parameter_value().string_value
        self.output_topic = self.get_parameter('output_topic').get_parameter_value().string_value
        self.odom_frame = self.get_parameter('odom_frame').get_parameter_value().string_value
        self.base_frame = self.get_parameter('base_frame').get_parameter_value().string_value
        self.zero_at_start = self.get_parameter('zero_at_start').get_parameter_value().bool_value
        self.publish_tf = self.get_parameter('publish_tf').get_parameter_value().bool_value
        self.use_ros_time = self.get_parameter('use_ros_time').get_parameter_value().bool_value
        relay_pipe = self.get_parameter('relay_pipe').get_parameter_value().string_value
        tf_lo_pipe = self.get_parameter('tf_lo_pipe').get_parameter_value().string_value
        reset_flag = self.get_parameter('reset_flag').get_parameter_value().string_value
        output_hz = self.get_parameter('output_rate_hz').get_parameter_value().double_value
        self._output_hz = output_hz
        self._output_period = 0.0 if output_hz <= 0.0 else 1.0 / output_hz
        self._last_output = 0.0

        self._origin: tuple[float, float, float, float] | None = None
        self._tf = TransformBroadcaster(self) if self.publish_tf else None
        self._fifo = FifoWriter(relay_pipe)
        self._tf_lo_fifo = FifoWriter(tf_lo_pipe) if self.publish_tf else None
        self._reset_flag = Path(reset_flag)
        try:
            self._reset_flag.unlink(missing_ok=True)
        except OSError:
            pass

        self._n = 0
        self._published = 0
        self._pub = self.create_publisher(Odometry, self.output_topic, _PUB_QOS)
        self.create_subscription(Odometry, self.input_topic, self._cb, _SUB_QOS)
        self.create_timer(0.05, self._poll_reset_flag)

        self.get_logger().info(
            f'{self.input_topic} -> {self.output_topic} '
            f'(frames {self.odom_frame} -> {self.base_frame}, '
            f'zero_at_start={self.zero_at_start}, publish_tf={self.publish_tf}, '
            f'use_ros_time={self.use_ros_time}, relay fifo {relay_pipe}, '
            f'loopback tf fifo {tf_lo_pipe}, output_rate_hz={output_hz})'
        )

    def _pose_xyzyaw(self, msg: Odometry) -> tuple[float, float, float, float]:
        p = msg.pose.pose.position
        return (p.x, p.y, p.z, yaw_from_quat(msg.pose.pose.orientation))

    def _clear_origin(self, via: str) -> None:
        self._origin = None
        self.get_logger().info(
            f'origin cleared ({via}); next /utlidar/robot_odom sample becomes origin'
        )

    def _poll_reset_flag(self) -> None:
        if not self._reset_flag.exists():
            return
        try:
            self._reset_flag.unlink()
        except OSError:
            return
        self._clear_origin(f'flag {self._reset_flag}')

    def _relative(self, msg: Odometry) -> Odometry:
        x, y, z, yaw = self._pose_xyzyaw(msg)
        if self._origin is None:
            self._origin = (x, y, z, yaw)
            self.get_logger().info(
                f'origin set x={x:.3f} y={y:.3f} z={z:.3f} yaw={math.degrees(yaw):.1f} deg'
            )
        ox, oy, oz, oyaw = self._origin
        c = math.cos(-oyaw)
        s = math.sin(-oyaw)
        dx, dy = x - ox, y - oy

        out = Odometry()
        out.header.stamp = msg.header.stamp
        out.header.frame_id = self.odom_frame
        out.child_frame_id = self.base_frame
        out.pose.pose.position.x = c * dx - s * dy
        out.pose.pose.position.y = s * dx + c * dy
        out.pose.pose.position.z = z - oz
        qx, qy, qz, qw = quat_from_yaw(wrap_pi(yaw - oyaw))
        out.pose.pose.orientation.x = qx
        out.pose.pose.orientation.y = qy
        out.pose.pose.orientation.z = qz
        out.pose.pose.orientation.w = qw
        out.pose.covariance = msg.pose.covariance
        out.twist = msg.twist
        return out

    def _passthrough(self, msg: Odometry) -> Odometry:
        out = Odometry()
        out.header.stamp = msg.header.stamp
        out.header.frame_id = self.odom_frame
        out.child_frame_id = self.base_frame
        out.pose = msg.pose
        out.twist = msg.twist
        return out

    def _cb(self, msg: Odometry) -> None:
        self._n += 1
        out = self._relative(msg) if self.zero_at_start else self._passthrough(msg)
        # Unitree lidar stamps lag the NX/camera clock (~minutes); TF lookup then fails.
        if self.use_ros_time:
            out.header.stamp = self.get_clock().now().to_msg()
        now = time.monotonic()
        if self._output_period > 0.0 and (now - self._last_output) < self._output_period:
            return
        self._last_output = now
        self._pub.publish(out)
        try:
            self._fifo.write(serialize_message(out))
        except OSError:
            pass
        self._published += 1
        if self._published == 1 or self._published % 40 == 0:
            self.get_logger().info(
                f'published /odom at {self._output_hz:.0f} Hz '
                f'n={self._published} from {self._n} lidar samples'
            )
        if self._tf is None:
            return
        t = TransformStamped()
        t.header = out.header
        t.child_frame_id = out.child_frame_id
        t.transform.translation.x = out.pose.pose.position.x
        t.transform.translation.y = out.pose.pose.position.y
        t.transform.translation.z = out.pose.pose.position.z
        t.transform.rotation = out.pose.pose.orientation
        self._tf.sendTransform(t)
        if self._tf_lo_fifo is not None:
            try:
                self._tf_lo_fifo.write(serialize_message(t))
            except OSError:
                pass

    def destroy_node(self) -> bool:
        self._fifo.close()
        if self._tf_lo_fifo is not None:
            self._tf_lo_fifo.close()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = UtlidarOdom()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
