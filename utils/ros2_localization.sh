#!/usr/bin/env bash
# Localize in the newest *.db under map_bk. The map is not updated.
#
#   ./utils/ros2_localization.sh
#   ./utils/ros2_localization.sh database_path:=/path/to/map.db

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

set +u
# shellcheck disable=SC1091
source "${PROJECT_DIR}/ros2_source.sh"
set -u

exec ros2 launch g1_nav g1_localization.launch.py "$@"
