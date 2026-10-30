#!/usr/bin/env bash
# Build the ROS 2 workspace in src/ with symlinks into install/.
# Do not use set -u: the ROS setup script reads unset variables.
set -eo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash

colcon build --symlink-install --base-paths src "$@"
