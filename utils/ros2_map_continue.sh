#!/usr/bin/env bash
# Continue the G1 map in map/g1_rtabmap.db.
# Starts in localization with the mapping parameters. After the scan locks
# onto the old map, switch to mapping without restarting:
#
#   ./utils/set_mode_mapping.sh
#
#   ./utils/ros2_map_continue.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

set +u
# shellcheck disable=SC1091
source "${PROJECT_DIR}/ros2_source.sh"
set -u

exec ros2 launch g1_nav g1_continue.launch.py "$@"
