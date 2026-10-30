#!/usr/bin/env python3
"""Launch robot_bridge (REST :8081 and /console/)."""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    prefix = get_package_prefix("g1_controller")
    script = os.path.join(prefix, "lib", "g1_controller", "robot_bridge.py")
    console = os.path.join(prefix, "share", "g1_controller", "console", "web")
    return LaunchDescription([
        DeclareLaunchArgument("interface", default_value="auto"),
        DeclareLaunchArgument("port", default_value="8081"),
        ExecuteProcess(
            cmd=[
                script,
                "--interface", LaunchConfiguration("interface"),
                "--port", LaunchConfiguration("port"),
            ],
            additional_env={"G1_CONSOLE_DIR": console},
            output="screen",
        ),
    ])
