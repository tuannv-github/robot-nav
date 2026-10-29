#!/usr/bin/env python3
"""Per-topic video overlay flags shared by the publishers and the console.

The camera processes burn a clock into the frame with textoverlay. The console
writes the on/off flag here; each publisher polls the file and shows or hides
its overlay. Missing entries stay on, which matches the previous always-on clock.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any


_lock = threading.Lock()


def _config_dir() -> str:
    override = os.environ.get("G1_CONFIG_DB", "").strip()
    if override:
        directory = os.path.dirname(override) or "."
    else:
        directory = os.path.join(os.path.expanduser("~"), ".g1-controller")
    os.makedirs(directory, exist_ok=True)
    return directory


def overlay_file() -> str:
    return os.path.join(_config_dir(), "video_overlay.json")


def normalize_path(path: str) -> str:
    return path.strip().strip("/")


def _load() -> dict[str, Any]:
    path = overlay_file()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "on", "yes"):
            return True
        if text in ("0", "false", "off", "no"):
            return False
    raise ValueError("timestamp must be a boolean")


def timestamp_enabled(path: str) -> bool:
    """True when the clock overlay should be drawn. Default is on."""
    topic = normalize_path(path)
    with _lock:
        entry = _load().get(topic)
    if isinstance(entry, dict) and "timestamp" in entry:
        try:
            return _as_bool(entry["timestamp"])
        except ValueError:
            return True
    return True


def set_timestamp(path: str, enabled: Any) -> bool:
    topic = normalize_path(path)
    if not topic:
        raise ValueError("path is required")
    flag = _as_bool(enabled)
    with _lock:
        payload = _load()
        payload[topic] = {"timestamp": flag}
        dest = overlay_file()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".overlay-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.write("\n")
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return flag
