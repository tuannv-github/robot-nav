import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'g1_nav'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    entry_points={
        'console_scripts': [
            'odom_tf = g1_nav.odom_tf_node:main',
        ],
    },
    maintainer='unitree',
    maintainer_email='tuannv.email@gmail.com',
    description='RTAB-Map SLAM for the Unitree G1.',
    license='TODO: License declaration',
)
