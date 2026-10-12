#!/bin/bash
# Shared helpers for startup/tmux_*.sh scripts (sourced as tmux.common.sh).

TMUX_STACK_SCRIPT_DIR=""
TMUX_STACK_PROJECT_DIR=""
TMUX_STACK_SESSION=""
TMUX_STACK_KILL_ONLY=0

# --- CLI ---

tmux_stack_parse_args() {
    TMUX_STACK_KILL_ONLY=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -k|--kill-only)
                TMUX_STACK_KILL_ONLY=1
                ;;
            -h|--help)
                _tmux_stack_usage "${BASH_SOURCE[1]}"
                exit 0
                ;;
            *)
                echo "Unknown option: $1 (try -h)" >&2
                exit 1
                ;;
        esac
        shift
    done
}

_tmux_stack_usage() {
    local script
    script=$(basename "${1:-tmux_stack.sh}")
    cat <<EOF
Usage: $script [-k|--kill-only]

  -k, --kill-only   Stop stack jobs (SIGINT + pkill) and kill the tmux session; do not start.
  -h, --help        Show this help.
EOF
}

tmux_stack_finish_kill_only() {
    if [ "${TMUX_STACK_KILL_ONLY:-0}" -eq 1 ]; then
        tmux_stack_kill_session "${TMUX_STACK_SESSION:-}"
        echo "Kill-only (-k): stack jobs stopped, tmux session removed."
        exit 0
    fi
}

# script_dir session kill_profile -- "$@"
tmux_stack_begin() {
    local script_dir=$1 session=$2 profile=$3
    shift 3

    TMUX_STACK_SCRIPT_DIR=$script_dir
    TMUX_STACK_PROJECT_DIR=$(cd "$script_dir/.." && pwd)
    TMUX_STACK_SESSION=$session
    tmux_stack_parse_args "$@"
    tmux_stack_kill_profile "$session" "$profile"
}

# --- kill ---

_tmux_stack_pkill() {
    pkill -f "$1" 2>/dev/null || true
}

# SIGTERM, then SIGKILL anything still matching. VLAA's speaker catches SIGINT/SIGTERM
# and outlives the tmux pane, so it keeps the USB speaker after a stack restart.
_tmux_stack_pkill_finish() {
    local pattern alive i
    for pattern in "$@"; do
        _tmux_stack_pkill "$pattern"
    done
    for i in 1 2 3 4 5; do
        alive=0
        for pattern in "$@"; do
            if pgrep -f "$pattern" >/dev/null 2>&1; then
                alive=1
                break
            fi
        done
        [ "$alive" -eq 0 ] && return 0
        sleep 0.3
    done
    for pattern in "$@"; do
        pkill -9 -f "$pattern" 2>/dev/null || true
    done
}

_tmux_stack_pkill_controller() {
    _tmux_stack_pkill '[r]un_mqtt_to_ros2\.sh'
    _tmux_stack_pkill '[r]un_go2_controller\.sh'
    _tmux_stack_pkill '[r]os2 launch go2_controller go2_controller\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch realsense_video_publisher video_publisher\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav realsense\.launch\.py'
}

_tmux_stack_pkill_nav_livox() {
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.location\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.livox\.location\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_nav2\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav livox_mid360\.launch\.py'
}

_tmux_stack_pkill_map_livox() {
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.location\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.livox\.mapping\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav livox_mid360\.launch\.py'
}

_tmux_stack_pkill_nav_realsense() {
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.location\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_nav2\.launch\.py'
}

_tmux_stack_pkill_map_realsense() {
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.location\.launch\.py'
    _tmux_stack_pkill '[r]os2 launch go2_nav go2_rtabmap\.mapping\.launch\.py'
}

_tmux_stack_pkill_llm() {
    _tmux_stack_pkill_finish \
        '[a]pp_robots/main\.py' \
        '[p]ython3 main\.py --robot-model' \
        '[r]un_vlaa\.sh' \
        '[a]udio_recorder' \
        '[a]udio_speaker' \
        '[w]ifi-heartbeat\.sh' \
        '[s]ystem-monitor\.sh'
}

_tmux_stack_pkill_remote() {
    _tmux_stack_pkill '[v]ideo-publisher\.py'
}

# session profile
tmux_stack_kill_profile() {
    local session=$1 profile=$2
    local restore_pulse=0

    echo "Stopping previous stack jobs (${profile})..."
    tmux_stack_interrupt_session "$session"
    sleep 1

    case "$profile" in
        remote)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_remote
            ;;
        remote_llm)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_remote
            _tmux_stack_pkill_llm
            restore_pulse=1
            ;;
        nav_livox)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_nav_livox
            ;;
        nav_livox_llm)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_nav_livox
            _tmux_stack_pkill_llm
            restore_pulse=1
            ;;
        map_livox)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_map_livox
            ;;
        nav_realsense)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_nav_realsense
            ;;
        map_realsense)
            _tmux_stack_pkill_controller
            _tmux_stack_pkill_map_realsense
            ;;
        *)
            echo "Unknown kill profile: ${profile}" >&2
            exit 1
            ;;
    esac

    if [ "$restore_pulse" -eq 1 ]; then
        tmux_stack_restore_pulse_cards
    fi
    sleep 0.5
    tmux_stack_kill_session "$session"
    tmux_stack_finish_kill_only
}

tmux_stack_interrupt_session() {
    local session=$1
    if ! tmux has-session -t "$session" 2>/dev/null; then
        return 0
    fi
    local target
    while IFS= read -r target; do
        [ -n "$target" ] || continue
        tmux send-keys -t "$session:$target" C-c 2>/dev/null || true
        sleep 0.2
        tmux send-keys -t "$session:$target" C-c 2>/dev/null || true
    done < <(tmux list-panes -s -t "$session" -F '#{window_name}.#{pane_index}' 2>/dev/null)
    sleep 0.5
}

tmux_stack_kill_session() {
    local session=$1
    [ -n "$session" ] || return 0
    if tmux has-session -t "$session" 2>/dev/null; then
        echo "Killing tmux session '$session'..."
        tmux kill-session -t "$session"
    fi
}

tmux_stack_restore_pulse_cards() {
    if command -v pactl >/dev/null 2>&1; then
        pactl list short cards 2>/dev/null \
            | awk '$2 ~ /Blink500B2|10d6_4803|USB_Composite|0909_005b/ {print $2}' \
            | xargs -r -I{} pactl suspend-card {} 0 2>/dev/null || true
    fi
}

# --- startup helpers ---

tmux_stack_max_perf() {
    local script_dir=$1
    echo "Setting Jetson to max performance..."
    if ! bash "$script_dir/02_cpu/cpu.sh"; then
        echo "Warning: failed to apply max performance settings, continuing startup."
    fi
}

tmux_stack_ensure_session() {
    local session=$1 window=$2 dir=$3
    # Session is always killed in tmux_stack_begin before startup; create fresh.
    echo "Creating new session $session"
    tmux new-session -d -s "$session" -n "$window" -c "$dir"
}

# Build exactly four panes in tiled layout (fresh window has one pane).
tmux_stack_ensure_four_panes() {
    local session=$1 window=$2 dir=$3
    while [ "$(tmux list-panes -t "$session:$window" 2>/dev/null | wc -l)" -lt 4 ]; do
        tmux split-window -t "$session:$window.0" -c "$dir"
    done
    tmux select-layout -t "$session:$window" tiled
    sleep 1
}

tmux_stack_pane_exists() {
    local session=$1 window=$2 pane_idx=$3
    tmux list-panes -t "$session:$window" -F '#{pane_index}' 2>/dev/null | grep -q "^${pane_idx}$"
}

# session window pane_idx cmd  — or  full_target cmd
tmux_stack_run_pane() {
    local target cmd
    if [[ "$1" == *:* ]]; then
        target=$1
        cmd=$2
    else
        target="$1:$2.$3"
        cmd=$4
        if ! tmux_stack_pane_exists "$1" "$2" "$3"; then
            echo "Pane $3 does not exist. Skipping."
            return 1
        fi
        echo "Starting pane $3..."
    fi
    tmux send-keys -t "$target" C-c C-u
    sleep 0.2
    tmux send-keys -t "$target" "$cmd" C-m
}

# Replace a named window; optional second command splits vertically.
tmux_stack_reset_window() {
    local session=$1 win=$2 dir=$3 cmd0=$4 cmd1=${5:-}
    if tmux list-windows -t "$session" -F '#{window_name}' | grep -q "^${win}$"; then
        tmux kill-window -t "$session:$win"
    fi
    tmux new-window -t "$session" -n "$win" -c "$dir"
    tmux send-keys -t "$session:$win.0" "$cmd0" C-m
    if [ -n "$cmd1" ]; then
        tmux split-window -v -t "$session:$win.0" -c "$dir"
        tmux send-keys -t "$session:$win.1" "$cmd1" C-m
        tmux select-layout -t "$session:$win" even-vertical
    fi
}

tmux_stack_attach() {
    local session=$1
    echo "Attaching to session '$session'..."
    if [ -z "$TMUX" ] && [ -t 0 ]; then
        tmux attach-session -t "$session"
    elif [ -n "$TMUX" ]; then
        tmux switch-client -t "$session"
    else
        echo "Attach: tmux attach -t $session"
    fi
}
