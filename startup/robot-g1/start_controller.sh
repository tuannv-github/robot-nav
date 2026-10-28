#!/usr/bin/env bash
# Launch the G1 controller (HTTP REST :8081, operator console /console/).
# Part of robot-nav. Same role as startup/run_go2_controller.sh.
#
#   ./startup/robot-g1/start_controller.sh
#   ./startup/robot-g1/start_controller.sh --interface enP8p1s0 --port 8081
#
# Starting again stops the controller already running (bridge process and port).
#
# After colcon build, this uses `ros2 launch g1_controller` when install/ exists.
# Otherwise it runs src/g1_controller/scripts/g1_controller_bridge.py directly.
# Do not use `set -u`: colcon install/setup.bash reads unset variables.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BRIDGE="${PROJECT_DIR}/src/g1_controller/scripts/g1_controller_bridge.py"
CONSOLE_DIR="${PROJECT_DIR}/src/g1_controller/console/web"
SDK_DIR="${G1_SDK_DIR:-${PROJECT_DIR}/../3thrdparties/unitree_sdk2_python}"
VIDEO_SRC="${PROJECT_DIR}/../services/video-src/stream_g1_front.py"
export G1_MEDIAMTX_HOST="${G1_MEDIAMTX_HOST:-10.1.101.173}"

export G1_CONSOLE_DIR="${CONSOLE_DIR}"
export LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
if [[ -d "${SDK_DIR}" ]]; then
  export PYTHONPATH="${SDK_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
fi

pick_python() {
  local candidate
  local candidates=()
  if [[ -n "${G1_PYTHON:-}" ]]; then
    candidates+=("${G1_PYTHON}")
  fi
  candidates+=(
    "${PROJECT_DIR}/../.conda/envs/unitree_deploy/bin/python3"
    "/home/unitree/vlaa/.conda/envs/unitree_deploy/bin/python3"
    python3
  )
  for candidate in "${candidates[@]}"; do
    if [[ "${candidate}" != "python3" && ! -x "${candidate}" ]]; then
      continue
    fi
    if "${candidate}" -c "import unitree_sdk2py, uvicorn, starlette, websockets, zmq" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

if [[ ! -f "${BRIDGE}" ]]; then
  echo "error: G1 controller bridge not found at ${BRIDGE}" >&2
  exit 1
fi

if ! PYTHON="$(pick_python)"; then
  echo "error: no Python with unitree_sdk2py, uvicorn, starlette, websockets, and pyzmq" >&2
  echo "  SDK expected at ${SDK_DIR}" >&2
  echo "  pip install --user 'cyclonedds' 'uvicorn' 'starlette' 'websockets' 'pyzmq'" >&2
  exit 1
fi

# Port the new process will bind. Match --port N, --port=N, and port:=N.
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

stop_existing_controller() {
  local pid killed=0
  local -a pids=()

  mapfile -t pids < <(pgrep -f "g1_controller_bridge.py|ros2 launch g1_controller|stream_g1_front.py" || true)
  for pid in "${pids[@]}"; do
    [[ -z "${pid}" || "${pid}" -eq $$ || "${pid}" -eq "${PPID:-0}" ]] && continue
    if kill -0 "${pid}" 2>/dev/null; then
      if [[ "${killed}" -eq 0 ]]; then
        echo "Stopping existing G1 controller..."
      fi
      killed=1
      kill "${pid}" 2>/dev/null || true
    fi
  done

  if [[ "${killed}" -eq 0 ]]; then
    return 0
  fi

  local i
  for i in $(seq 1 25); do
    local still=0
    mapfile -t pids < <(pgrep -f "g1_controller_bridge.py|ros2 launch g1_controller|stream_g1_front.py" || true)
    for pid in "${pids[@]}"; do
      [[ -z "${pid}" || "${pid}" -eq $$ || "${pid}" -eq "${PPID:-0}" ]] && continue
      if kill -0 "${pid}" 2>/dev/null; then
        still=1
      fi
    done
    if [[ "${still}" -eq 0 ]] && ! ss -tlnH "sport = :${REST_PORT}" | grep -q .; then
      return 0
    fi
    sleep 0.2
  done

  echo "Existing G1 controller did not exit; sending SIGKILL..."
  mapfile -t pids < <(pgrep -f "g1_controller_bridge.py|ros2 launch g1_controller|stream_g1_front.py" || true)
  for pid in "${pids[@]}"; do
    [[ -z "${pid}" || "${pid}" -eq $$ || "${pid}" -eq "${PPID:-0}" ]] && continue
    kill -9 "${pid}" 2>/dev/null || true
  done
  sleep 0.3
}

stop_existing_controller

if [[ -f "${VIDEO_SRC}" ]]; then
  echo "Starting BRIO stream -> whip://${G1_MEDIAMTX_HOST}:8889/stream/g1/front"
  nohup /usr/bin/python3 "${VIDEO_SRC}" --host "${G1_MEDIAMTX_HOST}" >/tmp/g1-video-src.log 2>&1 &
fi

if [[ -f "${PROJECT_DIR}/install/setup.bash" ]] && command -v ros2 >/dev/null 2>&1; then
  # shellcheck source=/dev/null
  source "${PROJECT_DIR}/install/setup.bash"
  exec ros2 launch g1_controller g1_controller.launch.py "$@"
fi

echo "G1 controller (direct) python=${PYTHON}"
echo "  console dir ${CONSOLE_DIR}"
exec "${PYTHON}" "${BRIDGE}" "$@"
