#!/usr/bin/env python3
"""V4L2 camera controls keyed by MediaMTX topic (e.g. stream/g1/front).

Persists operator settings in the G1 controller SQLite DB
(``~/.g1-controller/config.db`` by default, override with ``G1_CONFIG_DB``).
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any


FOCUS_AUTO = 0x009A090C
FOCUS_ABSOLUTE = 0x009A090A
ZOOM_ABSOLUTE = 0x009A090D
EXPOSURE_AUTO = 0x009A0901
EXPOSURE_ABSOLUTE = 0x009A0902
GAIN = 0x00980913

QUERYCTRL = (3 << 30) | (68 << 16) | (ord("V") << 8) | 36
G_CTRL = (3 << 30) | (8 << 16) | (ord("V") << 8) | 27
S_CTRL = (3 << 30) | (8 << 16) | (ord("V") << 8) | 28

SETTING_KEYS = (
    "zoom",
    "focus",
    "focus_auto",
    "exposure",
    "gain",
    "exposure_auto",
)


class _Query(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("default_value", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class _Ctrl(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


@dataclass(frozen=True)
class CameraTarget:
    path: str
    label: str
    vendor: str
    product: str
    bus: int
    devnum: int
    controls: tuple[str, ...]


# Topics the console can tune. RealSense /stream/g1/down has no UVC zoom/focus.
CAMERA_TARGETS: dict[str, CameraTarget] = {
    "stream/g1/front": CameraTarget(
        path="stream/g1/front",
        label="front",
        vendor="",  # resolved dynamically (BRIO or Kiyo)
        product="",
        bus=0,
        devnum=0,
        controls=("zoom", "focus", "focus_auto", "exposure", "gain"),
    ),
}

CONTROL_IDS: dict[str, int] = {
    "zoom": ZOOM_ABSOLUTE,
    "focus": FOCUS_ABSOLUTE,
    "focus_auto": FOCUS_AUTO,
    "exposure": EXPOSURE_ABSOLUTE,
    "gain": GAIN,
    "exposure_auto": EXPOSURE_AUTO,
}

FRONT_CAMERAS = (
    ("Logitech BRIO", "046d", "085e", 2, 7),
    ("Razer Kiyo V2", "1532", "0e0b", 1, 7),
)


def normalize_path(path: str) -> str:
    return path.strip().strip("/")


def config_db_path() -> str:
    override = os.environ.get("G1_CONFIG_DB", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".g1-controller", "config.db")


class CameraSettingsStore:
    """Per-topic camera settings in the shared G1 controller database."""

    def __init__(self, path: str | None = None):
        self.path = path or config_db_path()
        directory = os.path.dirname(self.path)
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
    def _sanitize(settings: dict[str, Any]) -> dict[str, int]:
        clean: dict[str, int] = {}
        for key in SETTING_KEYS:
            if key not in settings or settings[key] is None:
                continue
            try:
                clean[key] = int(settings[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be an integer") from exc
        return clean

    def load(self, path: str) -> dict[str, int]:
        topic = normalize_path(path)
        with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT settings FROM camera WHERE path = ?",
                    (topic,),
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

    def load_all(self) -> dict[str, dict[str, int]]:
        with self._lock:
            with self._connect() as db:
                rows = db.execute("SELECT path, settings FROM camera").fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                payload = json.loads(row["settings"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                out[str(row["path"])] = self._sanitize(payload)
        return out

    def merge(self, path: str, updates: dict[str, Any]) -> dict[str, int]:
        topic = normalize_path(path)
        patch = self._sanitize(updates)
        if not patch:
            raise ValueError("no camera settings to save")
        with self._lock:
            current = {}
            with self._connect() as db:
                row = db.execute(
                    "SELECT settings FROM camera WHERE path = ?",
                    (topic,),
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
                    (topic, json.dumps(current, sort_keys=True), time.time()),
                )
                db.commit()
        return current


_STORE: CameraSettingsStore | None = None
_DB_PATH: str | None = None


def configure_settings_db(path: str) -> CameraSettingsStore:
    """Point camera persistence at the controller config DB (same as scale/interfaces)."""
    global _STORE, _DB_PATH
    db_path = str(path or "").strip() or config_db_path()
    _DB_PATH = db_path
    if _STORE is None or _STORE.path != db_path:
        _STORE = CameraSettingsStore(db_path)
    return _STORE


def settings_store() -> CameraSettingsStore:
    global _STORE
    if _STORE is None:
        _STORE = CameraSettingsStore(_DB_PATH or config_db_path())
    return _STORE


def load_settings(path: str) -> dict[str, int]:
    return settings_store().load(path)


def save_settings(path: str, updates: dict[str, Any]) -> dict[str, int]:
    return settings_store().merge(path, updates)


def _sysfs_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _usb_devices(vendor: str, product: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    root = "/sys/bus/usb/devices"
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        device = os.path.join(root, name)
        if not os.path.isdir(device):
            continue
        if _sysfs_text(os.path.join(device, "idVendor")).lower() != vendor:
            continue
        if _sysfs_text(os.path.join(device, "idProduct")).lower() != product:
            continue
        bus = _sysfs_text(os.path.join(device, "busnum"))
        devnum = _sysfs_text(os.path.join(device, "devnum"))
        if not bus.isdigit() or not devnum.isdigit():
            continue
        found.append((device, int(bus), int(devnum)))
    return found


def _capture_node(usb_device: str) -> str | None:
    for dirpath, dirnames, _filenames in os.walk(usb_device):
        for dirname in dirnames:
            if not dirname.startswith("video"):
                continue
            node = os.path.join(dirpath, dirname)
            if _sysfs_text(os.path.join(node, "index")) != "0":
                continue
            device = f"/dev/{dirname}"
            if os.path.exists(device):
                return device
    return None


def find_front_device() -> tuple[str, str, int, int, str, str]:
    """Return (device, name, bus, devnum, vendor, product)."""
    for name, vendor, product, bus_pref, dev_pref in FRONT_CAMERAS:
        matches = _usb_devices(vendor, product)
        if not matches:
            continue
        chosen = next(
            (item for item in matches if item[1] == bus_pref and item[2] == dev_pref),
            matches[0],
        )
        usb_path, bus, devnum = chosen
        device = _capture_node(usb_path)
        if device is None:
            continue
        return device, name, bus, devnum, vendor, product
    raise LookupError("no front camera connected (BRIO or Kiyo V2)")


def resolve_topic(path: str) -> dict[str, Any]:
    topic = normalize_path(path)
    if not topic:
        raise ValueError("path is required")
    if topic not in CAMERA_TARGETS:
        return {
            "path": topic,
            "controllable": False,
            "camera": None,
            "device": None,
            "reason": "topic has no camera controls",
        }
    device, name, bus, devnum, vendor, product = find_front_device()
    return {
        "path": topic,
        "controllable": True,
        "camera": name,
        "device": device,
        "bus": bus,
        "devnum": devnum,
        "vendor": vendor,
        "product": product,
        "controls": list(CAMERA_TARGETS[topic].controls),
    }


def topic_controllable(path: str) -> bool:
    return normalize_path(path) in CAMERA_TARGETS


def _query_control(fd: int, control_id: int) -> dict[str, Any] | None:
    query = _Query()
    query.id = control_id
    try:
        fcntl.ioctl(fd, QUERYCTRL, query)
    except OSError:
        return None
    if query.flags & 0x01:  # disabled
        return None
    current = _Ctrl(control_id, 0)
    try:
        fcntl.ioctl(fd, G_CTRL, current)
        value = int(current.value)
    except OSError:
        value = None
    return {
        "id": f"0x{control_id:08x}",
        "name": query.name.decode(errors="replace"),
        "min": int(query.minimum),
        "max": int(query.maximum),
        "step": int(query.step) or 1,
        "default": int(query.default_value),
        "value": value,
    }


def _set_control(fd: int, control_id: int, value: int) -> int:
    ctrl = _Ctrl(control_id, int(value))
    fcntl.ioctl(fd, S_CTRL, ctrl)
    got = _Ctrl(control_id, 0)
    fcntl.ioctl(fd, G_CTRL, got)
    return int(got.value)


def get_controls(device: str, names: list[str] | tuple[str, ...]) -> dict[str, Any]:
    fd = os.open(device, os.O_RDWR)
    try:
        out: dict[str, Any] = {}
        for name in names:
            control_id = CONTROL_IDS.get(name)
            if control_id is None:
                continue
            meta = _query_control(fd, control_id)
            if meta is not None:
                out[name] = meta
        return out
    finally:
        os.close(fd)


def set_controls(device: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Apply known controls. Setting focus implies focus_auto=0."""
    unknown = [key for key in updates if key not in CONTROL_IDS]
    if unknown:
        raise ValueError(f"unknown controls: {', '.join(unknown)}")

    ordered: list[tuple[str, int]] = []
    if "focus" in updates and "focus_auto" not in updates:
        ordered.append(("focus_auto", 0))
    if "focus_auto" in updates:
        ordered.append(("focus_auto", int(updates["focus_auto"])))
    for key in ("zoom", "focus", "exposure", "gain", "exposure_auto"):
        if key in updates:
            ordered.append((key, int(updates[key])))

    fd = os.open(device, os.O_RDWR)
    try:
        applied: dict[str, int] = {}
        for name, value in ordered:
            control_id = CONTROL_IDS[name]
            meta = _query_control(fd, control_id)
            if meta is None:
                raise OSError(f"control {name} is not available")
            lo, hi = meta["min"], meta["max"]
            if value < lo or value > hi:
                raise ValueError(f"{name} out of range [{lo}, {hi}]")
            applied[name] = _set_control(fd, control_id, value)
        return applied
    finally:
        os.close(fd)


def camera_status(path: str) -> dict[str, Any]:
    info = resolve_topic(path)
    saved = load_settings(path)
    if not info.get("controllable"):
        return {"ok": True, **info, "controls": {}, "saved": saved}
    controls = get_controls(info["device"], info["controls"])
    return {
        "ok": True,
        **info,
        "controls": controls,
        "saved": saved,
        "database": settings_store().path,
    }


def camera_update(path: str, updates: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    info = resolve_topic(path)
    if not info.get("controllable"):
        raise LookupError(info.get("reason") or "topic is not controllable")
    if not updates:
        raise ValueError("no controls to update")
    set_controls(info["device"], updates)
    if persist:
        save_settings(path, updates)
    return camera_status(path)


def apply_saved(path: str) -> dict[str, Any] | None:
    """Re-apply DB settings for a topic onto the live device. None if nothing saved."""
    saved = load_settings(path)
    if not saved:
        return None
    return camera_update(path, saved, persist=False)


def apply_all_saved() -> list[dict[str, Any]]:
    """Apply every saved topic; skip missing cameras."""
    results: list[dict[str, Any]] = []
    for topic, saved in settings_store().load_all().items():
        if not saved or not topic_controllable(topic):
            continue
        try:
            results.append(camera_update(topic, saved, persist=False))
        except (LookupError, OSError, ValueError) as exc:
            results.append({"ok": False, "path": topic, "error": str(exc), "saved": saved})
    return results
