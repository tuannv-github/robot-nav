#!/usr/bin/env python3
"""
RTAB-Map **localization** with RealSense RGB-D + Livox MID-360 ``PointCloud2`` fusion.

Loads a map from ``database_path`` or ``PROJECT_ROOT_DIR/map/rtabmap.db``. Create the map first with
``go2_rtabmap.livox.mapping.launch.py``.

Odometry: Unitree lidar via ``go2-odom.service`` (``/odom``, TF ``odom`` → ``base_link``).

Also starts ``location_publisher`` (same as ``go2_rtabmap.location.launch.py``).

``ran_loc_seed`` publishes the heaviest ``/ran_loc`` position on ``/initialpose``,
then waits for RTAB-Map's next ICP result on ``/info`` before seeding again.
Heading is not in ran-loc. Each seed draws a location with probability
proportional to its weight, and a rejected seed turns the heading 15 degrees.
The seeder waits for ``/info`` before the next guess. After ICP accepts the
scan, ran-loc no longer moves the pose. Ran-loc can miss by 10 m, so the
scan is searched in that region. If the map pose stays farther than 10 m
from every candidate, or ICP keeps rejecting the scan while the robot is
moving, the initial pose is set again.

Prerequisites:
    ros2 launch go2_nav realsense.launch.py
    ros2 launch go2_nav livox_mid360.launch.py

Example:
    ros2 launch go2_nav go2_rtabmap.livox.location.launch.py

    ros2 launch go2_nav go2_rtabmap.livox.location.launch.py scan_cloud_topic:=/livox/lidar

    # RGB-D only (no Livox scan_cloud / no LiDAR ICP odometry):
    ros2 launch go2_nav go2_rtabmap.livox.location.launch.py enable_livox_cloud:=false
"""

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def get_workspace_root():
    """Get workspace root directory by finding the directory containing 'src'."""
    # Get the launch file's directory
    launch_file_path = os.path.abspath(__file__)
    current_dir = os.path.dirname(launch_file_path)
    
    # Go up until we find a directory with 'src' subdirectory (workspace root)
    # This works for both source and installed packages
    max_levels = 10  # Safety limit to avoid infinite loops
    level = 0
    while level < max_levels and current_dir != os.path.dirname(current_dir):
        if os.path.exists(os.path.join(current_dir, 'src')):
            return current_dir
        current_dir = os.path.dirname(current_dir)
        level += 1
    
    # Fallback: go up 3 levels from launch file location
    # (src/go2_nav/launch -> workspace root)
    launch_file_dir = os.path.dirname(launch_file_path)
    return os.path.dirname(os.path.dirname(os.path.dirname(launch_file_dir)))

def launch_setup(context, *args, **kwargs):
    
    use_sim_time = LaunchConfiguration('use_sim_time')
    use_imu_arg = LaunchConfiguration('use_imu')
    rtab_cpu_affinity = LaunchConfiguration('rtab_cpu_affinity').perform(context)
    rtab_prefix = f'taskset -c {rtab_cpu_affinity}'

    # Default: disable IMU if use_sim_time is true, or if explicitly disabled
    use_imu_enabled = use_imu_arg.perform(context) == 'true'
    use_imu_default = use_sim_time.perform(context) not in ['true', 'True']
    use_imu = use_imu_enabled if use_imu_arg.perform(context) in ['true', 'false'] else use_imu_default
    
    # Only wait for IMU initialization if IMU filter is enabled (which computes orientation)
    # Raw IMU from Go2 may not have orientation, so don't wait for it
    filter_imu_enabled = LaunchConfiguration('filter_imu').perform(context) == 'true'
    wait_imu_to_init = use_imu and filter_imu_enabled

    scan_cloud_topic = LaunchConfiguration('scan_cloud_topic').perform(context)
    enable_livox_cloud = LaunchConfiguration('enable_livox_cloud').perform(context) == 'true'

    # Database path for saving/loading maps
    # Only use database from PROJECT_ROOT_DIR/map, don't fall back to ~/.ros/rtabmap.db
    provided_db_path = LaunchConfiguration('database_path').perform(context)
    database_exists = False
    
    if provided_db_path:
        # Use explicitly provided database path
        database_path = provided_db_path
        database_exists = os.path.exists(database_path)
        if database_exists:
            print(f"[go2_rtabmap.livox.location] Using provided RTAB-Map database: {database_path}")
        else:
            print(f"[go2_rtabmap.livox.location] Provided database path does not exist: {database_path}")
    else:
        workspace_dir = get_workspace_root()
        map_db_path = os.path.join(workspace_dir, 'map', 'rtabmap.db')
        database_path = map_db_path
        if os.path.exists(map_db_path):
            database_exists = True
            print(f"[go2_rtabmap.livox.location] RTAB-Map database found in map directory: {database_path}")
        else:
            database_exists = False
            print(f"[go2_rtabmap.livox.location] No database found in map directory: {database_path}")
            print("  Create a map first with go2_rtabmap.livox.mapping.launch.py (saved under map/).")

    base_params = {
        'frame_id': 'base_link',
        'guess_frame_id': 'odom',
        'odom_frame_id': 'odom',
        'Reg/Force3DoF': 'true',
        'approx_sync': True,
        'sync_queue_size': 30,
        'topic_queue_size': 30,
        'use_sim_time': use_sim_time,
        'use_action_for_goal': True,
        'wait_imu_to_init': wait_imu_to_init,
        'wait_for_transform': 0.5,
        # Interpolate /odom to the sensor stamp. Without this, a 2–3 s loc
        # cycle publishes map→odom from the latest odom and the pose jumps
        # by however far the dog moved during that delay.
        'odom_sensor_sync': True,
        'database_path': database_path,
        'Grid/DepthDecimation': '1',
        'Grid/RangeMax': '20',
        'Grid/RayTracing': 'true',
        'GridGlobal/ProbMiss': '0.46',
        'GridGlobal/ProbHit': '0.63',
        'GridGlobal/OccupancyThr': '0.45',
        'GridGlobal/MinSize': '20',
        'Grid/MinClusterSize': '20',
        # 2D occupancy in map frame. After odom reset, z=0 is lowest pose (on ground).
        # z < 0.08 → ground;  0.08–0.5 m → obstacle on /map;  above → ignore.
        'Grid/3D': 'false',
        'Grid/MapFrameProjection': 'true',
        'Grid/NormalsSegmentation': 'false',
        'Grid/MinGroundHeight': '-0.20',
        'Grid/MaxGroundHeight': '0.08',
        'Grid/MaxObstacleHeight': '0.5',
        'Grid/RangeMin': '0.45',
        'Grid/FootprintLength': '0.90',
        'Grid/FootprintWidth': '0.50',
        'Grid/FootprintHeight': '0.45',
        'Odom/ResetCountdown': '2',
        'Kp/RoiRatios': '0.0 0.0 0.0 0.4',
    }

    rgbd_sync_params = {
        **base_params,
        'subscribe_rgbd': True,
        'approx_sync_max_interval': 0.2,
    }

    rtab_params = {
        **base_params,
        'subscribe_rgbd': True,
        'subscribe_scan_cloud': enable_livox_cloud,
        'scan_cloud_is_2d': False,
        'subscribe_odom_info': False,
        'approx_sync_max_interval': 0.15,
        # Fast-motion loc: Vis+ICP was rejecting every loop (0/12 inliers, blur)
        # and taking 2–3 s/update so pose rode stale odom. Livox ICP for the
        # transform; RGB-D only for place recognition. Keep cycle <0.5 s.
        # utlidar odom covariance is tiny → OptimizeMaxError=3 rejected real
        # ~0.3 m corrections. 0 disables that reject. Variance* loosens odom edges.
        'Rtabmap/DetectionRate': '1',
        'Rtabmap/TimeThr': '0',
        'Rtabmap/LoopThr': '0.11',
        'RGBD/OptimizeMaxError': '0',
        'Odom/VarianceLinear': '0.05',
        'Odom/VarianceAngular': '0.01',
        'RGBD/LinearUpdate': '0.05',
        'RGBD/AngularUpdate': '0.05',
        'RGBD/ProximityBySpace': 'true',
        'RGBD/ProximityByTime': 'false',
        'RGBD/ProximityMaxGraphDepth': '0',
        # Ran-loc can miss by 10 m and has no heading, so the first match
        # searches that region and a full turn. The seed node tightens this
        # after ICP accepts.
        'RGBD/ProximityPathFilteringRadius': '10.0',
        'RGBD/ProximityMaxPaths': '8',
        'RGBD/ProximityAngle': '180',
        'RGBD/ProximityOdomGuess': 'true',
        'RGBD/LoopClosureReextractFeatures': 'false',
        'RGBD/LocalRadius': '15',
        'RGBD/MaxLocalRetrieved': '8',
        'RGBD/MaxOdomCacheSize': '3',
        'Kp/MaxFeatures': '400',
        'Kp/NndrRatio': '0.8',
        'GFTT/MinDistance': '5',
        'Vis/MinInliers': '8',
        'Vis/CorNNDR': '0.8',
        'Vis/CorGuessWinSize': '0',
        'Mem/STMSize': '10',
        # Scan-to-scan refine warps the odom chain and fights map LC; proximity is enough.
        'RGBD/NeighborLinkRefining': 'false',
        'RGBD/ProximityPathMaxNeighbors': '5' if enable_livox_cloud else '0',
        # 1=ICP (Livox). 2=Vis+ICP fails at speed (0 visual inliers). 0=visual.
        'Reg/Strategy': '1' if enable_livox_cloud else '0',
        'Icp/Iterations': '30',
        'Icp/MaxCorrespondenceDistance': '10.0',
        'Icp/MaxTranslation': '10.0',
        'Icp/MaxRotation': '3.1416',
        'Icp/VoxelSize': '0.12',
        'Icp/RangeMin': '0.5',
    }

    imu_topic = '/input/imu/filtered' if filter_imu_enabled else '/input/imu'

    rtab_remappings = [('odom', '/odom')]
    if enable_livox_cloud:
        rtab_remappings.append(('scan_cloud', scan_cloud_topic))
    if use_imu:
        rtab_remappings.append(('imu', imu_topic))

    return [
        Node(
            package='rtabmap_sync',
            executable='rgbd_sync',
            output='screen',
            prefix=rtab_prefix,
            parameters=[rgbd_sync_params],
            remappings=[
                ('rgb/image', '/input/camera/camera/color/image_raw'),
                ('rgb/camera_info', '/input/camera/camera/color/camera_info'),
                ('depth/image', '/input/camera/camera/aligned_depth_to_color/image_raw'),
            ],
        ),
        Node(
            package='location_publisher',
            executable='location_publisher',
            name='location_publisher',
            output='screen',
        ),
        Node(
            package='rtabmap_slam',
            executable='rtabmap',
            output='screen',
            prefix=rtab_prefix,
            parameters=[rtab_params, {
                'Mem/IncrementalMemory': 'False',
                'Mem/InitWMWithAllNodes': 'True',
            }],
            remappings=rtab_remappings,
        ),
        Node(
            package='go2_nav',
            executable='ran_loc_seed',
            name='ran_loc_seed',
            output='screen',
        ),
    ]

def generate_launch_description():
    # Set up log directory in project root (for tee redirection)
    workspace_dir = get_workspace_root()
    log_dir = os.path.join(workspace_dir, 'log')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'go2_rtabmap.launch.log')
    print(f"[go2_rtabmap.livox.location] Logs: ros2 launch go2_nav go2_rtabmap.livox.location.launch.py 2>&1 | tee -a {log_file}")
    
    return LaunchDescription([
        DeclareLaunchArgument(
            name='use_sim_time',
            default_value='false',
            choices=['true', 'false'],
            description='Simulation / bag replay clock: true uses /clock. Default false (wall clock / robot).',
        ),
        DeclareLaunchArgument(
            name='filter_imu',
            default_value='false',
            description='Filter IMU data using imu_filter_madgwick (set to true if IMU needs filtering)'
        ),

        DeclareLaunchArgument(
            name='use_imu',
            default_value='false',
            description='Enable/disable IMU usage. Empty = auto (disabled when use_sim_time is true, enabled otherwise). Set to "true" or "false" to override.',
        ),

        DeclareLaunchArgument(
            'database_path',
            default_value='',
            description='Path to RTAB-Map database. If empty, uses PROJECT_ROOT_DIR/map/rtabmap.db (expected after mapping).'
        ),
        DeclareLaunchArgument(
            'rtab_cpu_affinity',
            default_value='0-6',
            description='CPU affinity for RTAB-Map nodes. Default 0-6 for heavier localization.'
        ),
        DeclareLaunchArgument(
            'scan_cloud_topic',
            default_value='/livox/lidar',
            description='Livox PointCloud2 topic for rtabmap scan_cloud.',
        ),
        DeclareLaunchArgument(
            'enable_livox_cloud',
            default_value='true',
            choices=['true', 'false'],
            description='If true, rtabmap subscribes to Livox scan_cloud in addition to RGB-D.',
        ),

        OpaqueFunction(function=launch_setup)
    ])
