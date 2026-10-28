#!/usr/bin/env bash
# Stop the G1 controller started by start_controller.sh.
#
#   ./startup/robot-g1/stop_controller.sh
#   ./startup/robot-g1/stop_controller.sh --port 8081

set -eo pipefail

REST_PORT="${G1_REST_PORT:-8081}"
prev=""
for arg in "$@"; do
  if [[ "${prev}" == "--port" ]]; then
    REST_PORT="${arg}"
  fi
  case "${arg}" in
    --port=*) REST_PORT="${arg#--port=}" ;;
    port:=*) REST_PORT="${arg#port:=}" ;;
  esac
  prev="${arg}"
done

PATTERN="g1_controller_bridge.py|ros2 launch g1_controller"

controller_pids() {
  local pid
  local -a found=()
  mapfile -t found < <(pgrep -f "${PATTERN}" || true)
  for pid in "${found[@]}"; do
    [[ -z "${pid}" || "${pid}" -eq $$ || "${pid}" -eq "${PPID:-0}" ]] && continue
    if kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
    fi
  done
}

mapfile -t PIDS < <(controller_pids)
if [[ "${#PIDS[@]}" -eq 0 ]]; then
  echo "G1 controller is not running"
  exit 0
fi

echo "Stopping G1 controller (${PIDS[*]})..."
kill "${PIDS[@]}" 2>/dev/null || true

for _ in $(seq 1 25); do
  mapfile -t PIDS < <(controller_pids)
  if [[ "${#PIDS[@]}" -eq 0 ]] && ! ss -tlnH "sport = :${REST_PORT}" | grep -q .; then
    echo "G1 controller stopped"
    exit 0
  fi
  sleep 0.2
done

mapfile -t PIDS < <(controller_pids)
if [[ "${#PIDS[@]}" -gt 0 ]]; then
  echo "G1 controller did not exit; sending SIGKILL..."
  kill -9 "${PIDS[@]}" 2>/dev/null || true
  sleep 0.3
fi

if ss -tlnH "sport = :${REST_PORT}" | grep -q .; then
  echo "error: port ${REST_PORT} is still in use" >&2
  exit 1
fi

echo "G1 controller stopped"
