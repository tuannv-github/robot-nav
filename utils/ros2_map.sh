#!/usr/bin/env bash
# Start a fresh G1 lidar map. The existing map/g1_rtabmap.db is deleted.
#
#   ./utils/ros2_map.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

set +u
# shellcheck disable=SC1091
source "${PROJECT_DIR}/ros2_source.sh"
set -u

exec ros2 launch g1_nav g1_rtabmap.launch.py lidar_only:=true "$@"
