#!/usr/bin/env python3
"""G1 controller bridge: HTTP REST and the operator console.

Same role as go2_controller_bridge. Stick and /cmd_vel holds call
LocoClient.SetVelocity; sport and arm routes call the Unitree SDK2 clients.
Console: http://0.0.0.0:<port>/console/
"""

from __future__ import annotations

import argparse
import json
import os
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
HOLD_TIMEOUT_SEC = 0.45
HOLD_PERIOD_SEC = 0.2
VELOCITY_DURATION_SEC = 0.5


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


class G1Controller:
    def __init__(self, network_interface: str):
        self.network_interface = network_interface
        self.actions = ActionLog(_action_log_path())
        self._drive_sig = None
        self._vel_fault = ""
        self.ready = False
        self.last_error = ""
        self.client = None
        self.arm_client = None
        self.arm_actions = []
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
        self.ready = True
        self.actions.record(
            "system",
            "Controller ready",
            detail=self.network_interface,
            source="bridge",
        )
        self._thread.start()

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
        self.actions.record("system", "Controller stopping", source="bridge")
        self.stop_motion(record=False)

    def health(self) -> dict:
        fsm_id = None
        fsm_name = None
        if self.ready and self.client is not None:
            try:
                with self._lock:
                    code, value = self.client.GetFsmId()
                if code == 0 and value is not None:
                    fsm_id = int(value)
                    fsm_name = FSM_NAMES.get(fsm_id)
            except Exception as exc:
                self.last_error = str(exc)
        hold = dict(self._hold)
        return {
            "ok": True,
            "robot": "g1",
            "sdk_ready": self.ready,
            "network_interface": self.network_interface,
            "fsm_id": fsm_id,
            "fsm_name": fsm_name,
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
            signature = ("stop",)
            name = "Stop"
            detail = "Motion released"
        elif hold["mode"] == "sport":
            signature = (
                "sport",
                round(hold["vx"], 2),
                round(hold["vy"], 2),
                round(hold["w"], 2),
            )
            name = "Sport velocity"
            detail = f"vx {hold['vx']:.2f}  vy {hold['vy']:.2f}  w {hold['w']:.2f}"
        else:
            signature = (
                "stick",
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
                    self.stop_motion()
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
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "G1 controller",
            "version": "1.0.0",
            "description": "Operator API for the Unitree G1. Stick and /cmd_vel hold SetVelocity.",
        },
        "paths": {
            "/health": {"get": {"summary": "SDK and FSM status", "responses": ok}},
            "/wireless": {
                "post": {
                    "summary": "Left stick moves, right stick turns",
                    "requestBody": {"content": {"application/json": {"schema": stick}}},
                    "responses": ok,
                }
            },
            "/cmd_vel": {
                "post": {
                    "summary": "Body velocity hold",
                    "requestBody": {"content": {"application/json": {"schema": vel}}},
                    "responses": ok,
                }
            },
            "/cmd_vel/stop": {"post": {"summary": "StopMove", "responses": ok}},
            "/sport": {"get": {"summary": "Sport action catalog", "responses": ok}},
            "/sport/{slug}": {
                "post": {"summary": "Run a sport or FSM action", "responses": ok}
            },
            "/arm": {"get": {"summary": "Arm gesture catalog", "responses": ok}},
            "/arm/{slug}": {"post": {"summary": "Run an arm gesture", "responses": ok}},
            "/fsm": {"get": {"summary": "Current FSM id", "responses": ok}},
            "/logs": {"get": {"summary": "Controlled-action log", "responses": ok}},
        },
    }


def build_app(robot: G1Controller):
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    async def health(_request: Request):
        return JSONResponse(robot.health())

    async def wireless(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        robot.set_stick(
            float(body.get("lx") or 0.0),
            float(body.get("ly") or 0.0),
            float(body.get("rx") or 0.0),
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
        )
        return JSONResponse({"ok": True})

    async def cmd_vel_stop(_request: Request):
        robot.stop_motion()
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
        code, payload = robot.sport(slug, body)
        return JSONResponse(payload, status_code=code)

    async def arm_catalog(_request: Request):
        return JSONResponse({"ok": True, "actions": robot.arm_actions})

    async def arm_post(request: Request):
        code, payload = robot.arm(request.path_params["slug"])
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

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/openapi.json", openapi_json, methods=["GET"]),
        Route("/docs", docs_page, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/wireless", wireless, methods=["POST"]),
        Route("/cmd_vel", cmd_vel, methods=["POST"]),
        Route("/cmd_vel/stop", cmd_vel_stop, methods=["POST"]),
        Route("/sport", sport_catalog, methods=["GET"]),
        Route("/sport/{slug}", sport_post, methods=["POST"]),
        Route("/arm", arm_catalog, methods=["GET"]),
        Route("/arm/{slug}", arm_post, methods=["POST"]),
        Route("/fsm", fsm_get, methods=["GET"]),
        Route("/logs", logs_get, methods=["GET"]),
    ]
    console_dir = _console_web_dir()
    if console_dir:
        routes.append(Mount("/console", app=StaticFiles(directory=console_dir, html=True), name="console"))
        print(f"operator console directory {console_dir}", flush=True)

    app = Starlette(routes=routes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="G1 controller REST bridge and console")
    parser.add_argument("--interface", default=os.environ.get("G1_DDS_INTERFACE", "auto"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("G1_REST_PORT", "8081")))
    args = parser.parse_args()
    host = "0.0.0.0"

    iface = _detect_interface(args.interface)
    robot = G1Controller(iface)
    print(f"G1 controller DDS interface {iface}", flush=True)
    try:
        robot.start()
    except Exception as exc:
        print(f"error: G1 SDK init failed: {exc}", flush=True)
        raise SystemExit(1) from exc
    print(f"G1 SDK ready on {iface}", flush=True)

    import uvicorn

    app = build_app(robot)
    print(f"  listen  http://{host}:{args.port}/", flush=True)
    print(f"  health  http://{host}:{args.port}/health", flush=True)
    print(f"  console http://{host}:{args.port}/console/", flush=True)
    print(f"  swagger http://{host}:{args.port}/docs", flush=True)
    print(f"  actions {robot.actions.path}", flush=True)
    try:
        uvicorn.run(app, host=host, port=args.port, log_level="warning")
    finally:
        robot.shutdown()


if __name__ == "__main__":
    main()
