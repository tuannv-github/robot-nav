#!/bin/bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VLAA_APP_ROBOTS="${VLAA_APP_ROBOTS:-$HOME/vlaa/app_robots}"
# shellcheck source=tmux.common.sh
source "$SCRIPT_DIR/tmux.common.sh"

SESSION=go2nav
tmux_stack_begin "$SCRIPT_DIR" "$SESSION" nav_livox_llm "$@"
tmux_stack_max_perf "$SCRIPT_DIR"

tmux_stack_ensure_session "$SESSION" main "$TMUX_STACK_PROJECT_DIR"
tmux_stack_ensure_four_panes "$SESSION" main "$TMUX_STACK_PROJECT_DIR"

SETUP="cd $TMUX_STACK_PROJECT_DIR && source ./scripts/setup.sh"
RS_SETUP="export CYCLONEDDS_URI=file://$TMUX_STACK_PROJECT_DIR/cyclonedds/cyclonedds.realsense.xml"
NAV_SETUP="export CYCLONEDDS_URI=file://$TMUX_STACK_PROJECT_DIR/cyclonedds/cyclonedds.nav.xml"

tmux_stack_run_pane "$SESSION" main 0 "cd $SCRIPT_DIR && ./run_go2_controller.sh"
tmux_stack_run_pane "$SESSION" main 1 "cd $SCRIPT_DIR && ./launch_video_publisher.sh"
tmux_stack_run_pane "$SESSION" main 2 "cd $SCRIPT_DIR && ./launch_realsense.sh"
tmux_stack_run_pane "$SESSION" main 3 "cd $SCRIPT_DIR && ./launch_rtabmap_localization.sh"

tmux_stack_reset_window "$SESSION" nav "$TMUX_STACK_PROJECT_DIR" \
    "cd $SCRIPT_DIR && ./launch_livox.sh" \
    "$SETUP && $NAV_SETUP && ros2 launch go2_nav go2_nav2.launch.py"

tmux_stack_reset_window "$SESSION" llm "$VLAA_APP_ROBOTS" \
    "VLAA_APP_ROBOTS=\"$VLAA_APP_ROBOTS\" bash \"$SCRIPT_DIR/run_vlaa.sh\""

tmux_stack_reset_window "$SESSION" monitor "$SCRIPT_DIR" \
    "bash \"$SCRIPT_DIR/system-monitor.sh\""

tmux_stack_reset_window "$SESSION" 5g "$SCRIPT_DIR" "nload 5g"

tmux select-pane -t "$SESSION:main.0"
tmux_stack_attach "$SESSION"
