#!/usr/bin/env python3
"""eth0 ``/utlidar/robot_odom`` -> ``/odom``; named pipe -> all-NIC ``/odom``.

    ros2 launch odom odom.launch.py
"""

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue

_REPO = os.environ.get('GO2_NAV_ROOT', os.path.expanduser('~/go2-nav'))
_ETH0_URI = os.environ.get(
    'CYCLONEDDS_ETH0_URI', f'file://{_REPO}/cyclonedds/cyclonedds.eth0.xml'
)
_EXT_URI = os.environ.get(
    'CYCLONEDDS_EXT_URI', f'file://{_REPO}/cyclonedds/cyclonedds.odom-ext.xml'
)
_FASTDDS_XML = os.environ.get(
    'FASTRTPS_DEFAULT_PROFILES_FILE',
    os.path.expanduser('/tmp/fastrtps.odom-ext.xml'),
)
_LO_URI = os.environ.get(
    'CYCLONEDDS_LO_URI', f'file://{_REPO}/cyclonedds/cyclonedds.local.xml'
)


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('input_topic', default_value='/utlidar/robot_odom'),
        DeclareLaunchArgument('output_topic', default_value='/odom'),
        DeclareLaunchArgument('odom_frame', default_value='odom'),
        DeclareLaunchArgument('base_frame', default_value='base_link'),
        DeclareLaunchArgument('zero_at_start', default_value='true'),
        DeclareLaunchArgument('publish_tf', default_value='true'),
        DeclareLaunchArgument('use_ros_time', default_value='true'),
        DeclareLaunchArgument('relay_pipe', default_value='/tmp/go2_odom.fifo'),
        DeclareLaunchArgument('tf_lo_pipe', default_value='/tmp/go2_tf_lo.fifo'),
        DeclareLaunchArgument('reset_flag', default_value='/tmp/go2_odom.reset'),
        Node(
            package='odom',
            executable='utlidar_odom',
            name='utlidar_odom',
            output='screen',
            additional_env={
                'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp',
                'CYCLONEDDS_URI': _ETH0_URI,
            },
            parameters=[{
                'input_topic': LaunchConfiguration('input_topic'),
                'output_topic': LaunchConfiguration('output_topic'),
                'odom_frame': LaunchConfiguration('odom_frame'),
                'base_frame': LaunchConfiguration('base_frame'),
                'zero_at_start': ParameterValue(LaunchConfiguration('zero_at_start'), value_type=bool),
                'publish_tf': ParameterValue(LaunchConfiguration('publish_tf'), value_type=bool),
                'use_ros_time': ParameterValue(LaunchConfiguration('use_ros_time'), value_type=bool),
                'relay_pipe': LaunchConfiguration('relay_pipe'),
                'reset_flag': LaunchConfiguration('reset_flag'),
            }],
        ),
        Node(
            package='odom',
            executable='odom_ext_relay',
            name='odom_ext_relay',
            output='screen',
            respawn=True,
            respawn_delay=3.0,
            # FastDDS on all NICs: roboticpc Humble default can echo /odom
            # without rmw_cyclonedds (and without seeing eth0 /utlidar/*).
            additional_env={
                'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
                'CYCLONEDDS_URI': '',
                'FASTRTPS_DEFAULT_PROFILES_FILE': _FASTDDS_XML,
                'ROS_LOCALHOST_ONLY': '0',
            },
            parameters=[{
                'output_topic': LaunchConfiguration('output_topic'),
                'publish_tf': ParameterValue(LaunchConfiguration('publish_tf'), value_type=bool),
                'relay_pipe': LaunchConfiguration('relay_pipe'),
                'reset_flag': LaunchConfiguration('reset_flag'),
            }],
        ),
        # eth0 tap + lo publisher: rviz2-local (lo only) can see odom -> base_link.
        # Do not put both interfaces in one process; that is the Unitree SPDP failure.
        Node(
            package='odom',
            executable='odom_tf_eth_tap',
            name='odom_tf_eth_tap',
            output='screen',
            respawn=True,
            respawn_delay=3.0,
            additional_env={
                'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp',
                'CYCLONEDDS_URI': _ETH0_URI,
                'FASTRTPS_DEFAULT_PROFILES_FILE': '',
            },
            parameters=[{
                'relay_pipe': LaunchConfiguration('tf_lo_pipe'),
                'odom_frame': LaunchConfiguration('odom_frame'),
                'base_frame': LaunchConfiguration('base_frame'),
            }],
        ),
        Node(
            package='odom',
            executable='odom_tf_lo',
            name='odom_tf_lo',
            output='screen',
            respawn=True,
            respawn_delay=3.0,
            additional_env={
                'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp',
                'CYCLONEDDS_URI': _LO_URI,
                'FASTRTPS_DEFAULT_PROFILES_FILE': '',
                'ROS_LOCALHOST_ONLY': '0',
            },
            parameters=[{
                'relay_pipe': LaunchConfiguration('tf_lo_pipe'),
            }],
        ),
    ])
