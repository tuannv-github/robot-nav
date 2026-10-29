#!/usr/bin/env python3
"""G1 controller bridge: HTTP REST and the operator console.

Same role as go2_controller_bridge. Stick and /cmd_vel holds call
LocoClient.SetVelocity; sport and arm routes call the Unitree SDK2 clients.
Console: http://0.0.0.0:<port>/console/
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import math
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime

SPORT_ACTIONS = {
    "zero-torque": {"name": "Zero Torque", "fsm_id": 0},
    "damping": {"name": "Damping", "fsm_id": 1},
    "position-control-squat": {"name": "Position Control Squat", "fsm_id": 2},
    "position-control-sit-down": {"name": "Position Control Sit Down", "fsm_id": 3},
    "lock-standing": {"name": "Lock Standing", "fsm_id": 4},
    "squat-stand": {"name": "Squat Stand", "fsm_id": 706},
    "balance-squat": {"name": "Balance Squat", "fsm_id": 706},
    "lie-down-stand-up": {"name": "Lie Down, Stand Up", "fsm_id": 702},
    "walk-motion": {"name": "Walk Motion", "fsm_id": 500},
    "walk-motion-3dof-waist": {"name": "Walk Motion-3Dof-waist", "fsm_id": 501},
    "run": {"name": "Run", "fsm_id": 801},
    "low-stand": {"name": "low stand", "kind": "stand_height", "value": 0},
    "high-stand": {"name": "high stand", "kind": "stand_height", "value": (1 << 32) - 1},
    "stop-move": {"name": "stop move", "kind": "stop"},
    "wave-hand": {"name": "wave hand", "kind": "wave"},
    "shake-hand": {"name": "shake hand", "kind": "shake"},
}

FSM_NAMES = {
    0: "Zero Torque",
    1: "Damping",
    2: "Position Control Squat",
    3: "Position Control Sit Down",
    4: "Lock Standing",
    706: "Squat Stand",
    702: "Lie Down, Stand Up",
    500: "Walk Motion",
    501: "Walk Motion-3Dof-waist",
    801: "Run",
    802: "Run",
}

LINEAR_SCALE = 0.6
YAW_SCALE = 1.0
HTTP_CLIENT_SEC = 3.0
HOLD_TIMEOUT_SEC = 0.45
HOLD_PERIOD_SEC = 0.2
VELOCITY_DURATION_SEC = 0.5
ODOM_STALE_SEC = 1.0
ODOM_SOURCES = (
    {"id": "pelvis", "name": "Pelvis", "topic": "rt/state_estimator/odom_pelvis", "kind": "nav"},
    {"id": "torso", "name": "Torso", "topic": "rt/state_estimator/odom_torso", "kind": "nav"},
    {"id": "odom", "name": "Odom mode", "topic": "rt/odommodestate", "kind": "sport"},
    {"id": "fusion", "name": "Fusion", "topic": "rt/state_estimator/fusion_odom", "kind": "nav"},
    {"id": "fusion-torso", "name": "Fusion torso", "topic": "rt/state_estimator/fusion_odom_torso", "kind": "nav"},
)


def _vec3(x, y, z) -> list[float]:
    return [float(x), float(y), float(z)]


def _rpy_from_quat(x: float, y: float, z: float, w: float) -> list[float]:
    sin_roll = 2.0 * (w * x + y * z)
    cos_roll = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sin_roll, cos_roll)
    sin_pitch = 2.0 * (w * y - z * x)
    if abs(sin_pitch) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sin_pitch)
    else:
        pitch = math.asin(sin_pitch)
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(sin_yaw, cos_yaw)
    return [roll, pitch, yaw]


class OdomFeed:
    """Latest sample from each G1 odometry topic. Read-only DDS subscribers."""

    def __init__(self):
        self.error = ""
        self._lock = threading.Lock()
        self._slots = {
            spec["id"]: {
                "sample": None,
                "mono": 0.0,
                "count": 0,
                "hz": 0.0,
                "hz_stamp": time.monotonic(),
                "hz_count": 0,
            }
            for spec in ODOM_SOURCES
        }
        self._subs = []

    def start(self) -> None:
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

        for spec in ODOM_SOURCES:
            msg_type = Odometry_ if spec["kind"] == "nav" else SportModeState_
            sub = ChannelSubscriber(spec["topic"], msg_type)

            def on_sample(msg, source_id=spec["id"], kind=spec["kind"]):
                self._store(source_id, kind, msg)

            sub.Init(on_sample, 1)
            self._subs.append(sub)

    def _store(self, source_id: str, kind: str, msg) -> None:
        try:
            sample = self._parse(kind, msg)
        except Exception:
            return
        now = time.monotonic()
        with self._lock:
            slot = self._slots.get(source_id)
            if slot is None:
                return
            slot["sample"] = sample
            slot["mono"] = now
            slot["count"] += 1
            window = now - slot["hz_stamp"]
            if window >= 1.0:
                slot["hz"] = (slot["count"] - slot["hz_count"]) / window
                slot["hz_stamp"] = now
                slot["hz_count"] = slot["count"]

    def _parse(self, kind: str, msg) -> dict:
        if kind == "nav":
            position = msg.pose.pose.position
            quat = msg.pose.pose.orientation
            linear = msg.twist.twist.linear
            angular = msg.twist.twist.angular
            return {
                "frame_id": str(msg.header.frame_id or ""),
                "child_frame_id": str(msg.child_frame_id or ""),
                "position": _vec3(position.x, position.y, position.z),
                "rpy": _rpy_from_quat(quat.x, quat.y, quat.z, quat.w),
                "linear": _vec3(linear.x, linear.y, linear.z),
                "angular": _vec3(angular.x, angular.y, angular.z),
            }
        rpy = msg.imu_state.rpy
        return {
            "frame_id": "",
            "child_frame_id": "",
            "position": _vec3(msg.position[0], msg.position[1], msg.position[2]),
            "rpy": _vec3(rpy[0], rpy[1], rpy[2]),
            "linear": _vec3(msg.velocity[0], msg.velocity[1], msg.velocity[2]),
            "angular": _vec3(0.0, 0.0, msg.yaw_speed),
        }

    def snapshot(self) -> dict:
        now = time.monotonic()
        sources = []
        with self._lock:
            for spec in ODOM_SOURCES:
                slot = self._slots[spec["id"]]
                age = None
                live = False
                if slot["mono"]:
                    age = max(0.0, now - slot["mono"])
                    live = age <= ODOM_STALE_SEC
                row = {
                    "id": spec["id"],
                    "name": spec["name"],
                    "topic": spec["topic"],
                    "live": live,
                    "hz": round(float(slot["hz"]), 1),
                    "age_ms": None if age is None else int(age * 1000),
                    "frame_id": "",
                    "child_frame_id": "",
                    "position": None,
                    "rpy": None,
                    "linear": None,
                    "angular": None,
                }
                if slot["sample"]:
                    row.update(slot["sample"])
                    row["live"] = live
                sources.append(row)
        return {"ok": True, "sources": sources, "error": self.error}


def _config_db_path() -> str:
    override = os.environ.get("G1_CONFIG_DB", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".g1-controller", "config.db")


def _action_log_path() -> str:
    override = os.environ.get("G1_ACTION_LOG", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".g1-controller", "actions.jsonl")


class ActionLog:
    """Append-only record of operator commands, also kept for the console tab."""

    def __init__(self, path: str, limit: int = 500):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._seq = 0
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-self.limit:]
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or "id" not in item:
                continue
            self._seq = max(self._seq, int(item["id"]))
            self._items.append(item)

    def record(
        self,
        kind: str,
        name: str,
        *,
        ok: bool = True,
        detail: str = "",
        source: str = "",
    ) -> dict:
        with self._lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "ts": time.time(),
                "iso": datetime.now().astimezone().isoformat(timespec="seconds"),
                "kind": kind,
                "name": name,
                "ok": bool(ok),
                "detail": detail,
                "source": source,
            }
            self._items.append(item)
            if len(self._items) > self.limit:
                self._items = self._items[-self.limit:]
            line = json.dumps(item, separators=(",", ":")) + "\n"
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                if os.path.getsize(self.path) > 2_000_000:
                    with open(self.path, "w", encoding="utf-8") as handle:
                        for saved in self._items:
                            handle.write(json.dumps(saved, separators=(",", ":")) + "\n")
            except OSError as exc:
                item = dict(item)
                item["file_error"] = str(exc)
            return dict(item)

    def since(self, after: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = [dict(item) for item in self._items if int(item.get("id") or 0) > after]
        if len(rows) > limit:
            rows = rows[-limit:]
        return rows


def _slug(name: str) -> str:
    out = []
    prev_dash = False
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _console_web_dir() -> str | None:
    override = os.environ.get("G1_CONSOLE_DIR", "").strip()
    if override and os.path.isfile(os.path.join(override, "index.html")):
        return override
    here = os.path.abspath(os.path.dirname(__file__))
    candidates = [
        os.path.join(here, "..", "console", "web"),
        os.path.join(here, "..", "..", "share", "g1_controller", "console", "web"),
    ]
    for candidate in candidates:
        path = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(path, "index.html")):
            return path
    return None


def _detect_interface(preferred: str) -> str:
    if preferred and preferred != "auto":
        return preferred
    try:
        import socket
        import fcntl
        import struct

        for iface in os.listdir("/sys/class/net"):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                packed = fcntl.ioctl(
                    sock.fileno(),
                    0x8915,
                    struct.pack("256s", iface.encode()[:15]),
                )
                ip = socket.inet_ntoa(packed[20:24])
            except OSError:
                continue
            if ip.startswith("192.168.123."):
                return iface
    except OSError:
        pass
    return "enP8p1s0"


_TCP_ENDPOINT = re.compile(r"^tcp://[^/\s:]+:\d{1,5}$")


def _as_bool(value, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} enabled must be true or false")


def _default_interfaces() -> dict:
    http_port = int(os.environ.get("G1_REST_PORT", "8081"))
    zmq_port = int(os.environ.get("G1_ZMQ_PORT", "8082"))
    return {
        "http": {"enabled": True, "host": "0.0.0.0", "port": http_port},
        "ws": {"enabled": True, "path": "/control"},
        "zmq": {"enabled": True, "endpoint": f"tcp://0.0.0.0:{zmq_port}"},
    }


class HttpClients:
    """Distinct HTTP clients that have called the controller in the last few seconds."""

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, client) -> None:
        if not client:
            return
        host = str(client[0])
        if not host:
            return
        now = time.time()
        with self._lock:
            self._seen[host] = now
            self._drop(now)

    def count(self) -> int:
        now = time.time()
        with self._lock:
            self._drop(now)
            return len(self._seen)

    def _drop(self, now: float) -> None:
        stale = [host for host, seen in self._seen.items() if now - seen > HTTP_CLIENT_SEC]
        for host in stale:
            del self._seen[host]


class InterfaceStore:
    """SQLite record of the HTTP, WebSocket, and ZMQ control interfaces."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS interfaces (
                    name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER,
                    path TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def load(self) -> dict:
        with self._lock:
            with self._connect() as db:
                rows = db.execute("SELECT name FROM interfaces").fetchall()
                if not rows:
                    self._write(db, _default_interfaces())
            return self._read()

    def save(self, incoming: dict) -> dict:
        if not isinstance(incoming, dict):
            raise ValueError("interfaces must be an object")
        saved = self._validate(incoming)
        with self._lock:
            with self._connect() as db:
                self._write(db, saved)
        return saved

    def _validate(self, incoming: dict) -> dict:
        missing = [name for name in ("http", "ws", "zmq") if name not in incoming]
        if missing:
            raise ValueError("Missing " + ", ".join(missing))
        http = incoming["http"] if isinstance(incoming["http"], dict) else {}
        ws = incoming["ws"] if isinstance(incoming["ws"], dict) else {}
        zmq = incoming["zmq"] if isinstance(incoming["zmq"], dict) else {}
        if not _as_bool(http.get("enabled", True), "HTTP"):
            raise ValueError("HTTP stays on because the console uses it")
        host = str(http.get("host") or "").strip()
        if not host or any(ch.isspace() for ch in host):
            raise ValueError("HTTP host is required")
        try:
            port = int(http.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValueError("HTTP port must be a number") from exc
        if port < 1 or port > 65535:
            raise ValueError("HTTP port must be from 1 to 65535")
        path = str(ws.get("path") or "").strip()
        if not path.startswith("/") or any(ch.isspace() for ch in path):
            raise ValueError("WebSocket path must start with /")
        endpoint = str(zmq.get("endpoint") or "").strip()
        if not _TCP_ENDPOINT.match(endpoint):
            raise ValueError("ZMQ endpoint must look like tcp://0.0.0.0:8082")
        zmq_port = int(endpoint.rsplit(":", 1)[1])
        if zmq_port < 1 or zmq_port > 65535:
            raise ValueError("ZMQ port must be from 1 to 65535")
        if zmq_port == port:
            raise ValueError("ZMQ port must be different from the HTTP port")
        return {
            "http": {"enabled": True, "host": host, "port": port},
            "ws": {"enabled": _as_bool(ws.get("enabled", True), "WebSocket"), "path": path},
            "zmq": {"enabled": _as_bool(zmq.get("enabled", True), "ZMQ"), "endpoint": endpoint},
        }

    def _write(self, db: sqlite3.Connection, cfg: dict) -> None:
        now = time.time()
        rows = (
            ("http", 1, cfg["http"]["host"], cfg["http"]["port"], "", ""),
            ("ws", int(bool(cfg["ws"]["enabled"])), "", None, cfg["ws"]["path"], ""),
            ("zmq", int(bool(cfg["zmq"]["enabled"])), "", None, "", cfg["zmq"]["endpoint"]),
        )
        for name, enabled, host, port, path, endpoint in rows:
            db.execute(
                """
                INSERT INTO interfaces (name, enabled, host, port, path, endpoint, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled = excluded.enabled,
                    host = excluded.host,
                    port = excluded.port,
                    path = excluded.path,
                    endpoint = excluded.endpoint,
                    updated_at = excluded.updated_at
                """,
                (name, enabled, host, port, path, endpoint, now),
            )
        db.commit()

    def _read(self) -> dict:
        with self._connect() as db:
            found = {
                row["name"]: row
                for row in db.execute("SELECT * FROM interfaces").fetchall()
            }
        defaults = _default_interfaces()
        cfg = {}
        for name in ("http", "ws", "zmq"):
            row = found.get(name)
            base = defaults[name]
            if row is None:
                cfg[name] = dict(base)
                continue
            if name == "http":
                cfg[name] = {
                    "enabled": True,
                    "host": row["host"] or base["host"],
                    "port": int(row["port"] if row["port"] is not None else base["port"]),
                }
            elif name == "ws":
                cfg[name] = {
                    "enabled": bool(row["enabled"]),
                    "path": row["path"] or base["path"],
                }
            else:
                cfg[name] = {
                    "enabled": bool(row["enabled"]),
                    "endpoint": row["endpoint"] or base["endpoint"],
                }
        return cfg


SCALE_LIMITS = {
    "move": (0.0, 2.0),
    "turn": (0.0, 2.0),
    "speed": (0.0, 0.6),
}
SCALE_DEFAULTS = {"move": 0.35, "turn": 1.0, "speed": 0.2}


def _scale_value(raw, name: str) -> float:
    low, high = SCALE_LIMITS[name]
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    if number < low or number > high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
    return number


class ScaleStore:
    """Move, turn, and sport speed scales, kept in the controller database."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS scale (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    move REAL NOT NULL,
                    turn REAL NOT NULL,
                    speed REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def load(self) -> dict:
        with self._lock:
            with self._connect() as db:
                row = db.execute("SELECT move, turn, speed FROM scale WHERE id = 1").fetchone()
                if row is None:
                    self._write(db, SCALE_DEFAULTS)
                    return dict(SCALE_DEFAULTS)
                return {
                    "move": float(row["move"]),
                    "turn": float(row["turn"]),
                    "speed": float(row["speed"]),
                }

    def save(self, incoming: dict) -> dict:
        if not isinstance(incoming, dict):
            raise ValueError("scale must be an object")
        saved = {
            "move": _scale_value(incoming.get("move", SCALE_DEFAULTS["move"]), "move"),
            "turn": _scale_value(incoming.get("turn", SCALE_DEFAULTS["turn"]), "turn"),
            "speed": _scale_value(incoming.get("speed", SCALE_DEFAULTS["speed"]), "speed"),
        }
        with self._lock:
            with self._connect() as db:
                self._write(db, saved)
        return saved

    def _write(self, db: sqlite3.Connection, cfg: dict) -> None:
        db.execute(
            """
            INSERT INTO scale (id, move, turn, speed, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                move = excluded.move,
                turn = excluded.turn,
                speed = excluded.speed,
                updated_at = excluded.updated_at
            """,
            (cfg["move"], cfg["turn"], cfg["speed"], time.time()),
        )
        db.commit()


class CameraStore:
    """Per-topic camera controls in the same SQLite DB as interfaces/scale."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS camera (
                    path TEXT PRIMARY KEY,
                    settings TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _sanitize(settings: dict) -> dict:
        keys = ("zoom", "focus", "focus_auto", "exposure", "gain", "exposure_auto")
        clean = {}
        for key in keys:
            if key not in settings or settings[key] is None:
                continue
            try:
                clean[key] = int(settings[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be an integer") from exc
        return clean

    def load(self, topic: str) -> dict:
        name = str(topic or "").strip().strip("/")
        with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT settings FROM camera WHERE path = ?",
                    (name,),
                ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row["settings"])
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return self._sanitize(payload)

    def load_all(self) -> dict:
        with self._lock:
            with self._connect() as db:
                rows = db.execute("SELECT path, settings FROM camera").fetchall()
        out = {}
        for row in rows:
            try:
                payload = json.loads(row["settings"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                out[str(row["path"])] = self._sanitize(payload)
        return out

    def merge(self, topic: str, updates: dict) -> dict:
        name = str(topic or "").strip().strip("/")
        if not name:
            raise ValueError("camera topic is required")
        if not isinstance(updates, dict):
            raise ValueError("camera settings must be an object")
        patch = self._sanitize(updates)
        if not patch:
            raise ValueError("no camera settings to save")
        with self._lock:
            current = {}
            with self._connect() as db:
                row = db.execute(
                    "SELECT settings FROM camera WHERE path = ?",
                    (name,),
                ).fetchone()
                if row is not None:
                    try:
                        payload = json.loads(row["settings"])
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    if isinstance(payload, dict):
                        current = self._sanitize(payload)
                current.update(patch)
                db.execute(
                    """
                    INSERT INTO camera (path, settings, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        settings = excluded.settings,
                        updated_at = excluded.updated_at
                    """,
                    (name, json.dumps(current, sort_keys=True), time.time()),
                )
                db.commit()
        return current


class ZmqInterface:
    """PULL socket. Clients PUSH the same JSON control messages used on the WebSocket."""

    def __init__(self, robot: "G1Controller"):
        self.robot = robot
        self.endpoint = ""
        self.running = False
        self.error = ""
        self._peers: set[int] = set()
        self._peer_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._mon = None
        self._thread: threading.Thread | None = None
        self._ctx = None
        self._sock = None

    def apply_config(self, cfg: dict) -> None:
        enabled = bool(cfg.get("enabled"))
        endpoint = str(cfg.get("endpoint") or "")
        if self.running and self.endpoint == endpoint and enabled and not self.error:
            return
        self.close()
        self.endpoint = endpoint
        self.error = ""
        self.running = False
        if not enabled:
            return
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(endpoint,), name="g1-zmq", daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def close(self) -> None:
        self._stop.set()
        sock = self._sock
        mon = self._mon
        ctx = self._ctx
        self._sock = None
        self._mon = None
        self._ctx = None
        if mon is not None:
            try:
                mon.close(linger=0)
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close(linger=0)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.term()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1.0)
        self._thread = None
        self.running = False
        with self._peer_lock:
            self._peers.clear()

    def connections(self) -> int:
        with self._peer_lock:
            return len(self._peers)

    def _note_peer(self, event: int, value: int, zmq) -> None:
        connected = (zmq.EVENT_ACCEPTED,)
        gone = (zmq.EVENT_DISCONNECTED, zmq.EVENT_CLOSED)
        if value <= 0:
            return
        with self._peer_lock:
            if event in connected:
                self._peers.add(value)
            elif event in gone:
                self._peers.discard(value)

    def _read_monitor(self, mon, zmq) -> None:
        from zmq.utils.monitor import recv_monitor_message

        while True:
            try:
                msg = recv_monitor_message(mon, flags=zmq.NOBLOCK)
            except zmq.Again:
                return
            except Exception:
                return
            try:
                value = int(msg.get("value") or 0)
            except (TypeError, ValueError):
                value = 0
            self._note_peer(int(msg.get("event") or 0), value, zmq)

    def _run(self, endpoint: str) -> None:
        try:
            import zmq
        except ImportError:
            self.error = "pyzmq is not installed"
            self.running = False
            self._ready.set()
            return
        ctx = zmq.Context()
        sock = ctx.socket(zmq.PULL)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 200)
        monitor = "inproc://g1-zmq-%s" % id(sock)
        flags = zmq.EVENT_ACCEPTED | zmq.EVENT_DISCONNECTED | zmq.EVENT_CLOSED
        if hasattr(zmq, "EVENT_HANDSHAKE_SUCCEEDED"):
            flags |= zmq.EVENT_HANDSHAKE_SUCCEEDED
        try:
            sock.monitor(monitor, flags)
        except Exception:
            monitor = ""
        try:
            sock.bind(endpoint)
        except Exception as exc:
            self.error = str(exc)
            self.running = False
            self._ready.set()
            try:
                sock.close(linger=0)
            except Exception:
                pass
            ctx.term()
            return
        self._ctx = ctx
        self._sock = sock
        mon = None
        if monitor:
            mon = ctx.socket(zmq.PAIR)
            try:
                mon.connect(monitor)
            except Exception:
                mon.close(linger=0)
                mon = None
        self._mon = mon
        self.running = True
        self.error = ""
        self._ready.set()
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        if mon is not None:
            poller.register(mon, zmq.POLLIN)
        while not self._stop.is_set():
            try:
                ready = dict(poller.poll(200))
            except Exception:
                break
            if mon is not None and mon in ready:
                self._read_monitor(mon, zmq)
            if sock not in ready:
                continue
            try:
                raw = sock.recv(zmq.NOBLOCK)
            except zmq.Again:
                continue
            except Exception:
                break
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(body, dict):
                continue
            hub = getattr(self.robot, "control_hub", None)
            if hub is None:
                continue
            try:
                hub.apply(body, "zmq")
            except ValueError:
                continue
        self.running = False


JS_EVENT_FMT = "<IhBB"
JS_EVENT_SIZE = struct.calcsize(JS_EVENT_FMT)
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02


class LocalJoystick:
    """Read /dev/input/js* on this machine and feed the same stick hold as POST /wireless.

    The browser Gamepad API is silent when the tab is not focused. A local device
    is not.
    """

    def __init__(self, robot: "G1Controller"):
        self.robot = robot
        self.path = ""
        self.error = ""
        self._grab = False
        self._fd = -1
        self._kind = "js"
        self._axes = [0.0] * 8
        self._axis_n = 0
        self._scale = {"move": 0.35, "turn": 1.0}
        self._scale_ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="g1-js", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.set_grab(False)
        self._close()

    def set_grab(self, grab: bool) -> None:
        with self._lock:
            was = self._grab
            self._grab = bool(grab)
        if was and not grab:
            self.robot.stop_motion(source="js")

    def status(self) -> dict:
        with self._lock:
            return {
                "grab": self._grab,
                "path": self.path or "",
                "error": self.error,
            }

    def _close(self) -> None:
        fd = self._fd
        self._fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open(self) -> bool:
        if self._fd >= 0:
            return True
        paths = sorted(glob.glob("/dev/input/js*")) + self._event_paths()
        if not paths:
            self.error = "No local joystick (plug it into this computer)"
            self.path = ""
            return False
        last = "open failed"
        for path in paths:
            try:
                self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                last = str(exc)
                continue
            self.path = path
            self._kind = "evdev" if "event" in os.path.basename(path) else "js"
            self.error = ""
            self._axes = [0.0] * 8
            self._axis_n = 0
            return True
        self.error = last
        self.path = ""
        return False

    def _event_paths(self) -> list:
        paths = []
        try:
            text = open("/proc/bus/input/devices", encoding="utf-8", errors="replace").read()
        except OSError:
            return paths
        block = {}
        for line in text.splitlines() + [""]:
            if not line.strip():
                name = (block.get("N") or "").lower()
                handlers = block.get("H") or ""
                bits = block.get("ABS") or ""
                if bits and any(word in name for word in ("js", "joy", "gamepad", "xbox", "sony", "wireless controller", "8bitdo")):
                    for part in handlers.split():
                        if part.startswith("event"):
                            paths.append("/dev/input/" + part)
                block = {}
                continue
            if line[1:2] == ":":
                block[line[0]] = line[3:].strip()
            elif line.startswith("B: ABS="):
                block["ABS"] = line.split("=", 1)[-1]
        return paths

    def _axis(self, index: int) -> float:
        if index >= len(self._axes):
            return 0.0
        return self._axes[index]

    def _read(self) -> None:
        if self._fd < 0:
            return
        size = 24 if self._kind == "evdev" else JS_EVENT_SIZE
        while True:
            try:
                raw = os.read(self._fd, size)
            except BlockingIOError:
                return
            except OSError as exc:
                self.error = str(exc)
                self._close()
                return
            if len(raw) != size:
                return
            if self._kind == "evdev":
                _sec, _usec, ev_type, code, value = struct.unpack("<QQHHi", raw)
                if ev_type != 3 or code >= len(self._axes):
                    continue
                self._axes[code] = max(-1.0, min(1.0, value / 32767.0))
                if code > self._axis_n:
                    self._axis_n = code
                continue
            _ts, value, kind, number = struct.unpack(JS_EVENT_FMT, raw)
            kind &= ~0x80
            if kind == JS_EVENT_AXIS and number < len(self._axes):
                self._axes[number] = max(-1.0, min(1.0, value / 32767.0))
                if number > self._axis_n:
                    self._axis_n = number

    def _loop(self) -> None:
        last_sig = None
        while not self._stop.wait(0.02):
            with self._lock:
                grab = self._grab
            if not grab:
                last_sig = None
                if self._fd >= 0:
                    self._close()
                continue
            if not self._open():
                continue
            self._read()
            lx = self._axis(0)
            ly = -self._axis(1)
            rx = self._axis(3) if self._axis_n >= 3 else self._axis(2)
            now = time.time()
            if now - self._scale_ts > 0.5:
                self._scale = self.robot.scales.load()
                self._scale_ts = now
            move = float(self._scale.get("move") or 0.35)
            turn = float(self._scale.get("turn") or 1.0)
            out = (lx * move, ly * move, rx * turn)
            active = max(abs(out[0]), abs(out[1]), abs(out[2])) > 0.0001
            sig = tuple(round(v, 3) for v in out) if active else ("stop",)
            if sig == last_sig and not active:
                continue
            last_sig = sig
            if active:
                self.robot.set_stick(out[0], out[1], out[2], source="js")
            elif self.robot._hold.get("mode") == "stick":
                self.robot.stop_motion(source="js")


class G1Controller:
    def __init__(self, network_interface: str):
        self.network_interface = network_interface
        self.actions = ActionLog(_action_log_path())
        db_path = _config_db_path()
        self.interfaces = InterfaceStore(db_path)
        self.scales = ScaleStore(db_path)
        self.cameras = CameraStore(db_path)
        self.http_clients = HttpClients()
        self.local_js = LocalJoystick(self)
        self.zmq_iface = ZmqInterface(self)
        self.bind_host = ""
        self.bind_port = 0
        self._drive_sig = None
        self._vel_fault = ""
        self.ready = False
        self.last_error = ""
        self.client = None
        self.arm_client = None
        self.arm_actions = []
        self.odom = OdomFeed()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._hold = {
            "lx": 0.0,
            "ly": 0.0,
            "rx": 0.0,
            "vx": 0.0,
            "vy": 0.0,
            "w": 0.0,
            "mode": "idle",
            "ts": 0.0,
        }
        self._moving = False
        self._gait = False
        self._thread = threading.Thread(target=self._hold_loop, name="g1-drive", daemon=True)
        self._fsm_lock = threading.Lock()
        self._fsm_state = {
            "up": False,
            "fsm_id": None,
            "fsm_name": None,
            "error": "",
            "checked_at": 0.0,
        }
        self._fsm_thread = threading.Thread(target=self._fsm_loop, name="g1-fsm", daemon=True)

    def start(self) -> None:
        import unitree_sdk2py.core.channel as channel
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.arm.g1_arm_action_client import (
            G1ArmActionClient,
            action_map,
        )
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        log_path = os.environ.get(
            "G1_CDDS_LOG",
            os.path.join(os.path.expanduser("~"), ".g1-cdds.log"),
        )
        channel.ChannelConfigHasInterface = channel.ChannelConfigHasInterface.replace(
            "/tmp/cdds.LOG",
            log_path,
        )
        ChannelFactoryInitialize(0, self.network_interface)
        self.client = LocoClient()
        self.client.SetTimeout(10.0)
        self.client.Init()
        self.arm_client = G1ArmActionClient()
        self.arm_client.SetTimeout(10.0)
        self.arm_client.Init()
        self.arm_actions = self._load_arm_actions(action_map)
        try:
            self.odom.start()
        except Exception as exc:
            self.odom.error = str(exc)
        self.ready = True
        self.actions.record(
            "system",
            "Controller ready",
            detail=self.network_interface,
            source="bridge",
        )
        self._fsm_thread.start()
        self._thread.start()
        self.local_js.start()

    def _load_arm_actions(self, action_map: dict) -> list:
        actions = [
            {"id": int(action_id), "name": name, "slug": _slug(name)}
            for name, action_id in action_map.items()
        ]
        try:
            code, data = self.arm_client.GetActionList()
        except Exception:
            return actions
        if code != 0 or not isinstance(data, dict):
            return actions
        raw = data.get("data", data.get("actions"))
        if not isinstance(raw, list):
            return actions
        live = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            action_id = item.get("id", item.get("action_id"))
            name = item.get("name") or item.get("action_name")
            if action_id is None or not name:
                continue
            live.append({"id": int(action_id), "name": str(name), "slug": _slug(str(name))})
        return live or actions

    def shutdown(self) -> None:
        self._stop.set()
        self.local_js.shutdown()
        self.zmq_iface.close()
        self.actions.record("system", "Controller stopping", source="bridge")
        self.stop_motion(record=False)

    def apply_interface_config(self, cfg: dict) -> bool:
        """Apply saved interface settings. Port and path changes need a restart."""
        hub = getattr(self, "control_hub", None)
        if hub is not None:
            hub.enabled = bool(cfg["ws"]["enabled"])
        self.zmq_iface.apply_config(cfg["zmq"])
        restart = False
        if self.bind_port and (
            str(cfg["http"]["host"]) != str(self.bind_host)
            or int(cfg["http"]["port"]) != int(self.bind_port)
        ):
            restart = True
        active_path = getattr(hub, "path", "/control") if hub is not None else "/control"
        if str(cfg["ws"]["path"]) != str(active_path):
            restart = True
        return restart

    def _store_robot(self, **fields) -> None:
        with self._fsm_lock:
            self._fsm_state.update(fields)
            self._fsm_state["checked_at"] = time.time()

    def robot_status(self) -> dict:
        """Last robot poll. Never calls the SDK."""
        with self._fsm_lock:
            state = dict(self._fsm_state)
        checked = float(state.get("checked_at") or 0.0)
        age = round(time.time() - checked, 2) if checked else None
        return {
            "up": bool(state.get("up")),
            "fsm_id": state.get("fsm_id"),
            "fsm_name": state.get("fsm_name"),
            "error": state.get("error") or "",
            "age_s": age,
        }

    def _poll_fsm(self) -> None:
        """One GetFsmId on this thread. A short timeout keeps the SDK lock free."""
        if not self.ready or self.client is None:
            self._store_robot(up=False, fsm_id=None, fsm_name=None, error="SDK not ready")
            return
        try:
            with self._lock:
                self.client.SetTimeout(1.5)
                try:
                    code, value = self.client.GetFsmId()
                finally:
                    self.client.SetTimeout(10.0)
        except Exception as exc:
            message = str(exc)
            self.last_error = message
            self._store_robot(up=False, error=message)
            return
        if code == 0 and value is not None:
            fsm_id = int(value)
            self.last_error = ""
            self._store_robot(
                up=True,
                fsm_id=fsm_id,
                fsm_name=FSM_NAMES.get(fsm_id),
                error="",
            )
            return
        message = f"GetFsmId failed ({code})"
        self.last_error = message
        self._store_robot(up=False, error=message)

    def _fsm_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_fsm()
            self._stop.wait(1.0)

    def health(self) -> dict:
        robot_link = self.robot_status()
        hold = dict(self._hold)
        return {
            "ok": True,
            "robot": "g1",
            "sdk_ready": self.ready,
            "network_interface": self.network_interface,
            "fsm_id": robot_link["fsm_id"],
            "fsm_name": robot_link["fsm_name"],
            "robot_link": robot_link,
            "drive": {
                "mode": hold["mode"],
                "lx": hold["lx"],
                "ly": hold["ly"],
                "rx": hold["rx"],
                "vx": hold["vx"],
                "vy": hold["vy"],
                "w": hold["w"],
            },
            "error": self.last_error,
            "links": self.link_status(),
            "js": self.local_js.status(),
        }

    def link_status(self) -> dict:
        hub = getattr(self, "control_hub", None)
        return {
            "http": {"up": True, "connections": self.http_clients.count()},
            "ws": {
                "up": bool(hub and hub.enabled),
                "connections": hub.count() if hub is not None else 0,
            },
            "zmq": {
                "up": bool(self.zmq_iface.running),
                "connections": self.zmq_iface.connections(),
                "error": self.zmq_iface.error,
            },
        }

    def set_stick(self, lx: float, ly: float, rx: float, source: str = "wireless") -> None:
        self._hold.update({
            "lx": lx,
            "ly": ly,
            "rx": rx,
            "vx": ly * LINEAR_SCALE,
            "vy": -lx * LINEAR_SCALE,
            "w": -rx * YAW_SCALE,
            "mode": "stick",
            "ts": time.time(),
        })
        self._record_drive(source)

    def set_cmd_vel(self, vx: float, vy: float, w: float, source: str = "cmd_vel") -> None:
        self._hold.update({
            "lx": 0.0,
            "ly": 0.0,
            "rx": 0.0,
            "vx": vx,
            "vy": vy,
            "w": w,
            "mode": "sport",
            "ts": time.time(),
        })
        self._record_drive(source)

    def stop_motion(self, *, record: bool = True, source: str = "stop") -> None:
        self._hold.update({
            "lx": 0.0,
            "ly": 0.0,
            "rx": 0.0,
            "vx": 0.0,
            "vy": 0.0,
            "w": 0.0,
            "mode": "idle",
            "ts": 0.0,
        })
        if self.client is None:
            self._moving = False
        else:
            with self._lock:
                try:
                    self.client.SetVelocity(0.0, 0.0, 0.0, 0.0)
                except Exception as exc:
                    self.last_error = str(exc)
            self._moving = False
        if record:
            self._record_drive(source)

    def _record_drive(self, source: str) -> None:
        hold = self._hold
        # Log when the velocity command changes by 0.01.
        stopped = hold["mode"] == "idle" or max(
            abs(hold["vx"]), abs(hold["vy"]), abs(hold["w"]),
        ) < 0.005
        if stopped:
            signature = ("stop", source)
            name = "Stop"
            detail = "Motion released"
        elif hold["mode"] == "sport":
            signature = (
                "sport",
                source,
                round(hold["vx"], 2),
                round(hold["vy"], 2),
                round(hold["w"], 2),
            )
            name = "Sport velocity"
            detail = f"vx {hold['vx']:.2f}  vy {hold['vy']:.2f}  w {hold['w']:.2f}"
        else:
            signature = (
                "stick",
                source,
                round(hold["vx"], 2),
                round(hold["vy"], 2),
                round(hold["w"], 2),
            )
            name = "Stick"
            detail = (
                f"lx {hold['lx']:.2f}  ly {hold['ly']:.2f}  rx {hold['rx']:.2f}"
                f"  vx {hold['vx']:.2f}  vy {hold['vy']:.2f}  w {hold['w']:.2f}"
            )
        if signature == self._drive_sig:
            return
        self._drive_sig = signature
        self.actions.record("drive", name, detail=detail, source=source)
        hub = getattr(self, "control_hub", None)
        if hub is not None:
            hub.publish()

    def _ensure_gait(self) -> None:
        if self._gait or self.client is None:
            return
        code = self.client.SetBalanceMode(1)
        if code == 0:
            self._gait = True

    def _hold_loop(self) -> None:
        last_send = 0.0
        while not self._stop.is_set():
            time.sleep(0.05)
            if not self.ready or self.client is None:
                continue
            hold = dict(self._hold)
            active = hold["mode"] != "idle" and (time.time() - hold["ts"]) <= HOLD_TIMEOUT_SEC
            moving = abs(hold["vx"]) + abs(hold["vy"]) + abs(hold["w"]) > 0.001
            if not active or not moving:
                if self._moving:
                    self.stop_motion(source="hold")
                continue
            now = time.time()
            if now - last_send < HOLD_PERIOD_SEC:
                continue
            with self._lock:
                try:
                    self._ensure_gait()
                    code = self.client.SetVelocity(
                        hold["vx"], hold["vy"], hold["w"], VELOCITY_DURATION_SEC
                    )
                    if code != 0:
                        message = f"SetVelocity failed ({code})"
                        self.last_error = message
                        if message != self._vel_fault:
                            self._vel_fault = message
                            self.actions.record(
                                "drive", "SetVelocity", ok=False, detail=message, source="hold",
                            )
                    else:
                        self.last_error = ""
                        self._vel_fault = ""
                        self._moving = True
                        last_send = now
                except Exception as exc:
                    message = str(exc)
                    self.last_error = message
                    if message != self._vel_fault:
                        self._vel_fault = message
                        self.actions.record(
                            "drive", "SetVelocity", ok=False, detail=message, source="hold",
                        )

    def sport(self, slug: str, body: dict | None = None) -> tuple[int, dict]:
        info = SPORT_ACTIONS.get(slug)
        if info is None:
            self.actions.record("sport", slug, ok=False, detail="Unknown action", source="api")
            return 404, {"detail": f"Unknown sport action: {slug}"}
        if not self.ready or self.client is None:
            self.actions.record(
                "sport", info["name"], ok=False, detail="G1 SDK not ready", source="api",
            )
            return 503, {"detail": "G1 SDK not ready"}
        body = body or {}
        self.stop_motion(record=False)
        with self._lock:
            try:
                if info.get("kind") == "stop":
                    code = self.client.SetVelocity(0.0, 0.0, 0.0, 0.0)
                elif info.get("kind") == "stand_height":
                    code = self.client.SetStandHeight(info["value"])
                elif info.get("kind") == "wave":
                    code = self.client.SetTaskId(1 if body.get("turn") else 0)
                elif info.get("kind") == "shake":
                    stage = body.get("stage")
                    code = self.client.SetTaskId(3 if stage == 1 else 2)
                else:
                    code = self.client.SetFsmId(int(info["fsm_id"]))
            except Exception as exc:
                self.last_error = str(exc)
                self.actions.record(
                    "mode" if info.get("fsm_id") is not None else "sport",
                    info["name"],
                    ok=False,
                    detail=str(exc),
                    source="api",
                )
                return 500, {"detail": str(exc), "ok": False, "action": slug, "name": info["name"]}
        fsm_id = None
        fsm_name = None
        requested = info.get("fsm_id")
        if requested is not None and self.client is not None:
            requested_id = int(requested)
            deadline = time.time() + 3.0
            while True:
                try:
                    with self._lock:
                        read_code, value = self.client.GetFsmId()
                    if read_code == 0 and value is not None:
                        fsm_id = int(value)
                        fsm_name = FSM_NAMES.get(fsm_id)
                except Exception as exc:
                    self.last_error = str(exc)
                    break
                if fsm_id == requested_id or time.time() >= deadline:
                    break
                time.sleep(0.25)
        payload = {
            "ok": code == 0,
            "action": slug,
            "name": info["name"],
            "error_code": code,
        }
        if requested is not None:
            payload["requested_fsm_id"] = int(requested)
            payload["fsm_id"] = fsm_id
            payload["fsm_name"] = fsm_name
            payload["applied"] = fsm_id == int(requested)
        kind = "mode" if requested is not None else "sport"
        if requested is not None:
            now = fsm_name or (str(fsm_id) if fsm_id is not None else "unknown")
            detail = f"requested {int(requested)}, now {now}"
            if code == 0 and payload.get("applied") is False:
                detail += ", not applied"
            ok = code == 0 and payload.get("applied") is True
        else:
            detail = "ok" if code == 0 else f"failed ({code})"
            ok = code == 0
        self.actions.record(kind, info["name"], ok=ok, detail=detail, source="api")
        if code != 0:
            self.last_error = f"{slug} failed ({code})"
            return 502, payload
        return 200, payload

    def arm(self, slug: str) -> tuple[int, dict]:
        match = next((item for item in self.arm_actions if item["slug"] == slug), None)
        if match is None:
            self.actions.record("gesture", slug, ok=False, detail="Unknown gesture", source="api")
            return 404, {"detail": f"Unknown arm action: {slug}"}
        if not self.ready or self.arm_client is None:
            self.actions.record(
                "gesture", match["name"], ok=False, detail="G1 SDK not ready", source="api",
            )
            return 503, {"detail": "G1 SDK not ready"}
        with self._lock:
            try:
                code = self.arm_client.ExecuteAction(match["id"])
            except Exception as exc:
                self.last_error = str(exc)
                self.actions.record(
                    "gesture", match["name"], ok=False, detail=str(exc), source="api",
                )
                return 500, {"detail": str(exc)}
        if code != 0:
            self.last_error = f"arm {slug} failed ({code})"
            self.actions.record(
                "gesture", match["name"], ok=False, detail=f"failed ({code})", source="api",
            )
            return 502, {"ok": False, "error_code": code, "action": slug}
        self.actions.record(
            "gesture", match["name"], detail=f"id {match['id']}", source="api",
        )
        return 200, {"ok": True, "action": slug, "name": match["name"], "id": match["id"]}


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>G1 controller — Swagger</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" crossorigin/>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js" crossorigin></script>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-standalone-preset.js" crossorigin></script>
<script>
  window.onload = function () {
    window.ui = SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
      layout: "BaseLayout",
    });
  };
</script>
</body>
</html>
"""


def _openapi_spec() -> dict:
    ok = {
        "200": {
            "description": "OK",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }
    stick = {
        "type": "object",
        "properties": {
            "lx": {"type": "number"},
            "ly": {"type": "number"},
            "rx": {"type": "number"},
            "ry": {"type": "number"},
            "keys": {"type": "integer"},
            "grab": {"type": "boolean"},
        },
    }
    vel = {
        "type": "object",
        "properties": {
            "vx": {"type": "number", "description": "Forward m/s"},
            "vy": {"type": "number", "description": "Left m/s"},
            "w": {"type": "number", "description": "Yaw rad/s"},
        },
    }
    camera_q = [
        {
            "name": "path",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "example": "stream/g1/front"},
        }
    ]

    def op(tag, summary, **extra):
        body = {"tags": [tag], "summary": summary, "responses": ok}
        body.update(extra)
        return body

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "G1 controller",
            "version": "1.0.0",
            "description": (
                "Operator API for the Unitree G1. Stick and /cmd_vel hold SetVelocity. "
                "WebSocket /control streams the same control messages: "
                "client sends {type: stick|cmd_vel|stop}; "
                "server sends {type: control, mode, lx, ly, rx, vx, vy, w, ts} "
                "on connect and whenever the command changes."
            ),
        },
        "tags": [
            {"name": "Status", "description": "Health, FSM, odometry, and the action log"},
            {"name": "Drive", "description": "Stick, body velocity, and stop"},
            {"name": "Modes", "description": "Sport actions and walking FSM"},
            {"name": "Gestures", "description": "Arm gestures"},
            {"name": "Video", "description": "MediaMTX streams and camera controls"},
            {"name": "Config", "description": "Interfaces and stick scales"},
        ],
        "paths": {
            "/health": {"get": op("Status", "SDK and FSM status")},
            "/fsm": {"get": op("Status", "Current FSM id")},
            "/odom": {"get": op("Status", "Latest sample from each odometry topic")},
            "/logs": {"get": op("Status", "Controlled-action log")},
            "/wireless": {
                "post": op(
                    "Drive",
                    "Left stick moves, right stick turns",
                    requestBody={"content": {"application/json": {"schema": stick}}},
                )
            },
            "/cmd_vel": {
                "post": op(
                    "Drive",
                    "Body velocity hold",
                    requestBody={"content": {"application/json": {"schema": vel}}},
                )
            },
            "/cmd_vel/stop": {"post": op("Drive", "StopMove")},
            "/control": {
                "get": op(
                    "Drive",
                    "WebSocket control stream",
                    description="Upgrade to WebSocket. Send {type: stick|cmd_vel|stop}.",
                )
            },
            "/sport": {"get": op("Modes", "Sport action catalog")},
            "/sport/{slug}": {"post": op("Modes", "Run a sport or FSM action")},
            "/arm": {"get": op("Gestures", "Arm gesture catalog")},
            "/arm/{slug}": {"post": op("Gestures", "Run an arm gesture")},
            "/video/paths": {"get": op("Video", "List MediaMTX topics (streams)")},
            "/video/whep": {
                "post": op(
                    "Video",
                    "Proxy WebRTC WHEP offer for a topic",
                    parameters=[
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                )
            },
            "/video/overlay": {
                "get": op("Video", "Read the burned-in timestamp flag for a topic", parameters=camera_q),
                "put": op(
                    "Video",
                    "Show or hide the burned-in timestamp on a topic",
                    parameters=camera_q,
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "timestamp": {"type": "boolean"},
                                    },
                                    "required": ["timestamp"],
                                }
                            }
                        }
                    },
                ),
            },
            "/video/camera": {
                "get": op("Video", "Read V4L2 camera controls for a topic", parameters=camera_q),
                "put": op(
                    "Video",
                    "Set V4L2 camera controls; saved in the same config.db as scale/interfaces",
                    parameters=camera_q,
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "zoom": {"type": "integer"},
                                        "focus": {"type": "integer"},
                                        "focus_auto": {"type": "integer"},
                                        "exposure": {"type": "integer"},
                                        "gain": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                ),
            },
            "/config/interfaces": {
                "get": op("Config", "HTTP, WebSocket, and ZMQ settings"),
                "put": op("Config", "Save interface settings to the database"),
            },
            "/config/scale": {
                "get": op("Config", "Move, turn, and speed scales"),
                "put": op("Config", "Save move, turn, and speed scales"),
            },
        },
    }


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Control fields must be numbers") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("Control fields must be finite")
    return number


class ControlHub:
    """WebSocket /control. Clients stream stick, cmd_vel, and stop; every change is broadcast."""

    def __init__(self, robot: G1Controller):
        self.robot = robot
        self.enabled = True
        self.path = "/control"
        self._sockets: set = set()
        self._lock = threading.Lock()
        self._loop = None

    def count(self) -> int:
        with self._lock:
            return len(self._sockets)

    def bind(self, loop) -> None:
        self._loop = loop

    def snapshot(self) -> dict:
        hold = dict(self.robot._hold)
        return {
            "type": "control",
            "mode": hold["mode"],
            "lx": hold["lx"],
            "ly": hold["ly"],
            "rx": hold["rx"],
            "vx": hold["vx"],
            "vy": hold["vy"],
            "w": hold["w"],
            "ts": hold["ts"],
        }

    def publish(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        message = self.snapshot()
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message), loop)
        except RuntimeError:
            return

    async def _broadcast(self, message: dict) -> None:
        with self._lock:
            sockets = list(self._sockets)
        dead = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if not dead:
            return
        with self._lock:
            for ws in dead:
                self._sockets.discard(ws)

    def apply(self, body: dict, source: str) -> None:
        kind = str(body.get("type") or body.get("op") or "").strip().lower()
        if kind in ("stop", "halt"):
            self.robot.stop_motion(source=source)
            return
        if kind in ("cmd_vel", "sport", "velocity"):
            self.robot.set_cmd_vel(
                _num(body.get("vx")),
                _num(body.get("vy")),
                _num(body.get("w", body.get("wz"))),
                source=source,
            )
            return
        if kind in ("stick", "wireless"):
            self.robot.set_stick(
                _num(body.get("lx")),
                _num(body.get("ly")),
                _num(body.get("rx")),
                source=source,
            )
            return
        raise ValueError("type must be stick, cmd_vel, or stop")

    async def handle(self, websocket) -> None:
        from starlette.websockets import WebSocketDisconnect

        self.bind(asyncio.get_running_loop())
        if not self.enabled:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        with self._lock:
            self._sockets.add(websocket)
        try:
            await websocket.send_json(self.snapshot())
            while True:
                raw = await websocket.receive()
                if raw.get("type") == "websocket.disconnect":
                    break
                text = raw.get("text")
                if text is None:
                    await websocket.send_json({"type": "error", "detail": "Control messages are JSON text"})
                    continue
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                    continue
                if not isinstance(body, dict):
                    await websocket.send_json({"type": "error", "detail": "Control message must be an object"})
                    continue
                try:
                    self.apply(body, "ws")
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
        except WebSocketDisconnect:
            pass
        finally:
            with self._lock:
                self._sockets.discard(websocket)


def build_app(robot: G1Controller):
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles

    async def health(_request: Request):
        return JSONResponse(robot.health())

    async def odom_get(_request: Request):
        return JSONResponse(robot.odom.snapshot())

    async def wireless(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        if "grab" in body:
            robot.local_js.set_grab(bool(body.get("grab")))
            if not any(key in body for key in ("lx", "ly", "rx", "ry")):
                return JSONResponse({"ok": True, "js": robot.local_js.status()})
        robot.set_stick(
            float(body.get("lx") or 0.0),
            float(body.get("ly") or 0.0),
            float(body.get("rx") or 0.0),
            source="http",
        )
        return JSONResponse({"ok": True})

    async def cmd_vel(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        w = body.get("w", body.get("wz", 0.0))
        robot.set_cmd_vel(
            float(body.get("vx") or 0.0),
            float(body.get("vy") or 0.0),
            float(w or 0.0),
            source="http",
        )
        return JSONResponse({"ok": True})

    async def cmd_vel_stop(_request: Request):
        await asyncio.to_thread(robot.stop_motion, source="http")
        return JSONResponse({"ok": True})

    async def sport_catalog(_request: Request):
        actions = []
        for slug, info in SPORT_ACTIONS.items():
            actions.append({
                "slug": slug,
                "name": info["name"],
                "path": f"/sport/{slug}",
                "fsm_id": info.get("fsm_id"),
            })
        return JSONResponse({"ok": True, "actions": actions})

    async def sport_post(request: Request):
        slug = request.path_params["slug"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code, payload = await asyncio.to_thread(robot.sport, slug, body)
        return JSONResponse(payload, status_code=code)

    async def arm_catalog(_request: Request):
        return JSONResponse({"ok": True, "actions": robot.arm_actions})

    async def arm_post(request: Request):
        code, payload = await asyncio.to_thread(robot.arm, request.path_params["slug"])
        return JSONResponse(payload, status_code=code)

    async def root(_request: Request):
        return RedirectResponse(url="/console/", status_code=307)

    async def openapi_json(_request: Request):
        return JSONResponse(_openapi_spec())

    async def docs_page(_request: Request):
        return HTMLResponse(_SWAGGER_HTML)

    async def logs_get(request: Request):
        raw = request.query_params.get("after") or "0"
        try:
            after = int(raw)
        except ValueError:
            after = 0
        return JSONResponse({
            "ok": True,
            "path": robot.actions.path,
            "entries": robot.actions.since(after),
        })

    async def fsm_get(_request: Request):
        status = robot.health()
        return JSONResponse({
            "ok": True,
            "fsm_id": status.get("fsm_id"),
            "fsm_name": status.get("fsm_name"),
            "known": [
                {"fsm_id": fsm_id, "name": name}
                for fsm_id, name in FSM_NAMES.items()
            ],
        })

    saved_interfaces = robot.interfaces.load()
    robot.control_hub = ControlHub(robot)
    robot.control_hub.enabled = bool(saved_interfaces["ws"]["enabled"])
    robot.control_hub.path = saved_interfaces["ws"]["path"]

    async def control_ws(websocket):
        await robot.control_hub.handle(websocket)

    def _interface_payload(restart: bool = False) -> dict:
        cfg = robot.interfaces.load()
        return {
            "ok": True,
            "database": robot.interfaces.path,
            "restart": restart,
            "interfaces": cfg,
            "runtime": {
                "http": {"host": robot.bind_host, "port": robot.bind_port},
                "ws": {
                    "enabled": bool(robot.control_hub.enabled),
                    "path": robot.control_hub.path,
                },
                "zmq": {
                    "enabled": bool(cfg["zmq"]["enabled"]),
                    "endpoint": robot.zmq_iface.endpoint,
                    "running": bool(robot.zmq_iface.running),
                    "error": robot.zmq_iface.error,
                },
            },
        }

    async def interfaces_get(_request: Request):
        return JSONResponse(_interface_payload(False))

    async def interfaces_put(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        incoming = body.get("interfaces") if isinstance(body, dict) else None
        if incoming is None and isinstance(body, dict):
            incoming = body
        try:
            saved = robot.interfaces.save(incoming)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        restart = robot.apply_interface_config(saved)
        return JSONResponse(_interface_payload(restart))

    async def scale_get(_request: Request):
        return JSONResponse({"ok": True, **robot.scales.load()})

    async def scale_put(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        try:
            saved = robot.scales.save(body if isinstance(body, dict) else {})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **saved})

    mediamtx_host = os.environ.get("G1_MEDIAMTX_HOST", "10.1.101.173")
    mediamtx_api = os.environ.get("G1_MEDIAMTX_API", f"http://{mediamtx_host}:9997").rstrip("/")
    mediamtx_webrtc = os.environ.get("G1_MEDIAMTX_WEBRTC", f"http://{mediamtx_host}:8889").rstrip("/")

    video_src_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "video-src")
    )
    if video_src_dir not in sys.path:
        sys.path.insert(0, video_src_dir)
    try:
        import camera_ctrl as _camera_ctrl
    except ImportError:
        _camera_ctrl = None
        print(f"camera controls unavailable (missing {video_src_dir}/camera_ctrl.py)", flush=True)
    try:
        import overlay_ctrl as _overlay_ctrl
    except ImportError:
        _overlay_ctrl = None
        print(f"video overlay unavailable (missing {video_src_dir}/overlay_ctrl.py)", flush=True)
    else:
        _camera_ctrl.configure_settings_db(robot.cameras.path)
        try:
            for topic, saved in robot.cameras.load_all().items():
                if not saved or not _camera_ctrl.topic_controllable(topic):
                    continue
                try:
                    result = _camera_ctrl.camera_update(topic, saved, persist=False)
                    print(
                        f"camera restored {result.get('path')} "
                        f"({result.get('camera') or 'device'}) from {robot.cameras.path}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"camera restore skipped {topic}: {exc}", flush=True)
        except Exception as exc:
            print(f"camera restore skipped: {exc}", flush=True)

    def _video_path_ok(path: str) -> bool:
        return bool(path) and ".." not in path and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", path))

    def _video_topics() -> list[dict]:
        import urllib.request

        with urllib.request.urlopen(f"{mediamtx_api}/v3/paths/list", timeout=3) as resp:
            payload = json.loads(resp.read().decode())
        topics = []
        for item in payload.get("items") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            topics.append({
                "name": name,
                "ready": bool(item.get("ready")),
                "tracks": item.get("tracks") or [],
                "controllable": bool(
                    _camera_ctrl and _camera_ctrl.topic_controllable(name)
                ),
            })
        return topics

    async def video_paths(_request: Request):
        try:
            topics = await asyncio.to_thread(_video_topics)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "topics": [], "error": str(exc)},
                status_code=502,
            )
        return JSONResponse({"ok": True, "topics": topics, "host": mediamtx_host})

    async def video_whep(request: Request):
        import urllib.error
        import urllib.request
        from starlette.responses import Response

        path = (request.query_params.get("path") or "").strip().strip("/")
        if not _video_path_ok(path):
            return JSONResponse({"ok": False, "error": "invalid topic"}, status_code=400)
        offer = await request.body()

        def post_offer() -> tuple[int, bytes]:
            req = urllib.request.Request(
                f"{mediamtx_webrtc}/{path}/whep",
                data=offer,
                method="POST",
                headers={"Content-Type": "application/sdp"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        status, answer = await asyncio.to_thread(post_offer)
        return Response(answer, status_code=status, media_type="application/sdp")

    async def video_camera_get(request: Request):
        if _camera_ctrl is None:
            return JSONResponse(
                {"ok": False, "error": "camera control module unavailable"},
                status_code=503,
            )
        path = (request.query_params.get("path") or "").strip().strip("/")
        if not _video_path_ok(path):
            return JSONResponse({"ok": False, "error": "invalid topic"}, status_code=400)

        def read_status():
            payload = _camera_ctrl.camera_status(path)
            payload["saved"] = robot.cameras.load(path)
            payload["database"] = robot.cameras.path
            return payload

        try:
            payload = await asyncio.to_thread(read_status)
        except LookupError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=404)
        except OSError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=503)
        except Exception as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=500)
        return JSONResponse(payload)

    async def video_camera_put(request: Request):
        if _camera_ctrl is None:
            return JSONResponse(
                {"ok": False, "error": "camera control module unavailable"},
                status_code=503,
            )
        path = (request.query_params.get("path") or "").strip().strip("/")
        if not _video_path_ok(path):
            return JSONResponse({"ok": False, "error": "invalid topic"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "body must be an object"}, status_code=400)
        allowed = ("zoom", "focus", "focus_auto", "exposure", "gain", "exposure_auto")
        updates = {key: body[key] for key in allowed if key in body}
        if not updates:
            return JSONResponse(
                {"ok": False, "error": f"provide one of: {', '.join(allowed)}"},
                status_code=400,
            )

        def apply_update():
            # Apply to the device, then persist beside interfaces/scale in config.db.
            payload = _camera_ctrl.camera_update(path, updates, persist=False)
            saved = robot.cameras.merge(path, updates)
            payload["saved"] = saved
            payload["database"] = robot.cameras.path
            return payload

        try:
            payload = await asyncio.to_thread(apply_update)
        except LookupError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=503)
        except Exception as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=500)
        return JSONResponse(payload)

    async def video_overlay_get(request: Request):
        if _overlay_ctrl is None:
            return JSONResponse(
                {"ok": False, "error": "video overlay module unavailable"},
                status_code=503,
            )
        path = (request.query_params.get("path") or "").strip().strip("/")
        if not _video_path_ok(path):
            return JSONResponse({"ok": False, "error": "invalid topic"}, status_code=400)
        return JSONResponse({
            "ok": True,
            "path": path,
            "timestamp": _overlay_ctrl.timestamp_enabled(path),
        })

    async def video_overlay_put(request: Request):
        if _overlay_ctrl is None:
            return JSONResponse(
                {"ok": False, "error": "video overlay module unavailable"},
                status_code=503,
            )
        path = (request.query_params.get("path") or "").strip().strip("/")
        if not _video_path_ok(path):
            return JSONResponse({"ok": False, "error": "invalid topic"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict) or "timestamp" not in body:
            return JSONResponse(
                {"ok": False, "error": "provide timestamp"},
                status_code=400,
            )
        try:
            enabled = _overlay_ctrl.set_timestamp(path, body["timestamp"])
        except ValueError as exc:
            return JSONResponse({"ok": False, "path": path, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "path": path, "timestamp": enabled})

    routes = [
        WebSocketRoute(saved_interfaces["ws"]["path"], control_ws),
        Route("/config/interfaces", interfaces_get, methods=["GET"]),
        Route("/config/interfaces", interfaces_put, methods=["PUT"]),
        Route("/config/scale", scale_get, methods=["GET"]),
        Route("/config/scale", scale_put, methods=["PUT"]),
        Route("/", root, methods=["GET"]),
        Route("/openapi.json", openapi_json, methods=["GET"]),
        Route("/docs", docs_page, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/odom", odom_get, methods=["GET"]),
        Route("/wireless", wireless, methods=["POST"]),
        Route("/cmd_vel", cmd_vel, methods=["POST"]),
        Route("/cmd_vel/stop", cmd_vel_stop, methods=["POST"]),
        Route("/sport", sport_catalog, methods=["GET"]),
        Route("/sport/{slug}", sport_post, methods=["POST"]),
        Route("/arm", arm_catalog, methods=["GET"]),
        Route("/arm/{slug}", arm_post, methods=["POST"]),
        Route("/fsm", fsm_get, methods=["GET"]),
        Route("/logs", logs_get, methods=["GET"]),
        Route("/video/paths", video_paths, methods=["GET"]),
        Route("/video/whep", video_whep, methods=["POST"]),
        Route("/video/camera", video_camera_get, methods=["GET"]),
        Route("/video/camera", video_camera_put, methods=["PUT"]),
        Route("/video/overlay", video_overlay_get, methods=["GET"]),
        Route("/video/overlay", video_overlay_put, methods=["PUT"]),
    ]
    console_dir = _console_web_dir()
    if console_dir:
        routes.append(Mount("/console", app=StaticFiles(directory=console_dir, html=True), name="console"))
        print(f"operator console directory {console_dir}", flush=True)

    app = Starlette(routes=routes)

    class _RememberHttpClient:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                robot.http_clients.touch(scope.get("client"))
            await self.app(scope, receive, send)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_RememberHttpClient)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="G1 controller REST bridge and console")
    parser.add_argument("--interface", default=os.environ.get("G1_DDS_INTERFACE", "auto"))
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    iface = _detect_interface(args.interface)
    robot = G1Controller(iface)
    saved = robot.interfaces.load()
    host = saved["http"]["host"]
    port = args.port if args.port is not None else int(saved["http"]["port"])
    robot.bind_host = host
    robot.bind_port = port
    print(f"G1 controller DDS interface {iface}", flush=True)
    try:
        robot.start()
    except Exception as exc:
        print(f"error: G1 SDK init failed: {exc}", flush=True)
        raise SystemExit(1) from exc
    print(f"G1 SDK ready on {iface}", flush=True)

    import uvicorn

    app = build_app(robot)
    robot.zmq_iface.apply_config(robot.interfaces.load()["zmq"])
    print(f"  listen  http://{host}:{port}/", flush=True)
    print(f"  control ws://{host}:{port}{robot.control_hub.path}", flush=True)
    print(f"  zmq     {robot.zmq_iface.endpoint or 'off'}", flush=True)
    print(f"  config  {robot.interfaces.path}", flush=True)
    print(f"  health  http://{host}:{port}/health", flush=True)
    print(f"  console http://{host}:{port}/console/", flush=True)
    print(f"  swagger http://{host}:{port}/docs", flush=True)
    print(f"  actions {robot.actions.path}", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        robot.shutdown()


if __name__ == "__main__":
    main()
