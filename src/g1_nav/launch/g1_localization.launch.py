#!/usr/bin/env python3
"""Localize the G1 in an existing RTAB-Map database. The map is not updated.

An empty database_path uses the newest *.db in <workspace>/map_bk.

    source ros2_source.sh
    ros2 launch g1_nav g1_localization.launch.py

    ros2 launch g1_nav g1_localization.launch.py \
        database_path:=/home/fcp/robot-nav/map_bk/g1_rtabmap_20261031_002906.db
"""

import glob
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def _default_params():
    share = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'config', 'g1_localization.yaml'))
    try:
        from ament_index_python.packages import get_package_share_directory
        installed = os.path.join(get_package_share_directory('g1_nav'), 'config', 'g1_localization.yaml')
        if os.path.isfile(installed):
            return installed
    except Exception:
        pass
    return share


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


def _latest_backup(workspace):
    folder = os.path.join(workspace, 'map_bk')
    candidates = [path for path in glob.glob(os.path.join(folder, '*.db')) if os.path.isfile(path)]
    if not candidates:
        raise RuntimeError(f'no database in {folder}')
    return max(candidates, key=os.path.getmtime)


def _launch(context, *args, **kwargs):
    database_path = LaunchConfiguration('database_path').perform(context).strip()
    if not database_path:
        database_path = _latest_backup(_workspace_root())
    if not os.path.isfile(database_path):
        raise RuntimeError(f'localization database not found: {database_path}')
    params_file = LaunchConfiguration('params_file').perform(context).strip() or _default_params()
    print(f'[g1_localization] Localizing in {database_path}')
    print(f'[g1_localization] Parameters: {params_file}')
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
            'Existing RTAB-Map database. Empty uses the newest *.db in <workspace>/map_bk.',
        ),
        arg('use_sim_time', 'false', 'Use /clock when true.'),
        arg('params_file', '', 'YAML file. Empty uses config/g1_localization.yaml.'),
        arg('rtab_cpu_affinity', '', 'CPU affinity. Empty uses the YAML value.'),
        OpaqueFunction(function=_launch),
    ])
