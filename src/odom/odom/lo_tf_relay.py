#!/usr/bin/env python3
"""Publish TF ``odom`` -> ``base_link`` on loopback from the 20 Hz fifo.

``utlidar_odom`` is the only downsample (lidar rate -> 20 Hz) and writes this
pipe. Loopback RViz cannot see the eth0 publisher, and this process must not
join eth0.
"""

from __future__ import annotations

import fcntl

import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.serialization import deserialize_message
from tf2_ros import TransformBroadcaster

from odom.fifo_ipc import FifoReader


def _lock(path: str) -> object:
    """Block until this process owns the relay. A second copy waits instead of publishing twice."""
    handle = open(path, 'w')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


class LoTfPub(Node):
    def __init__(self) -> None:
        super().__init__('odom_tf_lo')
        self.declare_parameter('relay_pipe', '/tmp/go2_tf_lo.fifo')
        pipe = self.get_parameter('relay_pipe').get_parameter_value().string_value
        self._fifo = FifoReader(pipe)
        self._tf = TransformBroadcaster(self)
        self._n = 0
        self.create_timer(0.05, self._poll)
        self.get_logger().info(f'loopback TF from {pipe}')

    def _poll(self) -> None:
        try:
            frames = self._fifo.read_messages()
        except OSError as exc:
            self.get_logger().warn(f'tf fifo: {exc}')
            return
        if not frames:
            return
        try:
            transform = deserialize_message(frames[-1], TransformStamped)
        except Exception as exc:  # noqa: BLE001
            self.get_logger().warn(f'tf deserialize: {exc}')
            return
        self._tf.sendTransform(transform)
        self._n += 1
        if self._n == 1 or self._n % 40 == 0:
            self.get_logger().info(
                f'published {transform.header.frame_id}->{transform.child_frame_id} n={self._n}'
            )

    def destroy_node(self) -> bool:
        self._fifo.close()
        return super().destroy_node()


def pub_main(args=None) -> None:
    lock = _lock('/tmp/go2_tf_lo_pub.lock')
    rclpy.init(args=args)
    try:
        node = LoTfPub()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if 'node' in locals():
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        lock.close()
