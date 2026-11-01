#!/usr/bin/env python3
"""
Wi-Fi Mesh Signal Monitor & Handover (HO) Tool for Unitree Go2
Location: /home/unitree/go2-nav/wifi/wifi_mesh.py

Fast single-channel (5180 MHz) real-time signal scanning.
Retains all 3 mesh nodes; dims out-of-range/very weak nodes in light gray.
Runs continuously by default (exit with Ctrl+C).
"""

import sys
import os
import subprocess
import time
import argparse
import re
import socket

UDP_PORT_DEFAULT = 9999
UNIX_SOCKET_PATH_DEFAULT = "/tmp/go2_wifi_mesh.sock"

class UnixSocketServer:
    def __init__(self, path=UNIX_SOCKET_PATH_DEFAULT):
        self.path = path
        self.server_sock = None
        self.clients = []
        self._setup_socket()

    def _setup_socket(self):
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
            self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_sock.bind(self.path)
            os.chmod(self.path, 0o777)
            self.server_sock.listen(5)
            self.server_sock.setblocking(False)
        except Exception:
            pass

    def accept_clients(self):
        if not self.server_sock:
            return
        try:
            while True:
                client_sock, _ = self.server_sock.accept()
                client_sock.settimeout(1.0)
                self.clients.append(client_sock)
        except Exception:
            pass

    def broadcast(self, frame_str):
        self.accept_clients()
        dead = []
        data = frame_str.encode('utf-8')
        for c in self.clients:
            try:
                c.sendall(data)
            except (BlockingIOError, socket.timeout):
                pass
            except Exception:
                dead.append(c)
        for c in dead:
            try:
                c.close()
            except Exception:
                pass
            if c in self.clients:
                self.clients.remove(c)

    def close(self):
        try:
            if self.server_sock:
                self.server_sock.close()
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass

def broadcast_udp_frame(frame_str, port=UDP_PORT_DEFAULT):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.sendto(frame_str.encode('utf-8'), ("127.0.0.1", port))
        sock.close()
    except Exception:
        pass

WPA_SOCKET = "/run/wpa_supplicant"
INTERFACE = "wlan0"
TARGET_SSID = "FCCLab"


def detect_interface():
    """Prefer the Wi-Fi interface that is already associated. The G1 dongle is not wlan0."""
    global INTERFACE
    out = run_cmd("iw dev")
    current = None
    connected = None
    names = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            current = stripped.split()[1]
            names.append(current)
        elif current and stripped.startswith("ssid "):
            connected = current
    INTERFACE = connected or (names[0] if names else INTERFACE)
    return INTERFACE


def wpa_ready():
    return os.path.exists(os.path.join(WPA_SOCKET, INTERFACE))


def quality_to_dbm(value):
    """NetworkManager reports 0–100. That scale is (dBm + 100) * 100 / 60, clamped."""
    quality = int(value)
    if quality < 0:
        return quality
    quality = max(0, min(100, quality))
    return int(round(quality * 0.6 - 100))

# FCCLab 5 GHz: one main Deco and three satellites. Channel follows the live beacon.
KNOWN_MESH_NODES = {
    "18:69:45:84:FB:EB": {"name": "Mesh Node 1 (Main)", "chan": "CH 44 (5 GHz)", "freq": "5220"},
    "18:69:45:84:FC:45": {"name": "Mesh Node 2 (Satellite 1)", "chan": "CH 44 (5 GHz)", "freq": "5220"},
    "18:69:45:84:FB:0D": {"name": "Mesh Node 3 (Satellite 2)", "chan": "CH 44 (5 GHz)", "freq": "5220"},
    "20:E1:5D:B6:6C:CF": {"name": "Mesh Node 4 (Satellite 3)", "chan": "CH 44 (5 GHz)", "freq": "5220"},
}

def run_cmd(cmd, timeout=2.5):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, timeout=timeout)
        return res.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""

def freq_to_chan(freq):
    try:
        f = int(freq)
        if 5170 <= f <= 5825:
            return f"CH {(f - 5000) // 5} (5 GHz)"
        elif 5955 <= f <= 7115:
            return f"CH {(f - 5950) // 5} (6 GHz)"
        elif 2412 <= f <= 2484:
            return f"CH {(f - 2407) // 5} (2.4 GHz)"
        return f"{f} MHz"
    except Exception:
        return str(freq)

def signal_bar(dbm_val, is_lost=False):
    if is_lost or dbm_val is None:
        return "\033[90m  N/A dBm  [░░░░░░░░]   0% (OUT OF RANGE)\033[0m"
    try:
        dbm = int(str(dbm_val).replace("dBm", "").strip())
        if dbm >= -50:
            quality = 100
            bar = "████████"
            color = "\033[92m"
            tag = ""
        elif dbm >= -65:
            quality = int(100 - (-(dbm + 50) * 2.5))
            bar = "██████░░"
            color = "\033[92m"
            tag = ""
        elif dbm >= -75:
            quality = int(60 - (-(dbm + 65) * 3))
            bar = "████░░░░"
            color = "\033[93m"
            tag = ""
        elif dbm >= -85:
            quality = int(30 - (-(dbm + 75) * 2))
            bar = "██░░░░░░"
            color = "\033[91m"
            tag = ""
        else:
            quality = max(2, int(10 - (-(dbm + 85) * 1.5)))
            bar = "█░░░░░░░"
            # Very weak signal: Dim in light gray
            color = "\033[90m"
            tag = " \033[90m(TOO WEAK)\033[0m"
        return f"{color}{dbm:4d} dBm  [{bar}] {quality:3d}%\033[0m{tag}"
    except Exception:
        return f"{dbm_val} dBm"

def get_current_status():
    iface = detect_interface()
    info = {}
    if wpa_ready():
        status_out = run_cmd(f"sudo wpa_cli -p {WPA_SOCKET} -i {iface} status")
        for line in status_out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        sig_poll = run_cmd(f"sudo wpa_cli -p {WPA_SOCKET} -i {iface} signal_poll")
        for line in sig_poll.splitlines():
            if line.startswith("RSSI="):
                info["link_signal"] = line.split("=")[1].strip()

    link_out = run_cmd(f"iw dev {iface} link")
    for line in link_out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Connected to "):
            info["bssid"] = stripped.split()[2]
        elif stripped.startswith("SSID:"):
            info["ssid"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("freq:"):
            info["freq"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("signal:"):
            sig_match = re.search(r"(-?\d+)\s*dBm", stripped)
            if sig_match:
                info["link_signal"] = sig_match.group(1)
        elif stripped.startswith("rx bitrate:"):
            info["rx_bitrate"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("tx bitrate:"):
            info["tx_bitrate"] = stripped.split(":", 1)[1].strip()

    ip_out = run_cmd(f"ip -4 -o addr show dev {iface}")
    ip_match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_out)
    if ip_match:
        info["ip_address"] = ip_match.group(1)
    return info


def scan_nmcli(ssid_filter):
    """Neighbor list from NetworkManager. BSSIDs are escaped as \\: in terse output."""
    raw = run_cmd("nmcli -t -f SSID,BSSID,FREQ,SIGNAL dev wifi")
    found = {}
    for line in raw.splitlines():
        escaped = line.replace("\\:", "\x1f")
        parts = escaped.split(":")
        if len(parts) < 4:
            continue
        ssid = parts[0]
        if ssid != ssid_filter:
            continue
        bssid = parts[1].replace("\x1f", ":").upper()
        freq = parts[2].split()[0]
        try:
            if int(freq) < 5000:
                continue
            signal = str(quality_to_dbm(parts[3]))
        except ValueError:
            continue
        found[bssid] = {"signal": signal, "age": 0, "freq": freq}
    return found

def scan_all_mesh_nodes(curr_status, ssid_filter=TARGET_SSID, five_ghz_only=True, passive=True):
    curr_freq = curr_status.get("freq", "5220")
    curr_bssid = curr_status.get("bssid", "").upper()

    # Refresh beacons on the channel the radio is actually using.
    run_cmd(f"sudo iw dev {INTERFACE} scan trigger freq {curr_freq} > /dev/null 2>&1", timeout=0.5)

    # Parse kernel iw scan dump for fresh over-the-air RSSIs (handles mesh alias MACs and bypasses stale associated cache)
    scanned_data = {}
    raw_dump = run_cmd(f"sudo iw dev {INTERFACE} scan dump")
    curr_bss = None
    is_assoc = False
    last_seen_ms = 0
    
    for line in raw_dump.splitlines():
        if line.startswith("BSS "):
            parts = line.split()
            curr_bss = parts[1].split("(")[0].upper()
            is_assoc = ("-- associated" in line)
            last_seen_ms = 0
        elif "last seen:" in line:
            match = re.search(r"(\d+)\s*ms", line)
            if match:
                last_seen_ms = int(match.group(1))
        elif "signal:" in line and curr_bss:
            try:
                sig = str(int(float(line.strip().split()[1])))
                suffix = curr_bss[2:]  # Match suffix 69:45:84:XX:XX
                for main_mac in KNOWN_MESH_NODES.keys():
                    if main_mac.endswith(suffix):
                        # For non-active nodes, prefer over-the-air beacon entries (is_assoc=False) over stale associated cache
                        if main_mac not in scanned_data or not is_assoc:
                            scanned_data[main_mac.upper()] = {
                                "signal": sig,
                                "age": max(0, int(last_seen_ms / 1000)),
                                "freq": curr_freq,
                            }
            except Exception:
                pass
            
    for mac, sample in scan_nmcli(ssid_filter).items():
        if mac not in scanned_data:
            scanned_data[mac] = sample

    # Fallback to wpa_cli BSS for any missing nodes
    for mac in KNOWN_MESH_NODES.keys():
        if mac.upper() not in scanned_data and wpa_ready():
            bss_out = run_cmd(f"sudo wpa_cli -p {WPA_SOCKET} -i {INTERFACE} BSS {mac.lower()}")
            level = None
            age = 999
            for line in bss_out.splitlines():
                if line.startswith("level="):
                    try:
                        level = line.split("=")[1].strip()
                    except Exception:
                        pass
                elif line.startswith("age="):
                    try:
                        age = int(line.split("=")[1].strip())
                    except Exception:
                        pass
            if level is not None and age <= 30:
                scanned_data[mac.upper()] = {
                    "signal": level,
                    "age": age,
                    "freq": curr_freq,
                }
            
    # Always assemble all 3 known mesh nodes
    bss_list = []
    
    for bssid, meta in KNOWN_MESH_NODES.items():
        is_active = (bssid == curr_bssid)
        node_name = meta["name"]
        node_freq = scanned_data.get(bssid, {}).get("freq") or (curr_status.get("freq") if curr_status.get("freq") else None)
        chan_str = freq_to_chan(node_freq) if node_freq else meta["chan"]
        
        if is_active:
            # Use fresh over-the-air beacon RSSI for consistent baseline across all nodes, fallback to active link_signal
            sig = scanned_data.get(bssid, {}).get("signal") or curr_status.get("link_signal", "-50")
            age_str = "0s ago (live)"
            is_lost = False
            state = "ACTIVE"
        elif bssid in scanned_data:
            sig = scanned_data[bssid]["signal"]
            age_val = scanned_data[bssid]["age"]
            age_str = f"{age_val}s ago"
            try:
                dbm = int(sig)
                if dbm < -85:
                    state = "WEAK"
                    is_lost = False
                else:
                    state = "CANDIDATE"
                    is_lost = False
            except Exception:
                state = "CANDIDATE"
                is_lost = False
        else:
            # Out of range / Not seen in passive beacon window
            sig = None
            age_str = "N/A"
            is_lost = True
            state = "LOST"
            
        bss_list.append({
            "bssid": bssid,
            "node_name": node_name,
            "chan": chan_str,
            "signal": sig,
            "age_str": age_str,
            "is_active": is_active,
            "is_lost": is_lost,
            "state": state
        })
    
    # Sort order: ACTIVE first, then strong CANDIDATES, then WEAK/LOST at bottom
    def sort_rank(item):
        if item["is_active"]:
            return 9999
        if item["is_lost"]:
            return -9999
        try:
            return int(item["signal"])
        except Exception:
            return -500
            
    bss_list.sort(key=sort_rank, reverse=True)
    return bss_list

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_ho.log")

def append_to_ho_logfile(plain_msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {plain_msg}\n")
    except Exception:
        pass

def ensure_sudo():
    """NetworkManager will not change the BSSID without root. Ask once per terminal."""
    if subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    print("\nWi-Fi roam needs sudo. Enter your password once.", flush=True)
    return subprocess.call(["sudo", "-v"]) == 0


def perform_roam(target_bssid, verbose=False):
    target_bssid = target_bssid.lower()
    if not re.fullmatch(r"[0-9a-f:]{17}", target_bssid):
        return False
    t_stamp = time.strftime("%H:%M:%S")
    target_name = KNOWN_MESH_NODES.get(target_bssid.upper(), {}).get("name", target_bssid)

    if verbose:
        print(f"\n\033[96m[*] [{t_stamp}] Triggering instant Handover (HO) to BSSID: {target_bssid} ({target_name})...\033[0m")

    iface = detect_interface()
    if wpa_ready():
        cmd = ["sudo", "-n", "wpa_cli", "-p", WPA_SOCKET, "-i", iface, "roam", target_bssid]
    else:
        # nmcli matches the scan list only when the BSSID is uppercase, and network-control is root-only here.
        cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", TARGET_SSID, "bssid", target_bssid.upper(), "ifname", iface]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
        out = (res.stdout or "").strip()
    except subprocess.TimeoutExpired:
        out = "roam timed out"
    except Exception as exc:
        out = str(exc)

    if verbose:
        print(f"\033[90m    roam response: {out}\033[0m")

    time.sleep(1.0)

    curr = get_current_status()
    success = (curr.get("bssid", "").lower() == target_bssid)
    if not success:
        perform_roam.last_error = out or "BSSID did not change"

    if verbose:
        if success:
            print(f"\033[92m[✓] Handover SUCCESS! Now connected to {target_name} ({target_bssid})\033[0m")
        else:
            print(f"\033[91m[!] Handover to {target_bssid} failed or pending. {perform_roam.last_error}\033[0m")

    return success


perform_roam.last_error = ""

def check_and_execute_auto_roam(bss_list, curr_status, threshold=-65, min_delta=8, ho_log_history=None):
    active_item = None
    best_candidate = None
    best_candidate_sig = -999

    for item in bss_list:
        if item["is_active"]:
            active_item = item
        elif not item["is_lost"] and item["signal"] is not None:
            try:
                sig_val = int(item["signal"])
                if sig_val > best_candidate_sig:
                    best_candidate_sig = sig_val
                    best_candidate = item
            except Exception:
                pass

    if not active_item or not best_candidate:
        return False, None, None

    try:
        active_sig = int(active_item["signal"])
    except Exception:
        return False, None, None

    delta = best_candidate_sig - active_sig

    # Check if:
    # 1. Active RSSI is worse than threshold (-65 dBm) AND best candidate is min_delta (8 dB) better
    # OR
    # 2. Candidate AP is dramatically stronger (delta >= 12 dB), e.g. standing right next to a new AP
    if (active_sig <= threshold and delta >= min_delta) or (delta >= 12):
        target_bssid = best_candidate["bssid"]
        target_name = best_candidate["node_name"]
        from_name = active_item["node_name"]
        short_from = re.sub(r'\s*\([^)]*\)', '', from_name).strip()
        short_target = re.sub(r'\s*\([^)]*\)', '', target_name).strip()
        t_stamp = time.strftime("%H:%M:%S")
        
        success = perform_roam(target_bssid, verbose=False)

        if success:
            event_str = f"\033[1;93m[{t_stamp}]\033[0m \033[92m{short_from}\033[0m \033[1;36m-->\033[0m \033[1;92m{short_target}\033[0m"
            plain_str = f"Auto-Roam: {short_from} ({active_sig} dBm) -> {short_target} ({best_candidate_sig} dBm) SUCCESS"
            append_to_ho_logfile(plain_str)

            if ho_log_history is not None:
                ho_log_history.append(event_str)

            return True, target_name, event_str

        reason = perform_roam.last_error.replace("\n", " ")[:120]
        event_str = f"\033[1;91m[{t_stamp}] {short_from} --> {short_target} failed: {reason}\033[0m"
        return False, target_name, event_str

    return False, None, None

def load_ho_history_from_file():
    history = []
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                for line in f.readlines():
                    line = line.strip()
                    if not line:
                        continue
                    m_auto = re.search(r"\[\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})\].*?(\bMesh Node \d).*?(?:->|➔)\s*(\bMesh Node \d)", line)
                    if m_auto:
                        t_str, n_a, n_b = m_auto.group(1), m_auto.group(2), m_auto.group(3)
                        history.append(f"\033[1;93m[{t_str}]\033[0m \033[92m{n_a}\033[0m \033[1;36m-->\033[0m \033[1;92m{n_b}\033[0m")
                    else:
                        m_man = re.search(r"\[\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})\].*?Manual Roam.*?(?:->|➔)\s*(\bMesh Node \d)", line)
                        if m_man:
                            t_str, n_b = m_man.group(1), m_man.group(2)
                            history.append(f"\033[1;93m[{t_str}]\033[0m \033[90mManual\033[0m \033[1;36m-->\033[0m \033[1;92m{n_b}\033[0m")
    except Exception:
        pass
    return history[-6:]

def get_last_roam_event_from_file():
    history = load_ho_history_from_file()
    if history:
        return history[-1]
    return None

def strip_ansi(text):
    return re.sub(r'\033\[[0-9;]*m', '', text)

def display_dashboard(ssid_filter=TARGET_SSID, five_ghz_only=True, auto_roam=False, threshold=-65, min_delta=8, last_roam_event=None, ho_log_history=None, udp_port=UDP_PORT_DEFAULT, unix_server=None):
    current = get_current_status()
    curr_bssid = current.get("bssid", "").upper()
    curr_ssid = current.get("ssid", "Unknown")
    curr_ip = current.get("ip_address", "Unknown")
    curr_rx = current.get("rx_bitrate", "N/A")
    curr_tx = current.get("tx_bitrate", "N/A")

    active_name = KNOWN_MESH_NODES.get(curr_bssid, {}).get("name", "Mesh Node")
    bss_list = scan_all_mesh_nodes(current, ssid_filter, five_ghz_only)

    lines = []
    lines.append("\033[1;36m" + "="*96)
    lines.append("       📶  UNITREE GO2 WI-FI 5GHz MESH SIGNAL & HANDOVER (HO) MONITOR")
    lines.append("="*96 + "\033[0m")
    
    auto_tag = f"\033[1;92mACTIVE\033[0m (Thresh: {threshold}dBm, MinDelta: +{min_delta}dB)" if auto_roam else "\033[90mDISABLED (--no-auto-roam)\033[0m"
    
    if last_roam_event:
        roam_event_tag = last_roam_event
    else:
        file_event = get_last_roam_event_from_file()
        if file_event:
            roam_event_tag = file_event
        else:
            roam_event_tag = f"\033[90m[Monitoring]\033[0m Connected to \033[92m{active_name}\033[0m"

    # Render Top Border Box for Latest Handover Event
    box_width = 92
    plain_title = "⚡ LATEST ROAMING HANDOVER EVENT:"
    plain_content = strip_ansi(roam_event_tag)
    pad_title = box_width - len(plain_title)
    pad_content = box_width - len(plain_content)

    lines.append("\033[1;93m┌" + "─"*(box_width + 2) + "┐")
    lines.append(f"│ \033[1;93m{plain_title}\033[0m" + " "*max(0, pad_title) + " \033[1;93m│")
    lines.append(f"│ \033[1m{roam_event_tag}\033[0m" + " "*max(0, pad_content) + " \033[1;93m│")
    lines.append("└" + "─"*(box_width + 2) + "┘\033[0m")

    lines.append(f"\033[1mActive Network:\033[0m  \033[92m{curr_ssid}\033[0m  |  \033[1mRobot IP:\033[0m {curr_ip}  |  \033[1mAuto-Roam:\033[0m {auto_tag}\033[K")
    lines.append(f"\033[1mActive BSSID:\033[0m    \033[1;93m{curr_bssid}\033[0m ({active_name})  |  \033[1mChannel:\033[0m {freq_to_chan(current.get('freq', '0'))}\033[K")
    lines.append(f"\033[1mLink Bitrate:\033[0m    RX: {curr_rx}  |  TX: {curr_tx}\033[K")
    lines.append("\033[36m" + "-"*96 + "\033[0m\033[K")
    
    lines.append(f"\n\033[1;37m{'STATE':<13} {'NODE / IDENTITY':<28} {'BSSID (MAC)':<20} {'BAND & CHANNEL':<16} {'SIGNAL STRENGTH':<24} {'LAST UPDATED':<12}\033[0m\033[K")
    lines.append("-" * 116 + "\033[K")

    for b in bss_list:
        bssid = b["bssid"]
        node_name = b["node_name"]
        chan_str = b["chan"]
        age_str = b["age_str"]
        
        if b["state"] == "ACTIVE":
            state_tag = "\033[1;92m[ACTIVE]\033[0m     "
            row_color = "\033[1;97m"
            bssid_color = "\033[1;97m"
            sig_str = signal_bar(b["signal"])
        elif b["state"] == "CANDIDATE":
            state_tag = "\033[97m CANDIDATE\033[0m  "
            row_color = "\033[37m"
            bssid_color = "\033[1m"
            sig_str = signal_bar(b["signal"])
        elif b["state"] == "WEAK":
            state_tag = "\033[90m [WEAK]\033[0m      "
            row_color = "\033[90m"
            bssid_color = "\033[90m"
            sig_str = signal_bar(b["signal"])
        else: # LOST / OUT OF RANGE
            state_tag = "\033[90m [OUT OF RANGE]\033[0m"
            row_color = "\033[90m"
            bssid_color = "\033[90m"
            sig_str = signal_bar(None, is_lost=True)
            
        lines.append(f"{state_tag:<22} {row_color}{node_name:<28}\033[0m {bssid_color}{bssid:<20}\033[0m \033[90m{chan_str:<16}\033[0m {sig_str}  \033[90m{age_str:<12}\033[0m\033[K")

    lines.append("\n\033[36m" + "="*88 + "\033[0m")
    lines.append("\033[1;33m📜 HANDOVER (HO) LOG HISTORY:\033[0m")
    if ho_log_history:
        for entry in reversed(ho_log_history[-6:]):
            lines.append(f"  • {entry}")
    else:
        lines.append("  \033[90mNo handovers recorded yet.\033[0m")

    lines.append("\033[36m" + "="*88 + "\033[0m")
    lines.append("\033[90m[Live 5GHz Fast Monitor... Press Ctrl+C to exit]\033[0m\n")

    frame_str = "\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J"
    print(frame_str, end="")
    sys.stdout.flush()

    broadcast_udp_frame(frame_str, udp_port)
    if unix_server:
        unix_server.broadcast(frame_str)

    return bss_list, current

def main():
    parser = argparse.ArgumentParser(description="Wi-Fi 5GHz Mesh Signal & Handover Tool")
    parser.add_argument("--ssid", default=TARGET_SSID, help="SSID filter (default: FCCLab)")
    parser.add_argument("--all-bands", action="store_true", help="Include 2.4GHz in scan results")
    parser.add_argument("--once", action="store_true", help="Run a single snapshot and exit")
    parser.add_argument("--interval", "-i", type=float, default=1.0, help="Refresh interval in seconds (default: 1.0)")
    parser.add_argument("--roam", "-r", type=str, help="Target BSSID to roam to immediately")
    parser.add_argument("--auto-roam", "-a", action="store_true", help="Roam to a stronger BSSID. This is already the default")
    parser.add_argument("--no-auto-roam", action="store_true", help="Stay on the current BSSID")
    parser.add_argument("--threshold", "-t", type=int, default=-65, help="RSSI threshold in dBm to trigger auto-roam (default: -65)")
    parser.add_argument("--min-delta", "-d", type=int, default=8, help="Minimum RSSI improvement in dB required to auto-roam (default: 8)")
    parser.add_argument("--cooldown", "-c", type=float, default=5.0, help="Minimum seconds between auto-roam triggers (default: 5.0)")
    parser.add_argument("--udp-port", type=int, default=UDP_PORT_DEFAULT, help="UDP IPC broadcast port (default: 9999)")
    parser.add_argument("--socket-path", type=str, default=UNIX_SOCKET_PATH_DEFAULT, help="Unix Domain Socket IPC path (default: /tmp/go2_wifi_mesh.sock)")
    args = parser.parse_args()
    auto_roam = not args.no_auto_roam

    unix_server = UnixSocketServer(args.socket_path)
    ho_log_history = load_ho_history_from_file()

    if auto_roam or args.roam:
        if not wpa_ready():
            ensure_sudo()

    if args.roam:
        perform_roam(args.roam, verbose=True)
        return

    five_ghz_only = not args.all_bands

    if args.once:
        display_dashboard(args.ssid, five_ghz_only, auto_roam, args.threshold, args.min_delta, None, ho_log_history, args.udp_port, unix_server)
        return

    last_roam_time = 0
    last_roam_event = get_last_roam_event_from_file()

    # Clear terminal screen ONCE at startup to prevent text window flashing
    print("\033[2J\033[H", end="")

    try:
        while True:
            bss_list, curr_status = display_dashboard(args.ssid, five_ghz_only, auto_roam, args.threshold, args.min_delta, last_roam_event, ho_log_history, args.udp_port, unix_server)

            if auto_roam and (time.time() - last_roam_time >= args.cooldown):
                roamed, target, event_str = check_and_execute_auto_roam(bss_list, curr_status, args.threshold, args.min_delta, ho_log_history)
                if event_str:
                    last_roam_time = time.time()
                    last_roam_event = event_str
                if roamed:
                    time.sleep(1.0)
            
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n\033[92m[✓] Exited monitor cleanly.\033[0m")
    finally:
        unix_server.close()

if __name__ == "__main__":
    main()
