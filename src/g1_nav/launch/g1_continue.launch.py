#!/usr/bin/env python3
"""Continue G1 SLAM on an existing database, using the mapping parameters.

Starts in localization so the current odom can lock onto the old map. The
database is not deleted. After the scan matches, switch to mapping without
restarting:

    ./utils/set_mode_mapping.sh

    source ros2_source.sh
    ros2 launch g1_nav g1_continue.launch.py
"""

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def _workspace_root():
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isdir(os.path.join(current, 'src')):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mapping_params():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'config', 'g1_rtabmap.yaml'))


def _launch(context, *args, **kwargs):
    database_path = LaunchConfiguration('database_path').perform(context).strip()
    if not database_path:
        database_path = os.path.join(_workspace_root(), 'map', 'g1_rtabmap.db')
    if not os.path.isfile(database_path):
        raise RuntimeError(f'continue database not found: {database_path}')
    params_file = LaunchConfiguration('params_file').perform(context).strip() or _mapping_params()
    print(f'[g1_continue] Localizing with mapping parameters, then call /rtabmap/set_mode_mapping')
    print(f'[g1_continue] Database: {database_path}')
    print(f'[g1_continue] Parameters: {params_file}')
    return [IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([
                FindPackageShare('g1_nav'),
                'launch',
                'g1_rtabmap.launch.py',
            ])
        ),
        launch_arguments={
            'localization': 'true',
            'lidar_only': 'true',
            'database_path': database_path,
            'use_sim_time': LaunchConfiguration('use_sim_time').perform(context),
            'params_file': params_file,
            'rtab_cpu_affinity': LaunchConfiguration('rtab_cpu_affinity').perform(context),
        }.items(),
    )]


def generate_launch_description():
    def arg(name, default, description):
        return DeclareLaunchArgument(name, default_value=default, description=description)

    return LaunchDescription([
        arg(
            'database_path',
            '',
            'Existing RTAB-Map database. Empty uses <workspace>/map/g1_rtabmap.db.',
        ),
        arg('use_sim_time', 'false', 'Use /clock when true.'),
        arg('params_file', '', 'YAML file. Empty uses config/g1_rtabmap.yaml.'),
        arg('rtab_cpu_affinity', '', 'CPU affinity. Empty uses the YAML value.'),
        OpaqueFunction(function=_launch),
    ])
