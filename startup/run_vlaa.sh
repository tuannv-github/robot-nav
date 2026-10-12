#!/bin/bash
# startup/run_vlaa.sh
#
# Launch the VLAA voice/LLM client (app_robots/main.py) and dynamically hand
# the Blink500B2+ mic and the USB Composite speaker from PulseAudio to VLAA's
# direct ALSA path, then give them back to PulseAudio when VLAA exits.
#
# This is "Option 1" from fix_audio.md (dynamic Pulse handoff): instead of
# permanently telling Pulse to ignore those USB devices via a udev rule, we
# ask Pulse to suspend its ALSA card client only while VLAA is running.
#
# Env vars (override before invoking if needed):
#   VLAA_APP_ROBOTS           Path to VLAA app_robots tree (default: ~/vlaa/app_robots)
#   VLAA_PULSE_CARD_PATTERNS  ERE matching Pulse card names to hand over
#                             (default covers Blink500B2+ and the 0909:005b speaker)
#   VLAA_ALSA_SPEAKER_CARD    ALSA card index for the USB speaker (default: auto from
#                             /proc/asound/cards line containing "USB Composite Device")
#   VLAA_AUDIO_NICE           Nice for this shell + python tree (default: -20)
#   VLAA_AUDIO_RT_PRIO        SCHED_FIFO for VLAA audio processes (default: 80;
#                             set empty to disable)
#   ROUTE_SCRIPT              Host-route script (default: startup/route.5g.sh)
#
# Audio priority: calls setup_audio_priority.sh (sudo) then applies nice + SCHED_FIFO.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_AUDIO_PRIORITY="$SCRIPT_DIR/setup_audio_priority.sh"
ROUTE_SCRIPT="${ROUTE_SCRIPT:-$SCRIPT_DIR/route.5g.sh}"

VLAA_APP_ROBOTS="${VLAA_APP_ROBOTS:-$HOME/vlaa/app_robots}"
VLAA_PULSE_CARD_PATTERNS="${VLAA_PULSE_CARD_PATTERNS:-Blink500B2|10d6_4803|USB_Composite|0909_005b}"
VLAA_AUDIO_NICE="${VLAA_AUDIO_NICE:--20}"
VLAA_AUDIO_RT_PRIO="${VLAA_AUDIO_RT_PRIO:-80}"
VLAA_USE_UDP_LOG="${VLAA_USE_UDP_LOG:-1}"
export VLAA_USE_UDP_LOG
USER_SLICE_RT_RUNTIME=/sys/fs/cgroup/cpu,cpuacct/user.slice/cpu.rt_runtime_us
export VLAA_AUDIO_NICE VLAA_AUDIO_RT_PRIO

log()  { echo "[run_vlaa] $*"; }
warn() { echo "[run_vlaa] WARN: $*" >&2; }

run_setup_audio_priority() {
    if [ ! -f "$SETUP_AUDIO_PRIORITY" ]; then
        warn "Missing $SETUP_AUDIO_PRIORITY; skipping system audio setup."
        return 1
    fi
    log "Running audio priority setup: $SETUP_AUDIO_PRIORITY"
    if sudo bash "$SETUP_AUDIO_PRIORITY"; then
        log "Audio priority setup completed."
        return 0
    fi
    warn "Audio priority setup failed; continuing with best-effort runtime setup."
    return 1
}

run_routes() {
    if [ ! -f "$ROUTE_SCRIPT" ]; then
        warn "Missing $ROUTE_SCRIPT; skipping route setup."
        return 1
    fi
    log "Applying routes: $ROUTE_SCRIPT"
    if bash "$ROUTE_SCRIPT"; then
        log "Routes applied."
        return 0
    fi
    warn "Route setup failed; continuing."
    return 1
}

# Fallback if setup_audio_priority.sh could not enable cgroup RT (e.g. sudo denied).
enable_user_slice_rt() {
    [ -n "${VLAA_AUDIO_RT_PRIO:-}" ] || return 0
    [ -f "$USER_SLICE_RT_RUNTIME" ] || return 0

    local rt_runtime kernel_rt
    rt_runtime="$(cat "$USER_SLICE_RT_RUNTIME")"
    if [ "$rt_runtime" != "0" ]; then
        log "user.slice SCHED_FIFO runtime already enabled ($rt_runtime us)."
        return 0
    fi

    kernel_rt="$(cat /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null || echo 950000)"
    if [ -w "$USER_SLICE_RT_RUNTIME" ]; then
        echo "$kernel_rt" >"$USER_SLICE_RT_RUNTIME"
        log "Enabled user.slice SCHED_FIFO runtime ($kernel_rt us)."
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && sudo -n sh -c "echo '$kernel_rt' > '$USER_SLICE_RT_RUNTIME'" 2>/dev/null; then
        log "Enabled user.slice SCHED_FIFO runtime via sudo ($kernel_rt us)."
        return 0
    fi

    warn "user.slice cpu.rt_runtime_us=0; SCHED_FIFO unavailable."
    return 1
}

apply_shell_nice() {
    if ! renice -n "$VLAA_AUDIO_NICE" -p $$ >/dev/null 2>&1; then
        warn "Could not set nice $VLAA_AUDIO_NICE (re-login after setup_audio_priority.sh)."
        return 1
    fi
    log "Shell nice set to $VLAA_AUDIO_NICE (python + children inherit CFS priority)."
}

verify_sched_fifo() {
    [ -n "${VLAA_AUDIO_RT_PRIO:-}" ] || return 0
    if chrt -f "$VLAA_AUDIO_RT_PRIO" true 2>/dev/null; then
        log "SCHED_FIFO available for VLAA audio (prio $VLAA_AUDIO_RT_PRIO)."
        return 0
    fi
    warn "SCHED_FIFO prio $VLAA_AUDIO_RT_PRIO not available; audio will use nice $VLAA_AUDIO_NICE only."
    return 1
}

run_setup_audio_priority || true
enable_user_slice_rt || true
apply_shell_nice
verify_sched_fifo || true
# Routes are managed by the surrounding tmux/network startup scripts.

list_target_cards() {
    pactl list short cards 2>/dev/null \
        | awk -v pat="$VLAA_PULSE_CARD_PATTERNS" '$2 ~ pat {print $2}'
}

# mode: 1 = suspend (release ALSA so VLAA can grab hw:),
#       0 = resume   (let Pulse re-attach to the ALSA card)
set_card_suspend() {
    local mode="$1"
    if ! command -v pactl >/dev/null 2>&1; then
        log "pactl not installed; skipping Pulse handoff (mode=$mode)."
        return 0
    fi
    local cards
    cards="$(list_target_cards)" || true
    if [ -z "$cards" ]; then
        if [ "$mode" = "1" ]; then
            log "No matching PulseAudio cards (Pulse may not be running) — nothing to suspend."
        fi
        return 0
    fi
    local card
    while IFS= read -r card; do
        [ -z "$card" ] && continue
        if [ "$mode" = "1" ]; then
            log "Suspending PulseAudio card so VLAA can take ALSA exclusive: $card"
        else
            log "Resuming PulseAudio card: $card"
        fi
        pactl suspend-card "$card" "$mode" 2>/dev/null \
            || warn "pactl suspend-card $card $mode failed"
    done <<<"$cards"
}

# Max out ALSA PCM on the USB Composite speaker so direct hw playback (PyAudio)
# is not left at a low hardware level after Pulse releases the card.
max_speaker_alsa_pcm() {
    local card="${VLAA_ALSA_SPEAKER_CARD:-}"
    if [ -z "$card" ] && [ -r /proc/asound/cards ]; then
        card=$(awk '/USB Composite Device/ {print $1; exit}' /proc/asound/cards) || true
    fi
    if [ -z "$card" ]; then
        warn "Could not detect ALSA speaker card (no 'USB Composite Device' in /proc/asound/cards); skip amixer PCM."
        return 0
    fi
    if ! command -v amixer >/dev/null 2>&1; then
        warn "amixer not installed; skipping ALSA PCM max for card $card."
        return 0
    fi
    log "Setting ALSA card $card PCM to 100% (max) for USB speaker..."
    if amixer -c "$card" sset PCM 100% unmute 2>/dev/null; then
        log "ALSA card $card: PCM set to 100%."
    elif amixer -c "$card" sset PCM 312 unmute 2>/dev/null; then
        log "ALSA card $card: PCM set to max step."
    else
        warn "amixer failed for card $card (device busy or no PCM control); you can set VLAA_ALSA_SPEAKER_CARD or run amixer when VLAA is stopped."
    fi
}

PYPID=""
RAN_CLEANUP=0
cleanup() {
    [ "$RAN_CLEANUP" -eq 1 ] && return 0
    RAN_CLEANUP=1
    if [ -n "${PYPID:-}" ] && kill -0 "$PYPID" 2>/dev/null; then
        log "Terminating VLAA child (pid=$PYPID)..."
        kill -TERM "$PYPID" 2>/dev/null || true
        local i
        for i in $(seq 1 30); do
            kill -0 "$PYPID" 2>/dev/null || break
            sleep 0.1
        done
        kill -KILL "$PYPID" 2>/dev/null || true
    fi
    set_card_suspend 0
}

trap cleanup EXIT
trap 'cleanup; exit 143' TERM HUP

if [ ! -d "$VLAA_APP_ROBOTS" ]; then
    warn "VLAA_APP_ROBOTS not found: $VLAA_APP_ROBOTS"
    exit 1
fi

cd "$VLAA_APP_ROBOTS"

# Wait until the Blink500B2+ mic is enumerated by ALSA + PyAudio. This is the
# same gate the tmux script used to call inline; running it here keeps all
# pre-VLAA setup in one place.
if [ -f app_go2/setup/wait_for_blink500b2.sh ]; then
    log "Waiting for Blink500B2+ mic to be ready..."
    bash app_go2/setup/wait_for_blink500b2.sh
else
    warn "wait_for_blink500b2.sh not found under $VLAA_APP_ROBOTS/app_go2/setup; skipping."
fi

# A SIGKILL during the previous shutdown can leave the YunChen speaker's
# isochronous endpoint running with hw_ptr stuck. PipeWire still shows the
# sink as RUNNING, but nothing is played. Reset only in that case.
speaker_playback_stuck() {
    local card status state hw1 hw2
    card="$(awk '/USB Composite Device/ {print $1; exit}' /proc/asound/cards)"
    [ -n "$card" ] || return 1
    status="/proc/asound/card${card}/pcm0p/sub0/status"
    [ -r "$status" ] || return 1
    state="$(awk '/^state:/ {print $2}' "$status")"
    [ "$state" = "RUNNING" ] || return 1
    hw1="$(awk '/^hw_ptr/ {print $3}' "$status")"
    sleep 0.4
    hw2="$(awk '/^hw_ptr/ {print $3}' "$status")"
    [ "$hw1" = "$hw2" ]
}

reset_usb_speaker() {
    local dev vendor product name
    for dev in /sys/bus/usb/devices/*; do
        [ -f "${dev}/idVendor" ] || continue
        vendor="$(cat "${dev}/idVendor" 2>/dev/null || true)"
        product="$(cat "${dev}/idProduct" 2>/dev/null || true)"
        [ "$vendor" = "0909" ] && [ "$product" = "005b" ] || continue
        name="$(basename "$dev")"
        log "Resetting USB speaker ${name} (0909:005b)"
        if sudo sh -c "echo 0 > '${dev}/authorized' && sleep 0.4 && echo 1 > '${dev}/authorized'"; then
            sleep 1
            if systemctl --user restart pipewire pipewire-pulse pipewire-media-session 2>/dev/null; then
                log "Restarted PipeWire after speaker reset."
                sleep 1
            fi
            return 0
        fi
        warn "Could not reset USB speaker ${name}"
        return 1
    done
    warn "USB speaker 0909:005b not found; skip reset"
    return 0
}

if speaker_playback_stuck; then
    warn "USB speaker playback pointer is not moving; resetting it."
    reset_usb_speaker || true
else
    log "USB speaker playback is moving; not resetting."
fi

# Hand the cards over to VLAA, then start it. Background python so we keep a
# pid for clean shutdown via the EXIT/TERM traps above.
set_card_suspend 1

max_speaker_alsa_pcm

log "Starting VLAA: python3 main.py --robot-model go2"
python3 main.py --robot-model go2 &
PYPID=$!
wait "$PYPID" || true
