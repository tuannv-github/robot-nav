#!/usr/bin/env python3
"""Copy TF ``odom`` -> ``base_link`` from the eth0 Cyclone bus onto loopback.

``utlidar_odom`` publishes that transform on eth0 only. ``rviz2-local.sh``
uses ``cyclonedds.local.xml`` (lo only), so it never sees it. FastDDS
``odom_ext_relay`` is a different RMW and is invisible to Cyclone subscribers.

Two processes, one named pipe: an eth0 subscriber must not share a
participant with the loopback publisher.
"""

from __future__ import annotations

import fcntl

import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message, serialize_message
from tf2_msgs.msg import TFMessage
from tf2_ros import TransformBroadcaster

from odom.fifo_ipc import FifoReader, FifoWriter

_TF_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
    history=HistoryPolicy.KEEP_LAST,
    depth=100,
)


def _lock(path: str) -> object:
    """Block until this process owns the relay. A second copy waits instead of publishing twice."""
    handle = open(path, 'w')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


class EthTfTap(Node):
    def __init__(self) -> None:
        super().__init__('odom_tf_eth_tap')
        self.declare_parameter('relay_pipe', '/tmp/go2_tf_lo.fifo')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')
        pipe = self.get_parameter('relay_pipe').get_parameter_value().string_value
        self._parent = self.get_parameter('odom_frame').get_parameter_value().string_value
        self._child = self.get_parameter('base_frame').get_parameter_value().string_value
        self._fifo = FifoWriter(pipe)
        self._n = 0
        self.create_subscription(TFMessage, '/tf', self._cb, _TF_QOS)
        self.get_logger().info(
            f'eth0 /tf {self._parent} -> {self._child} tap -> {pipe}'
        )

    def _cb(self, msg: TFMessage) -> None:
        for transform in msg.transforms:
            if transform.header.frame_id != self._parent:
                continue
            if transform.child_frame_id != self._child:
                continue
            try:
                ok = self._fifo.write(serialize_message(transform))
            except OSError:
                return
            if not ok:
                return
            self._n += 1
            if self._n == 1 or self._n % 200 == 0:
                self.get_logger().info(f'forwarded {self._parent}->{self._child} n={self._n}')

    def destroy_node(self) -> bool:
        self._fifo.close()
        return super().destroy_node()


class LoTfPub(Node):
    def __init__(self) -> None:
        super().__init__('odom_tf_lo')
        self.declare_parameter('relay_pipe', '/tmp/go2_tf_lo.fifo')
        pipe = self.get_parameter('relay_pipe').get_parameter_value().string_value
        self._fifo = FifoReader(pipe)
        self._tf = TransformBroadcaster(self)
        self._n = 0
        self.create_timer(0.005, self._poll)
        self.get_logger().info(f'loopback TF from {pipe}')

    def _poll(self) -> None:
        try:
            frames = self._fifo.read_messages()
        except OSError as exc:
            self.get_logger().warn(f'tf fifo: {exc}')
            return
        for data in frames:
            try:
                transform = deserialize_message(data, TransformStamped)
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warn(f'tf deserialize: {exc}')
                continue
            self._tf.sendTransform(transform)
            self._n += 1
            if self._n == 1 or self._n % 200 == 0:
                self.get_logger().info(
                    f'published {transform.header.frame_id}->{transform.child_frame_id} n={self._n}'
                )

    def destroy_node(self) -> bool:
        self._fifo.close()
        return super().destroy_node()


def _spin(node: Node, lock) -> None:
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        lock.close()


def tap_main(args=None) -> None:
    lock = _lock('/tmp/go2_tf_lo_tap.lock')
    rclpy.init(args=args)
    _spin(EthTfTap(), lock)


def pub_main(args=None) -> None:
    lock = _lock('/tmp/go2_tf_lo_pub.lock')
    rclpy.init(args=args)
    _spin(LoTfPub(), lock)
