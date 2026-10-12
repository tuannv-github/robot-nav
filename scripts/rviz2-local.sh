#!/usr/bin/env bash
# RViz2 on this robot. Livox, RealSense, and video publish on loopback only.
# Nav2 also listens on lo (cyclonedds.nav.xml). scripts/rviz2.sh is for the
# master at 10.1.100.220 and does not see those topics from here.
# Do not add eth0: that bus is the Unitree MCU link.
# Do not use `set -u`: sourcing the ROS setup touches unset variables.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${PROJECT_DIR}/install/setup.bash" ]]; then
  # shellcheck source=/dev/null
  source "${PROJECT_DIR}/install/setup.bash"
elif [[ -f /opt/ros/humble/setup.bash ]]; then
  # shellcheck source=/dev/null
  source /opt/ros/humble/setup.bash
else
  echo "ROS Humble not found" >&2
  exit 1
fi

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="file://${PROJECT_DIR}/cyclonedds/cyclonedds.local.xml"

echo "rviz2 local CycloneDDS lo ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"

if [[ $# -eq 0 ]]; then
  RVIZ="$(ros2 pkg prefix go2_nav)/share/go2_nav/rviz/go2_navigation.rviz"
  exec rviz2 -d "${RVIZ}"
fi
exec rviz2 "$@"
