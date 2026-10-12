import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'odom'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='unitree',
    maintainer_email='tuannv.email@gmail.com',
    description='Republish Unitree /utlidar/robot_odom as /odom and TF odom -> base_link.',
    license='TODO: License declaration',
    entry_points={
        'console_scripts': [
            'utlidar_odom = odom.utlidar_odom_node:main',
            'odom_ext_relay = odom.odom_ext_relay_node:main',
            'odom_tf_eth_tap = odom.lo_tf_relay:tap_main',
            'odom_tf_lo = odom.lo_tf_relay:pub_main',
        ],
    },
)
