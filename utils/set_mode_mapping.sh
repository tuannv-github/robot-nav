#!/usr/bin/env bash
# Switch the running RTAB-Map from localization to mapping.
# The process stays up, so the map -> odom correction is kept.
#
#   ./utils/set_mode_mapping.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RTABMAP_NODE="${RTABMAP_NODE:-/rtabmap}"
MAPPING_SVC="${RTABMAP_NODE}/set_mode_mapping"

set +u
# shellcheck disable=SC1091
source "${PROJECT_DIR}/ros2_source.sh"
set -u

if ! ros2 service list 2>/dev/null | grep -qx "${MAPPING_SVC}"; then
  echo "error: ${MAPPING_SVC} is not available. Start g1_continue.launch.py first." >&2
  exit 1
fi

echo "Switching ${RTABMAP_NODE} to mapping"
ros2 service call "${MAPPING_SVC}" std_srvs/srv/Empty
echo "Mapping mode enabled"
