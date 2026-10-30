#!/usr/bin/env python3
"""Publish odom -> base_link from a nav_msgs/Odometry topic.

robot-bridge already broadcasts that transform. Do not run this node
while the bridge is publishing it: two publishers of odom -> base_link fight.
RTAB-Map reads that pose from TF when odom_frame_id is set.

Sport /odom is an absolute pelvis pose, not a pose that starts at the
origin. With zero_start, the first message is the origin, so the robot
begins at x=0, y=0, z=0, yaw=0 and later messages are motion relative to it.
"""

import rclpy
from geometry_msgs.msg import Quaternion, TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from tf2_ros import TransformBroadcaster


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
    # q * (v, 0) * q^{-1}, with q assumed unit.
    ix = w * vx + y * vz - z * vy
    iy = w * vy + z * vx - x * vz
    iz = w * vz + x * vy - y * vx
    iw = -x * vx - y * vy - z * vz
    return (
        iw * -x + ix * w + iy * -z - iz * -y,
        iw * -y + iy * w + iz * -x - ix * -z,
        iw * -z + iz * w + ix * -y - iy * -x,
    )


class OdomTf(Node):
    def __init__(self):
        super().__init__('g1_odom_tf')
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('z_offset', 0.0)
        self.declare_parameter('zero_start', True)
        topic = self.get_parameter('odom_topic').get_parameter_value().string_value
        self._z_offset = self.get_parameter('z_offset').get_parameter_value().double_value
        self._zero_start = self.get_parameter('zero_start').get_parameter_value().bool_value
        self._origin = None
        self._tf = TransformBroadcaster(self)
        self.create_subscription(Odometry, topic, self._on_odom, 20)
        self.get_logger().info(
            f'{topic} -> TF, zero_start={self._zero_start} z_offset={self._z_offset:.3f}'
        )

    def _relative(self, position, quat):
        if self._origin is None:
            ox, oy, oz = position
            qx, qy, qz, qw = quat
            # Inverse of the first pose.
            self._origin = (
                _rotate((-qx, -qy, -qz, qw), (-ox, -oy, -oz)),
                (-qx, -qy, -qz, qw),
            )
            self.get_logger().info(
                f'origin captured x={ox:.3f} y={oy:.3f} z={oz:.3f}'
            )
        ot, oq = self._origin
        rotated = _rotate(oq, position)
        return (
            ot[0] + rotated[0],
            ot[1] + rotated[1],
            ot[2] + rotated[2],
        ), _quat_mul(oq, quat)

    def _on_odom(self, msg: Odometry) -> None:
        position = msg.pose.pose.position
        quat = msg.pose.pose.orientation
        xyz = (position.x, position.y, position.z)
        q = (quat.x, quat.y, quat.z, quat.w)
        if self._zero_start:
            xyz, q = self._relative(xyz, q)
        else:
            xyz = (xyz[0], xyz[1], xyz[2] - self._z_offset)
        transform = TransformStamped()
        transform.header = msg.header
        transform.child_frame_id = msg.child_frame_id or 'base_link'
        transform.transform.translation.x = xyz[0]
        transform.transform.translation.y = xyz[1]
        transform.transform.translation.z = xyz[2]
        transform.transform.rotation = Quaternion(x=q[0], y=q[1], z=q[2], w=q[3])
        self._tf.sendTransform(transform)


def main():
    rclpy.init()
    node = OdomTf()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
