#!/usr/bin/env python3
"""Video overlay flags shared by the publishers and the console.

The camera processes burn a clock into the frame with textoverlay. The console
writes one on/off flag here for every stream; each publisher polls the file and
shows or hides its overlay. A missing flag stays on, which matches the previous
always-on clock.
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


# One flag for every publisher. Older files stored a flag under each topic.
_ALL = "*"
# Publishers that read their own path. Written on every toggle so a process
# that has not reloaded this module still follows the shared switch.
_KNOWN_TOPICS = ("stream/g1/front", "stream/g1/down")


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


def _flag_of(entry: Any) -> bool | None:
    if isinstance(entry, dict) and "timestamp" in entry:
        try:
            return _as_bool(entry["timestamp"])
        except ValueError:
            return None
    return None


def _shared_flag(payload: dict[str, Any]) -> bool:
    """The clock is one setting for every stream. Default is on."""
    shared = _flag_of(payload.get(_ALL))
    if shared is not None:
        return shared
    flags = [
        flag
        for key, entry in payload.items()
        if key != _ALL and (flag := _flag_of(entry)) is not None
    ]
    if flags and all(item == flags[0] for item in flags):
        return flags[0]
    return True


def _store(payload: dict[str, Any]) -> None:
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


def timestamp_enabled(path: str = "") -> bool:
    """True when every stream should draw the clock. Default is on.

    ``path`` is accepted so existing callers keep working. The flag is shared.
    """
    del path
    with _lock:
        return _shared_flag(_load())


def set_timestamp(path: str, enabled: Any, topics: Any = None) -> bool:
    """Turn the clock on or off for every stream.

    The shared ``*`` entry is what current publishers read. Each topic is
    written too, so a publisher that still looks up its own path follows
    the same switch.
    """
    flag = _as_bool(enabled)
    names = set(_KNOWN_TOPICS)
    if normalize_path(path):
        names.add(normalize_path(path))
    if isinstance(topics, (list, tuple, set)):
        for item in topics:
            topic = normalize_path(str(item))
            if topic and topic != _ALL:
                names.add(topic)
    with _lock:
        for key in _load():
            if isinstance(key, str) and key and key != _ALL:
                names.add(key)
        payload: dict[str, Any] = {_ALL: {"timestamp": flag}}
        for topic in sorted(names):
            payload[topic] = {"timestamp": flag}
        _store(payload)
    return flag
