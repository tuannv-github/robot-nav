#!/usr/bin/env bash
# Snapshot the live G1 RTAB-Map without closing its database.
#
# /rtabmap/backup closes the live file and reloads it. On this database that
# hits "database is locked" and aborts the process. This script pauses RTAB-Map,
# copies the file with SQLite's backup API, saves /map, then resumes.
#
#   ./utils/save_map.sh
#   ./utils/save_map.sh /path/to/output_dir

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAP_DIR="${PROJECT_DIR}/map"
DEST_DIR="${1:-${PROJECT_DIR}/map_bk}"
DB_PATH="${DB_PATH:-${MAP_DIR}/g1_rtabmap.db}"
MAP_TOPIC="${MAP_TOPIC:-/map}"
RTABMAP_NODE="${RTABMAP_NODE:-/rtabmap}"
PAUSE_SVC="${RTABMAP_NODE}/pause"
RESUME_SVC="${RTABMAP_NODE}/resume"
STAMP="$(date +%Y%m%d_%H%M%S)"
paused=0

set +u
# shellcheck disable=SC1091
source "${PROJECT_DIR}/ros2_source.sh"
set -u

if ! ros2 service list 2>/dev/null | grep -qx "${PAUSE_SVC}"; then
  echo "error: ${PAUSE_SVC} is not available. Start mapping first." >&2
  exit 1
fi
if [[ ! -f "${DB_PATH}" ]]; then
  echo "error: database not found at ${DB_PATH}" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"

resume_mapping() {
  if [[ "${paused}" == 1 ]]; then
    paused=0
    sleep 1
    echo "Resuming RTAB-Map"
    ros2 service call "${RESUME_SVC}" std_srvs/srv/Empty >/dev/null || true
  fi
}
trap resume_mapping EXIT

echo "Pausing RTAB-Map"
ros2 service call "${PAUSE_SVC}" std_srvs/srv/Empty >/dev/null
paused=1

DB_DEST="${DEST_DIR}/g1_rtabmap_${STAMP}.db"
echo "Copying ${DB_PATH} → ${DB_DEST}"
python3 - "${DB_PATH}" "${DB_DEST}" << 'PY'
import sqlite3
import sys

source_path, dest_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
dest = sqlite3.connect(dest_path)
source.backup(dest)
dest.close()
source.close()
PY
echo "saved ${DB_DEST}"

MAP_STEM="${DEST_DIR}/map_${STAMP}"
echo "Saving occupancy grid from ${MAP_TOPIC}"
python3 - "${MAP_TOPIC}" "${MAP_STEM}" << 'PY'
import math
import sys

import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

topic, stem = sys.argv[1], sys.argv[2]
rclpy.init()
node = Node("save_map_grid")
holder = {"msg": None}

def on_map(msg):
    holder["msg"] = msg

qos = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)
node.create_subscription(OccupancyGrid, topic, on_map, qos)
end = node.get_clock().now().nanoseconds + 10 * 1_000_000_000
while rclpy.ok() and holder["msg"] is None and node.get_clock().now().nanoseconds < end:
    rclpy.spin_once(node, timeout_sec=0.2)
msg = holder["msg"]
node.destroy_node()
rclpy.shutdown()
if msg is None:
    raise SystemExit(f"no message on {topic}")

width, height = msg.info.width, msg.info.height
rows = []
for row in range(height - 1, -1, -1):
    pixels = bytearray(width)
    base = row * width
    for col in range(width):
        cell = msg.data[base + col]
        if cell < 0:
            pixels[col] = 205
        elif cell == 0:
            pixels[col] = 254
        else:
            pixels[col] = 0
    rows.append(pixels)
with open(stem + ".pgm", "wb") as handle:
    handle.write(f"P5\n{width} {height}\n255\n".encode())
    handle.write(b"".join(rows))
origin = msg.info.origin
q = origin.orientation
yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
with open(stem + ".yaml", "w", encoding="utf-8") as handle:
    handle.write(
        f"image: {stem.split('/')[-1]}.pgm\n"
        "mode: trinary\n"
        f"resolution: {msg.info.resolution}\n"
        f"origin: [{origin.position.x}, {origin.position.y}, {yaw}]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.25\n"
    )
print(f"{width}x{height} cells")
PY
echo "saved ${MAP_STEM}.pgm"
echo "saved ${MAP_STEM}.yaml"
