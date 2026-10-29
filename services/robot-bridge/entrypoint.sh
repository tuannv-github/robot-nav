#!/usr/bin/env bash
# Start the ZMQ control bridge on the host ROS 2 graph.
# Do not use nounset around setup.bash: it reads unset variables.
set -eo pipefail

set +u
source /opt/ros/humble/setup.bash
set -u

export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

if [[ "${RMW_IMPLEMENTATION}" == *cyclonedds* && -z "${CYCLONEDDS_URI:-}" ]]; then
  iface="${DDS_INTERFACE:-ens12f0}"
  peers="${DDS_PEERS:-10.1.100.220}"
  config="${CYCLONEDDS_CONFIG:-/tmp/robot-bridge-cyclonedds.xml}"
  if [[ ! "${iface}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    echo "error: DDS_INTERFACE must be a network interface name" >&2
    exit 1
  fi
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<CycloneDDS><Domain id="any"><General><Interfaces>'
    echo "<NetworkInterface name=\"${iface}\" priority=\"1\" multicast=\"default\" />"
    echo '</Interfaces></General><Discovery><Peers>'
    IFS=',' read -ra addrs <<< "${peers}"
    for addr in "${addrs[@]}"; do
      addr="${addr// /}"
      if [[ -z "${addr}" ]]; then
        continue
      fi
      if [[ ! "${addr}" =~ ^[A-Za-z0-9.:-]+$ ]]; then
        echo "error: bad DDS peer ${addr}" >&2
        exit 1
      fi
      echo "<Peer address=\"${addr}\"/>"
    done
    echo '</Peers></Discovery></Domain></CycloneDDS>'
  } > "${config}"
  export CYCLONEDDS_URI="file://${config}"
  echo "dds ${iface} peers ${peers}"
fi

echo "rmw ${RMW_IMPLEMENTATION} domain ${ROS_DOMAIN_ID}"
echo "zmq ${ZMQ_CONTROL_SUB:-tcp://127.0.0.1:5603} -> ${CMD_VEL_TOPIC:-/cmd_vel}"
exec python3 -u /app/control_bridge.py
