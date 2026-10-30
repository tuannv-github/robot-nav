#!/usr/bin/env python3
"""
Downward RealSense D435i for the G1.

Publishes color and aligned depth under ``/input/camera``. Does not publish
``base_link`` → ``camera_link``; ``g1_rtabmap.launch.py`` owns that transform.

    ros2 launch g1_nav realsense.launch.py
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import PushRosNamespace, SetParameter


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('camera_name', default_value='camera'),
        DeclareLaunchArgument(
            'serial_no',
            default_value='',
            description='RealSense serial. Empty uses the first camera.',
        ),
        DeclareLaunchArgument('rgb_camera.color_profile', default_value='1280x720x15'),
        SetParameter(name='depth_module.emitter_enabled', value=1),
        GroupAction([
            PushRosNamespace('input'),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource([
                    os.path.join(
                        get_package_share_directory('realsense2_camera'),
                        'launch',
                        'rs_launch.py',
                    )
                ]),
                launch_arguments={
                    'camera_name': LaunchConfiguration('camera_name'),
                    'serial_no': LaunchConfiguration('serial_no'),
                    'enable_depth': 'true',
                    'enable_color': 'true',
                    'rgb_camera.color_profile': LaunchConfiguration('rgb_camera.color_profile'),
                    'enable_gyro': 'false',
                    'enable_accel': 'false',
                    'align_depth.enable': 'true',
                    'enable_sync': 'true',
                    'pointcloud.enable': 'false',
                }.items(),
            ),
        ]),
    ])
