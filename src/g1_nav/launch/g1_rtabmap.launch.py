#!/usr/bin/env python3
"""
RTAB-Map SLAM for the Unitree G1.

Tunable values live in ``config/g1_rtabmap.yaml`` next to this package.
Edit that file and relaunch. ``lidar_only``, ``localization``,
``database_path``, and ``use_sim_time`` can still be set on the command line.

The Mid-360 cloud is the default. Pass ``lidar_only:=false`` to also use the camera.

Prerequisites::

    # robot-bridge publishes /livox/lidar and odom -> base_link (z held at 0).
    ros2 launch g1_nav g1_rtabmap.launch.py

    # keep an existing map
    ros2 launch g1_nav g1_rtabmap.launch.py localization:=true database_path:=/path/g1_rtabmap.db
"""

import math
import os

import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

SYNC_KEYS = (
    'frame_id',
    'guess_frame_id',
    'odom_frame_id',
    'Reg/Force3DoF',
    'approx_sync',
    'sync_queue_size',
    'topic_queue_size',
    'use_action_for_goal',
    'wait_imu_to_init',
    'wait_for_transform',
    'Grid/DepthDecimation',
    'Grid/RangeMax',
    'Grid/RayTracing',
    'GridGlobal/ProbMiss',
    'GridGlobal/ProbHit',
    'GridGlobal/OccupancyThr',
    'GridGlobal/MinSize',
    'Grid/MinClusterSize',
    'Grid/3D',
    'Grid/MapFrameProjection',
    'Grid/NormalsSegmentation',
    'Grid/MinGroundHeight',
    'Grid/MaxGroundHeight',
    'Grid/MaxObstacleHeight',
    'Grid/RangeMin',
    'Grid/FootprintLength',
    'Grid/FootprintWidth',
    'Grid/FootprintHeight',
    'Odom/ResetCountdown',
    'Kp/RoiRatios',
)


def get_workspace_root():
    """Directory that contains ``src``, else three levels above this file."""
    launch_file_path = os.path.abspath(__file__)
    current_dir = os.path.dirname(launch_file_path)
    for _ in range(10):
        if os.path.exists(os.path.join(current_dir, 'src')):
            return current_dir
        parent = os.path.dirname(current_dir)
        if parent == current_dir:
            break
        current_dir = parent
    launch_file_dir = os.path.dirname(launch_file_path)
    return os.path.dirname(os.path.dirname(os.path.dirname(launch_file_dir)))


def default_params_file():
    launch_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.normpath(os.path.join(launch_dir, '..', 'config', 'g1_rtabmap.yaml'))


def _load_params(path):
    with open(path, encoding='utf-8') as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f'expected a mapping in {path}')
    return data


def _quat_from_rpy_deg(roll_deg, pitch_deg, yaw_deg):
    """ZYX quaternion, matching realsense.launch.py."""
    roll = math.radians(float(roll_deg))
    pitch = math.radians(float(pitch_deg))
    yaw = math.radians(float(yaw_deg))
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    qw = cr * cp * cy + sr * sp * sy
    qx = sr * cp * cy - cr * sp * sy
    qy = cr * sp * cy + sr * cp * sy
    qz = cr * cp * sy - sr * sp * cy
    return qx, qy, qz, qw


def _static_tf(name, parent, child, xyz, rpy_deg):
    qx, qy, qz, qw = _quat_from_rpy_deg(*rpy_deg)
    x, y, z = xyz
    return Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name=name,
        arguments=[
            str(x), str(y), str(z),
            str(qx), str(qy), str(qz), str(qw),
            parent, child,
        ],
    )


def _pose(section):
    return (
        (section['x'], section['y'], section['z']),
        (section['roll'], section['pitch'], section['yaw']),
    )


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() == 'true'


def launch_setup(context, *args, **kwargs):
    params_path = LaunchConfiguration('params_file').perform(context) or default_params_file()
    cfg = _load_params(params_path)
    print(f'[g1_rtabmap] Parameters: {params_path}')

    localization = LaunchConfiguration('localization')
    use_sim_time = LaunchConfiguration('use_sim_time')
    cpu = LaunchConfiguration('rtab_cpu_affinity').perform(context) or str(cfg.get('rtab_cpu_affinity') or '')
    rtab_prefix = '' if cpu == '' or cpu.lower() == 'all' else 'taskset -c ' + cpu
    # RTAB-Map aborts when SQLite returns "database is locked". This makes that write wait.
    busy_lib = os.path.join(get_workspace_root(), 'utils', 'libsqlite_busy_timeout.so')
    slam_prefix = f'env LD_PRELOAD={busy_lib}'
    if rtab_prefix:
        slam_prefix = f'{rtab_prefix} {slam_prefix}'

    lidar_only_arg = LaunchConfiguration('lidar_only').perform(context)
    lidar_only = _as_bool(cfg['lidar_only'] if lidar_only_arg == '' else lidar_only_arg)
    enable_livox_cloud = lidar_only or _as_bool(cfg['enable_livox_cloud'])
    odom_topic = cfg['odom_topic']
    scan_cloud_topic = cfg['scan_cloud_topic']

    provided_db_path = LaunchConfiguration('database_path').perform(context)
    database_exists = False
    if provided_db_path:
        database_path = provided_db_path
        os.makedirs(os.path.dirname(os.path.abspath(database_path)) or '.', exist_ok=True)
        database_exists = os.path.exists(database_path)
        if database_exists:
            print(f"[g1_rtabmap] Using RTAB-Map database: {database_path}")
        else:
            print(f"[g1_rtabmap] Database path does not exist, will create: {database_path}")
    else:
        database_path = os.path.join(get_workspace_root(), 'map', 'g1_rtabmap.db')
        os.makedirs(os.path.dirname(database_path), exist_ok=True)
        database_exists = False
        if os.path.exists(database_path):
            print(f"[g1_rtabmap] Existing DB ignored for a fresh map: {database_path}")
        else:
            print(f"[g1_rtabmap] New database: {database_path}")

    camera_xyz, camera_rpy = _pose(cfg['camera'])
    lidar_xyz, lidar_rpy = _pose(cfg['lidar'])
    if lidar_only:
        print(
            "[g1_rtabmap] Lidar only. "
            f"scan_cloud={scan_cloud_topic} odom from the bridge, z fixed. "
            f"base_link -> livox_frame xyz={lidar_xyz} rpy_deg={lidar_rpy}"
        )
    else:
        print(
            "[g1_rtabmap] Uncalibrated extrinsics. "
            f"base_link -> camera_link xyz={camera_xyz} rpy_deg={camera_rpy}; "
            f"base_link -> livox_frame xyz={lidar_xyz} rpy_deg={lidar_rpy}"
        )

    rtab_params = dict(cfg['rtab'])
    rtab_params['use_sim_time'] = use_sim_time
    rtab_params['database_path'] = database_path
    # 0 = lidar only, 1 = depth only, 2 = both into /map.
    rtab_params['Grid/Sensor'] = '0' if lidar_only else '2'
    rtab_params['subscribe_rgb'] = not lidar_only
    rtab_params['subscribe_depth'] = not lidar_only
    rtab_params['subscribe_rgbd'] = not lidar_only
    if lidar_only:
        rtab_params['RGBD/LoopClosureReextractFeatures'] = 'false'
        # No camera, so the visual loop detector only warns and never returns a pose.
        rtab_params['Rtabmap/LoopThr'] = '1'
        rtab_params['Kp/MaxFeatures'] = '0'
    rtab_params['subscribe_scan_cloud'] = enable_livox_cloud
    rtab_params['RGBD/NeighborLinkRefining'] = 'true' if enable_livox_cloud else 'false'
    if not enable_livox_cloud:
        rtab_params['RGBD/ProximityPathMaxNeighbors'] = '0'
    # 0 = visual, 1 = ICP (lidar), 2 = visual then ICP.
    rtab_params['Reg/Strategy'] = '1' if lidar_only else ('2' if enable_livox_cloud else '0')

    rgbd_sync_params = {key: rtab_params[key] for key in SYNC_KEYS}
    rgbd_sync_params.update({
        'use_sim_time': use_sim_time,
        'database_path': database_path,
        'subscribe_rgbd': True,
        'approx_sync_max_interval': 0.2,
    })

    rtab_remappings = [('odom', odom_topic)]
    if enable_livox_cloud:
        rtab_remappings.append(('scan_cloud', scan_cloud_topic))

    nodes = []
    if not lidar_only:
        nodes.append(_static_tf(
            'g1_base_link_to_camera_link',
            'base_link',
            'camera_link',
            camera_xyz,
            camera_rpy,
        ))
    nodes.append(_static_tf(
        'g1_base_link_to_livox_frame',
        'base_link',
        'livox_frame',
        lidar_xyz,
        lidar_rpy,
    ))
    if not lidar_only:
        nodes.append(Node(
            package='rtabmap_sync',
            executable='rgbd_sync',
            output='screen',
            prefix=rtab_prefix,
            parameters=[rgbd_sync_params],
            remappings=[
                ('rgb/image', cfg['rgb_topic']),
                ('rgb/camera_info', cfg['rgb_info_topic']),
                ('depth/image', cfg['depth_topic']),
            ],
        ))
    return nodes + [
        Node(
            condition=UnlessCondition(localization),
            package='rtabmap_slam',
            executable='rtabmap',
            output='screen',
            prefix=slam_prefix,
            parameters=[rtab_params] + ([
                {'Mem/InitWMWithAllNodes': 'True'}
            ] if database_exists else []),
            remappings=rtab_remappings,
            arguments=[] if database_exists else ['-d'],
        ),
        Node(
            condition=IfCondition(localization),
            package='rtabmap_slam',
            executable='rtabmap',
            output='screen',
            prefix=slam_prefix,
            parameters=[rtab_params, {
                'Mem/IncrementalMemory': 'False',
                'Mem/InitWMWithAllNodes': 'True',
                # The saved map is the reference. Refining against the previous scan fights that match.
                'RGBD/NeighborLinkRefining': 'false',
            }],
            remappings=rtab_remappings,
        ),
    ]


def generate_launch_description():
    workspace_dir = get_workspace_root()
    log_dir = os.path.join(workspace_dir, 'log')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'g1_rtabmap.launch.log')
    print(
        "[g1_rtabmap] Logs: ros2 launch g1_nav g1_rtabmap.launch.py "
        f"2>&1 | tee -a {log_file}"
    )

    def arg(name, default, description, choices=None):
        kwargs = {'default_value': default, 'description': description}
        if choices is not None:
            kwargs['choices'] = choices
        return DeclareLaunchArgument(name, **kwargs)

    return LaunchDescription([
        arg('params_file', default_params_file(), 'YAML file with extrinsics and RTAB-Map parameters.'),
        arg('use_sim_time', 'false', 'Use /clock when true.', ['true', 'false']),
        arg(
            'localization',
            'false',
            'True localizes in an existing database. False starts a fresh map.',
            ['true', 'false'],
        ),
        arg(
            'database_path',
            '',
            'RTAB-Map database. Empty uses <workspace>/map/g1_rtabmap.db and starts fresh.',
        ),
        arg('rtab_cpu_affinity', '', 'CPU affinity for the RTAB-Map processes. Empty uses the YAML value.'),
        arg(
            'lidar_only',
            'true',
            'SLAM from the Mid-360 cloud and odometry. False also subscribes to the camera.',
            ['true', 'false'],
        ),
        OpaqueFunction(function=launch_setup),
    ])
