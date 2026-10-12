import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'go2_nav'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py') + glob('launch/*.launch.*.py')),
        (os.path.join('share', package_name, 'params'), glob('params/*.yaml')),
        (os.path.join('share', package_name, 'rviz'), glob('rviz/*.rviz')),
        (os.path.join('share', package_name, 'config'), glob('config/*')),
        # Install Python scripts as executables
        ('lib/' + package_name, [
            os.path.join('go2_nav', 'imu_timestamp_fixer', 'imu_timestamp_fixer_node.py'),
            os.path.join('go2_nav', 'base_link_project_tf_node.py'),
            os.path.join('go2_nav', 'goal_server_node.py'),
            os.path.join('go2_nav', 'livox_height_filter_node.py'),
            os.path.join('go2_nav', 'ran_loc_seed_node.py'),
        ]),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='unitree',
    maintainer_email='tuannv.email@gmail.com',
    description='TODO: Package description',
    license='TODO: License declaration',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'imu_timestamp_fixer = go2_nav.imu_timestamp_fixer.imu_timestamp_fixer_node:main',
            'base_link_project_tf = go2_nav.base_link_project_tf_node:main',
            'goal_server = go2_nav.goal_server_node:main',
            'livox_height_filter = go2_nav.livox_height_filter_node:main',
            'ran_loc_seed = go2_nav.ran_loc_seed_node:main',
        ],
    },
)
