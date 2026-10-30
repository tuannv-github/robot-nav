#!/usr/bin/env python3
"""Fit the floor in the map frame and print the odom z offset that puts it at z=0.

The map frame matches odom until RTAB-Map adds a correction. Each cloud is
transformed livox_frame -> base_link (roll, lidar_z) -> odom using /odom.
lidar_z already puts the floor at z=0 in base_link. The printed offset is
what g1_odom_tf subtracts from /odom z.

    source ros2_source.sh
    python3 src/g1_nav/utils/fit_ground.py
    python3 src/g1_nav/utils/fit_ground.py --lidar-z 1.252
"""

import argparse
import os

import numpy as np
import yaml
import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2

ITERS = 400
THRESH = 0.03
PARAMS_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'config', 'g1_rtabmap.yaml')
)


def config_mount():
    with open(PARAMS_FILE, encoding='utf-8') as handle:
        cfg = yaml.safe_load(handle)
    lidar = cfg['lidar']
    return float(lidar['z']), float(lidar['roll'])


def rotation_x(roll_deg):
    roll = np.deg2rad(roll_deg)
    c, s = np.cos(roll), np.sin(roll)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def quat_to_rot(x, y, z, w):
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def to_map(pts, rotation, lidar_z, odom_rot, odom_t):
    base = pts @ rotation.T + np.array([0.0, 0.0, lidar_z])
    return base @ odom_rot.T + odom_t


def fit_plane(pts):
    n = pts.shape[0]
    best_inl = None
    best_count = 0
    rng = np.random.default_rng(0)
    for _ in range(ITERS):
        sample = pts[rng.choice(n, 3, replace=False)]
        normal = np.cross(sample[1] - sample[0], sample[2] - sample[0])
        norm = np.linalg.norm(normal)
        if norm < 1e-6:
            continue
        normal = normal / norm
        if abs(normal[2]) < 0.9:
            continue
        if normal[2] < 0:
            normal = -normal
        dist = np.abs((pts - sample[0]) @ normal)
        inl = dist < THRESH
        count = int(inl.sum())
        if count > best_count:
            best_count = count
            best_inl = inl
    if best_inl is None:
        return None
    plane = pts[best_inl]
    centered = plane - plane.mean(axis=0)
    _, _, vh = np.linalg.svd(centered, full_matrices=False)
    normal = vh[-1]
    if normal[2] < 0:
        normal = -normal
    height = float(np.median(plane[:, 2]))
    return {
        "inliers": best_count,
        "total": n,
        "normal": normal,
        "height": height,
        "z_span": float(np.percentile(plane[:, 2], 95) - np.percentile(plane[:, 2], 5)),
    }


def collect(topic, odom_topic, clouds):
    rclpy.init()
    node = Node("fit_ground")
    qos = QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        history=HistoryPolicy.KEEP_LAST,
        depth=5,
    )
    latest = {"pose": None}
    chunks = []

    def on_odom(msg):
        q = msg.pose.pose.orientation
        p = msg.pose.pose.position
        latest["pose"] = (
            quat_to_rot(q.x, q.y, q.z, q.w),
            np.array([p.x, p.y, p.z]),
        )

    def on_cloud(msg):
        if len(chunks) >= clouds or latest["pose"] is None:
            return
        raw = np.frombuffer(msg.data, dtype=np.float32)
        width = msg.point_step // 4
        pts = raw.reshape(-1, width)[:, :3].astype(np.float64)
        finite = np.isfinite(pts).all(axis=1)
        pts = pts[finite]
        dist = np.linalg.norm(pts, axis=1)
        pts = pts[(dist > 0.4) & (dist < 15.0)]
        if pts.shape[0]:
            chunks.append((pts, latest["pose"]))
            print(
                f"cloud {len(chunks)} points={pts.shape[0]} frame={msg.header.frame_id} "
                f"odom_z={latest['pose'][1][2]:.3f}"
            )

    node.create_subscription(Odometry, odom_topic, on_odom, qos)
    node.create_subscription(PointCloud2, topic, on_cloud, qos)
    for _ in range(200):
        rclpy.spin_once(node, timeout_sec=0.1)
        if len(chunks) >= clouds:
            break
    node.destroy_node()
    rclpy.shutdown()
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="/livox/lidar")
    parser.add_argument("--odom-topic", default="/odom")
    lidar_z, lidar_roll = config_mount()
    parser.add_argument("--lidar-z", type=float, default=lidar_z)
    parser.add_argument("--lidar-roll", type=float, default=lidar_roll)
    parser.add_argument("--clouds", type=int, default=4)
    args = parser.parse_args()

    chunks = collect(args.topic, args.odom_topic, args.clouds)
    if not chunks:
        raise SystemExit(f"no clouds on {args.topic} with {args.odom_topic}")
    rotation = rotation_x(args.lidar_roll)
    parts = [to_map(pts, rotation, args.lidar_z, rot, trans) for pts, (rot, trans) in chunks]
    pts = np.concatenate(parts, axis=0)
    if pts.shape[0] > 80000:
        pts = pts[np.random.default_rng(1).choice(pts.shape[0], 80000, replace=False)]
    result = fit_plane(pts)
    if result is None:
        raise SystemExit("no horizontal plane")
    print(
        f"map frame inliers={result['inliers']}/{result['total']} "
        f"normal={np.round(result['normal'], 3).tolist()} "
        f"plane_z={result['height']:.4f} span={result['z_span']:.4f}"
    )
    print(
        f"set odom_z_offset: {result['height']:.4f} in src/g1_nav/config/g1_rtabmap.yaml"
        f"  (lidar.z stays {args.lidar_z:.4f})"
    )


if __name__ == "__main__":
    main()
