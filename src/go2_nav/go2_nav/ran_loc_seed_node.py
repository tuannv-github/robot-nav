#!/usr/bin/env python3
"""Seed RTAB-Map localization from ran-loc, then leave the pose to the lidar.

While RTAB-Map has not locked onto the map, the heaviest ran-loc fix is
published on ``/initialpose`` so the next scan is searched around that
map position. Ran-loc has no heading, so yaw is the robot's own facing
and the rotation search stays wide until the lidar locks. Lock and
re-seed follow the ICP scan-match result on ``/info``. After an accepted
ICP match, ran-loc is ignored. If ICP keeps rejecting the scan while the
robot is moving, the initial pose is set from ran-loc again.
"""

from __future__ import annotations

import math
import random

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from go2_controller_msgs.msg import RanLoc
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from rclpy.node import Node
from rtabmap_msgs.msg import Info
from tf2_ros import Buffer, TransformException, TransformListener


def _quat_from_yaw(yaw: float) -> tuple[float, float, float, float]:
    half = 0.5 * float(yaw)
    return 0.0, 0.0, math.sin(half), math.cos(half)


def _stat(info: Info, key: str) -> float | None:
    try:
        index = list(info.stats_keys).index(key)
    except ValueError:
        return None
    return float(info.stats_values[index])


def _stat_any(info: Info, *keys: str) -> float | None:
    for key in keys:
        value = _stat(info, key)
        if value is not None:
            return value
    return None


class RanLocSeed(Node):
    """Ran-loc guess until ICP accepts the scan, then lidar."""

    def __init__(self) -> None:
        super().__init__('ran_loc_seed')
        self.declare_parameter('ran_loc_topic', '/ran_loc')
        self.declare_parameter('initialpose_topic', '/initialpose')
        self.declare_parameter('info_topic', '/info')
        self.declare_parameter('lost_hold_sec', 5.0)
        # Ran-loc's top place jumps by several metres. A pose inside this
        # distance of a real candidate is already close enough to keep.
        self.declare_parameter('disagree_m', 10.0)
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('min_weight', 0.2)
        self.declare_parameter('retry_period_sec', 3.0)
        self.declare_parameter('icp_rms_max', 0.20)
        self.declare_parameter('icp_inliers_ratio_min', 0.20)
        # Ran-loc has no heading. Rejected seeds walk the full circle at this step.
        self.declare_parameter('yaw_step_deg', 15.0)
        self.declare_parameter('rtabmap_node', '/rtabmap')

        self._retry_period = float(self.get_parameter('retry_period_sec').value)
        self._rms_max = float(self.get_parameter('icp_rms_max').value)
        self._ratio_min = float(self.get_parameter('icp_inliers_ratio_min').value)
        self._yaw_step_deg = float(self.get_parameter('yaw_step_deg').value)
        self._yaw_steps = max(1, int(round(360.0 / self._yaw_step_deg)))
        self._lost_hold = float(self.get_parameter('lost_hold_sec').value)
        self._disagree_m = float(self.get_parameter('disagree_m').value)
        self._map_frame = str(self.get_parameter('map_frame').value)
        self._base_frame = str(self.get_parameter('base_frame').value)
        self._odom_frame = str(self.get_parameter('odom_frame').value)
        self._yaw_step = 0
        self._candidates: list[dict] = []
        self._locked = False
        self._accepts = 0
        self._confirmed = False
        self._force_seed = False
        self._bad_since: float | None = None
        self._gap_since: float | None = None
        self._last_seed = 0.0
        self._awaiting_seed = False
        self._infos_after_seed = 0
        self._search_widened = False

        self._pose_pub = self.create_publisher(
            PoseWithCovarianceStamped,
            str(self.get_parameter('initialpose_topic').value),
            10,
        )
        self.create_subscription(
            RanLoc,
            str(self.get_parameter('ran_loc_topic').value),
            self._on_ran_loc,
            10,
        )
        self.create_subscription(
            Info,
            str(self.get_parameter('info_topic').value),
            self._on_info,
            10,
        )
        node_name = str(self.get_parameter('rtabmap_node').value).rstrip('/')
        self._param_client = self.create_client(SetParameters, node_name + '/set_parameters')
        self._tf = Buffer()
        self._tf_listener = TransformListener(self._tf, self)
        self.create_timer(0.5, self._tick)
        self.get_logger().info(
            'ran-loc seeds once, then waits for RTAB-Map /info before seeding again; '
            f'a rejected match for {self._lost_hold:.0f} s sets the initial pose again'
        )

    def _on_ran_loc(self, msg: RanLoc) -> None:
        candidates = []
        for loc in sorted(msg.locations, key=lambda item: item.weight, reverse=True):
            candidates.append({
                'seq': int(msg.seq),
                'x': float(loc.x),
                'y': float(loc.y),
                'z': float(loc.z),
                'accuracy_m': float(loc.accuracy_m),
                'weight': float(loc.weight),
            })
        if not candidates:
            return
        self._candidates = candidates
        self._watch_gap()

    def _sample_fix(self) -> dict | None:
        """Draw one ran-loc fix. Probability is proportional to its weight."""
        if not self._candidates:
            return None
        weights = [max(float(fix['weight']), 0.0) for fix in self._candidates]
        total = sum(weights)
        if total <= 0.0:
            return self._candidates[0]
        draw = random.random() * total
        cumulative = 0.0
        for fix, weight in zip(self._candidates, weights):
            cumulative += weight
            if draw <= cumulative:
                return fix
        return self._candidates[-1]

    def _advance_search(self) -> None:
        """A rejected seed tries a new heading. The next location is drawn by weight."""
        self._yaw_step += 1

    def _on_info(self, info: Info) -> None:
        if self._awaiting_seed:
            self._infos_after_seed += 1
            # The update already running when /initialpose was sent did not use it.
            if self._infos_after_seed < 2:
                return
            score = self._icp_score(info)
            if score is None:
                return
            self._awaiting_seed = False
            if score >= self._ratio_min and self._rms_ok(info):
                self._lock(info, score)
                return
            self._last_seed = 0.0
            self._advance_search()
            self.get_logger().info(
                f'RTAB rejected the seed '
                f'(score={score:.2f} proximity={info.proximity_detection_id} '
                f'loop={info.loop_closure_id})'
            )
            return
        score = self._icp_score(info)
        if score is None:
            return
        # A match that is already in place is the pose to keep. Do not seed over it.
        if not self._locked:
            if score >= self._ratio_min and self._rms_ok(info) and self._last_seed == 0.0:
                self._lock(info, score)
            return
        if score >= self._ratio_min and self._rms_ok(info):
            self._lock(info, score)
            return
        # A later frame with no new match is normal once the pose is right.
        if self._confirmed:
            return
        if not self._locked:
            return
        now = self.get_clock().now().nanoseconds * 1e-9
        if self._bad_since is None:
            self._bad_since = now
            return
        if now - self._bad_since < self._lost_hold:
            return
        self._locked = False
        self._bad_since = None
        self._last_seed = 0.0
        self.get_logger().warn(
            f'ICP match score {score:.2f} stayed below {self._ratio_min:.2f}; '
            'setting the initial pose again'
        )

    def _lock(self, info: Info, score: float) -> None:
        if self._confirmed:
            self._bad_since = None
            self._awaiting_seed = False
            return
        self._locked = True
        self._confirmed = True
        self._accepts += 1
        self._bad_since = None
        self._awaiting_seed = False
        self._force_seed = False
        self._set_search_window(wide=False)
        self.get_logger().info(
            f'ICP match accepted '
            f'(score={score:.2f} proximity={info.proximity_detection_id} '
            f'loop={info.loop_closure_id}); ran-loc will not move the pose'
        )

    def _icp_score(self, info: Info) -> float | None:
        """ICP correspondence score, or None when this frame did not run ICP.

        RTAB-Map 0.23 does not publish the proximity inlier ratio. An accepted
        scan match scores 1. A published ratio, when one is present, replaces
        that. A rejected match while the robot is moving scores 0.
        """
        matched = info.proximity_detection_id > 0 or info.loop_closure_id > 0
        ratio = _stat_any(
            info,
            'NeighborLinkRefining/ICP_inliers_ratio/',
            'Icp/InliersRatio',
            'Icp/CorrespondenceRatio',
        )
        if ratio is not None and matched:
            return ratio
        if matched:
            return 1.0
        # A still frame does not run ICP, so it is not a verdict on the seed.
        if _stat_any(info, 'Memory/Small_movement/', 'Memory/Small_movement') == 1.0:
            return None
        return 0.0

    def _rms_ok(self, info: Info) -> bool:
        rms = _stat_any(info, 'Icp/RMS', 'Icp/RMS_error')
        return rms is None or rms <= self._rms_max

    def _tick(self) -> None:
        self._watch_gap()
        if self._hold_if_close():
            return
        fix = self._sample_fix()
        if self._locked or fix is None:
            return
        now = self.get_clock().now().nanoseconds * 1e-9
        if self._awaiting_seed:
            return
        if self._last_seed and now - self._last_seed < self._retry_period:
            return
        heading = self._seed_yaw()
        if heading is None:
            self.get_logger().warn(
                'ran-loc has no heading and the robot yaw is not in TF yet',
                throttle_duration_sec=5.0,
            )
            return
        yaw, yaw_frame, offset = heading
        self._set_search_window(wide=True)
        self._publish_seed(fix, yaw, yaw_frame, offset)
        self._last_seed = now
        self._force_seed = False

    def _frame_yaw(self, parent: str) -> float | None:
        try:
            tf = self._tf.lookup_transform(parent, self._base_frame, rclpy.time.Time())
        except TransformException:
            return None
        rotation = tf.transform.rotation
        return 2.0 * math.atan2(rotation.z, rotation.w)

    def _seed_yaw(self) -> tuple[float, str, float] | None:
        """Heading to try. Ran-loc has none, and the map yaw is the one we last wrote.

        The base is the odom facing. Each rejected seed turns by ``yaw_step_deg``
        until the full circle has been tried.
        """
        base = self._frame_yaw(self._odom_frame)
        frame = self._odom_frame
        if base is None:
            base = self._frame_yaw(self._map_frame)
            frame = self._map_frame
        if base is None:
            return None
        offset = (self._yaw_step % self._yaw_steps) * (2.0 * math.pi / self._yaw_steps)
        yaw = math.atan2(math.sin(base + offset), math.cos(base + offset))
        return yaw, frame, offset

    def _map_xy(self) -> tuple[float, float] | None:
        try:
            tf = self._tf.lookup_transform(self._map_frame, self._base_frame, rclpy.time.Time())
        except TransformException:
            return None
        translation = tf.transform.translation
        return float(translation.x), float(translation.y)

    def _heaviest(self) -> dict | None:
        if not self._candidates:
            return None
        return max(self._candidates, key=lambda fix: float(fix['weight']))

    def _gap_to_ran_loc(self) -> float | None:
        """Distance from the map pose to the nearest ran-loc place with real weight.

        The heaviest place jumps between modes. A lighter real place next to a
        correct pose still counts. A near-zero speck does not.
        """
        xy = self._map_xy()
        if xy is None or not self._candidates:
            return None
        heaviest = max(float(fix['weight']) for fix in self._candidates)
        floor = max(0.05, 0.10 * heaviest)
        considered = [fix for fix in self._candidates if float(fix['weight']) >= floor]
        if not considered:
            considered = [self._heaviest()]
        return min(
            math.hypot(xy[0] - fix['x'], xy[1] - fix['y'])
            for fix in considered
            if fix is not None
        )

    def _hold_if_close(self) -> bool:
        """Keep a pose that is already near ran-loc. Do not seed it away."""
        if self._force_seed:
            return False
        if self._locked or self._awaiting_seed:
            return self._locked
        gap = self._gap_to_ran_loc()
        # The map pose is not known yet. Wait instead of publishing a guess.
        if gap is None:
            return True
        if gap > self._disagree_m:
            return False
        self._locked = True
        self._confirmed = True
        self._bad_since = None
        self._gap_since = None
        self.get_logger().info(
            f'map pose is {gap:.1f} m from ran-loc (limit {self._disagree_m:.1f} m); '
            'keeping the current pose'
        )
        return True

    def _watch_gap(self) -> None:
        """Clear a lock only before the pose has been accepted.

        Once ICP accepts, or the pose is already near ran-loc, later frames
        often have no new match and the heaviest ran-loc place keeps moving.
        Neither of those is a reason to publish another initial pose.
        """
        if not self._locked or self._confirmed:
            self._gap_since = None
            return
        gap = self._gap_to_ran_loc()
        if gap is None or gap <= self._disagree_m:
            self._gap_since = None
            return
        now = self.get_clock().now().nanoseconds * 1e-9
        if self._gap_since is None:
            self._gap_since = now
            return
        if now - self._gap_since < self._lost_hold:
            return
        self._locked = False
        self._gap_since = None
        self._bad_since = None
        self._last_seed = 0.0
        self.get_logger().warn(
            f'map pose is {gap:.1f} m from ran-loc (limit {self._disagree_m:.1f} m); '
            'setting the initial pose again'
        )

    def _publish_seed(self, fix: dict, yaw: float, yaw_frame: str, offset: float) -> None:
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self._map_frame
        msg.pose.pose.position.x = fix['x']
        msg.pose.pose.position.y = fix['y']
        msg.pose.pose.position.z = fix['z']
        qx, qy, qz, qw = _quat_from_yaw(yaw)
        msg.pose.pose.orientation.x = qx
        msg.pose.pose.orientation.y = qy
        msg.pose.pose.orientation.z = qz
        msg.pose.pose.orientation.w = qw
        variance = max(fix['accuracy_m'], 0.5) ** 2
        msg.pose.covariance[0] = variance
        msg.pose.covariance[7] = variance
        msg.pose.covariance[14] = 0.05
        # Ran-loc did not measure yaw. A half-turn of variance marks it unknown.
        msg.pose.covariance[35] = math.pi ** 2
        self._pose_pub.publish(msg)
        self._awaiting_seed = True
        self._infos_after_seed = 0
        self.get_logger().info(
            f'set initial pose seq={fix["seq"]} '
            f'x={fix["x"]:.2f} y={fix["y"]:.2f} z={fix["z"]:.2f} '
            f'yaw={yaw:.3f} ({yaw_frame} {math.degrees(offset):+.0f} deg) '
            f'accuracy={fix["accuracy_m"]:.2f} m xy_variance={variance:.2f} '
            f'weight={fix["weight"]:.2f} of {len(self._candidates)}'
        )

    def _set_search_window(self, wide: bool) -> None:
        if wide == self._search_widened:
            return
        if not self._param_client.service_is_ready():
            return
        # Ran-loc can miss by 10 m and has no heading, so the first match
        # searches that whole region. After ICP accepts, the tracker tightens.
        if wide:
            values = {
                'Icp/Iterations': '30',
                'Icp/MaxCorrespondenceDistance': '10.0',
                'Icp/MaxTranslation': '10.0',
                'Icp/MaxRotation': str(math.pi),
                'RGBD/ProximityPathFilteringRadius': '10.0',
                'RGBD/ProximityMaxPaths': '8',
                'RGBD/ProximityAngle': '180',
                'RGBD/MaxLocalRetrieved': '8',
            }
        else:
            values = {
                'Icp/Iterations': '15',
                'Icp/MaxCorrespondenceDistance': '1.5',
                'Icp/MaxTranslation': '2.0',
                'Icp/MaxRotation': '1.2',
                'RGBD/ProximityPathFilteringRadius': '4.0',
                'RGBD/ProximityMaxPaths': '3',
                'RGBD/ProximityAngle': '90',
                'RGBD/MaxLocalRetrieved': '2',
            }
        request = SetParameters.Request()
        for name, value in values.items():
            param = Parameter()
            param.name = name
            param.value = ParameterValue(type=ParameterType.PARAMETER_STRING, string_value=str(value))
            request.parameters.append(param)
        self._param_client.call_async(request)
        self._search_widened = wide


def main() -> None:
    rclpy.init()
    node = RanLocSeed()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
