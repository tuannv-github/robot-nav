#!/usr/bin/env bash
# startup/setup_audio_priority.sh
#
# Invoked by startup/run_vlaa.sh on every VLAA launch (sudo).
# Can also be run manually: sudo bash startup/setup_audio_priority.sh
#
# Installs:
#   /etc/security/limits.d/99-vlaa-audio-priority.conf  (nice -20, rtprio, memlock)
#   /etc/pulse/daemon.conf.d/99-vlaa-high-priority.conf (Pulse RT + high nice)
#   /etc/systemd/system/user.slice.d/50-rt-runtime.conf (SCHED_FIFO for user sessions)
#
# After running: log out and back in (or reboot) so PAM limits apply to new shells.
# Then restart PulseAudio: systemctl --user restart pulseaudio.service

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[setup_audio_priority] Run with sudo." >&2
    exit 1
fi

LIMITS_FILE=/etc/security/limits.d/99-vlaa-audio-priority.conf
PULSE_DROPIN_DIR=/etc/pulse/daemon.conf.d
PULSE_DROPIN_FILE="$PULSE_DROPIN_DIR/99-vlaa-high-priority.conf"
LEGACY_LIMITS=/etc/security/limits.d/99-vlaa-nice.conf
USER_SLICE_DROPIN_DIR=/etc/systemd/system/user.slice.d
USER_SLICE_DROPIN_FILE="$USER_SLICE_DROPIN_DIR/50-rt-runtime.conf"
USER_SLICE_RT_RUNTIME=/sys/fs/cgroup/cpu,cpuacct/user.slice/cpu.rt_runtime_us

log() { echo "[setup_audio_priority] $*"; }
warn() { echo "[setup_audio_priority] WARN: $*" >&2; }

log "Writing $LIMITS_FILE"
cat >"$LIMITS_FILE" <<'EOF'
# VLAA voice stack: max CFS nice, SCHED_FIFO headroom, memlock for PulseAudio.
unitree soft nice -20
unitree hard nice -20
unitree soft rtprio 95
unitree hard rtprio 95
unitree soft memlock unlimited
unitree hard memlock unlimited
EOF

if [ -f "$LEGACY_LIMITS" ]; then
    log "Removing legacy $LEGACY_LIMITS (superseded by 99-vlaa-audio-priority.conf)"
    rm -f "$LEGACY_LIMITS"
fi

log "Writing $PULSE_DROPIN_FILE"
mkdir -p "$PULSE_DROPIN_DIR"
cat >"$PULSE_DROPIN_FILE" <<'EOF'
# VLAA: keep Pulse responsive when it owns USB audio (between VLAA runs).
high-priority = yes
nice-level = -11
realtime-scheduling = yes
realtime-priority = 5
lock-memory = yes
EOF

log "Writing $USER_SLICE_DROPIN_FILE"
mkdir -p "$USER_SLICE_DROPIN_DIR"
cat >"$USER_SLICE_DROPIN_FILE" <<'EOF'
[Slice]
CPUAccounting=yes
EOF

if command -v systemctl >/dev/null 2>&1; then
    log "Reloading systemd (user.slice drop-in)..."
    systemctl daemon-reload
fi

if [ -w "$USER_SLICE_RT_RUNTIME" ]; then
    rt_runtime="$(cat "$USER_SLICE_RT_RUNTIME")"
    if [ "$rt_runtime" = "0" ]; then
        kernel_rt="$(cat /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null || echo 950000)"
        log "Enabling SCHED_FIFO in user.slice (cpu.rt_runtime_us=$kernel_rt)..."
        echo "$kernel_rt" >"$USER_SLICE_RT_RUNTIME"
    else
        log "user.slice cpu.rt_runtime_us already $rt_runtime"
    fi
elif [ -f "$USER_SLICE_RT_RUNTIME" ]; then
    warn "Could not write $USER_SLICE_RT_RUNTIME; reboot or re-login after setup."
fi

restart_pulse_for_user() {
    local user="$1"
    local uid server
    uid="$(id -u "$user" 2>/dev/null)" || return 0

    # This Jetson session is PipeWire (pipewire-pulse). pulseaudio -k / --start
    # either hits that socket or leaves a second daemon beside it, and the USB
    # speaker stays with PipeWire instead of the new VLAA process.
    server="$(sudo -u "$user" XDG_RUNTIME_DIR="/run/user/$uid" pactl info 2>/dev/null | awk -F': ' '/^Server Name:/ {print $2; exit}')"
    if [[ "$server" == *PipeWire* ]]; then
        log "Session audio for $user is PipeWire; not starting PulseAudio."
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if sudo -u "$user" XDG_RUNTIME_DIR="/run/user/$uid" \
            systemctl --user is-active pulseaudio.service >/dev/null 2>&1; then
            log "Restarting pulseaudio.service for $user (systemd user session)..."
            sudo -u "$user" XDG_RUNTIME_DIR="/run/user/$uid" \
                systemctl --user restart pulseaudio.service 2>/dev/null \
                && return 0
        fi
    fi

    log "Fallback pulseaudio -k/--start for $user..."
    sudo -u "$user" XDG_RUNTIME_DIR="/run/user/$uid" pulseaudio -k 2>/dev/null || true
    sudo -u "$user" XDG_RUNTIME_DIR="/run/user/$uid" pulseaudio --start 2>/dev/null || true
}

if command -v pulseaudio >/dev/null 2>&1; then
    log "Restarting PulseAudio to pick up daemon.conf.d..."
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        restart_pulse_for_user "$SUDO_USER"
    fi
    restart_pulse_for_user unitree
fi

log "Done."
log "Verify after re-login:  ulimit -e   (expect 40 for nice -20)"
log "                      ulimit -r   (expect 95)"
log "                      ulimit -l   (expect unlimited)"
log "Pulse (after restart): ps -o pid,ni,cls,pri,cmd -p \$(pgrep pulseaudio | head -1)"
log "  expect NI=-11 on SCHED_OTHER; SCHED_FIFO needs rtkit (may fall back on Jetson)."
log "VLAA: run_vlaa.sh enables user.slice RT at startup + nice -20; VLAA_AUDIO_RT_PRIO=80 for SCHED_FIFO."
