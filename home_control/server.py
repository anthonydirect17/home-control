"""Local web server: the JSON API plus the control page.

Everything under /api/ needs a remembered device (the page PIN), except
/api/health (for ServerWatch; no names, addresses or secrets) and /api/auth/*.
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, patterns, scheduler as sched
from .auth import COOKIE_NAME, REMEMBER_SECONDS, PinError
from .govee.base import BackendError

# Settings the page may change. Others (secrets store, DLL path, credential
# names) are only set from the command line on the server itself.
PAGE_SETTINGS = {"latitude", "longitude", "default_fps", "use_govee_desktop"}
OPEN_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Target(BaseModel):
    target: str


class PowerBody(Target):
    on: bool


class BrightnessBody(Target):
    value: int = Field(ge=1, le=100)


class ColorBody(Target):
    color: str


class SceneBody(Target):
    scene: str


class RenameBody(BaseModel):
    name: str = ""


class EffectBody(Target):
    pattern: Optional[str] = None
    preset: Optional[str] = None
    params: dict = {}
    fps: Optional[float] = None


class GroupBody(BaseModel):
    name: str
    members: list[str]
    mode: str = "span"


class PresetBody(BaseModel):
    name: str
    pattern: str
    params: dict = {}


class PreviewBody(BaseModel):
    pattern: str
    params: dict = {}
    segments: int = 20
    seconds: float = Field(default=6, gt=0, le=30)
    fps: float = Field(default=8, gt=0, le=20)


class PinBody(BaseModel):
    pin: str = ""


class ScheduleBody(BaseModel):
    id: Optional[str] = None
    name: str
    enabled: bool = True
    days: list[int] = []
    when: dict
    target: str
    action: dict


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def confirm_with_pin(gate, pin):
    """For arm, disarm and sirens: the PIN is required on every request, even on a remembered device."""
    try:
        gate.check_action_pin(pin)
    except PinError as e:
        raise HTTPException(429 if e.locked_for else 403, str(e))


def create_app(controller, store, scheduler, gate, started_at=None):
    app = FastAPI(title="Home Control")
    started_at = started_at or time.time()

    @app.middleware("http")
    async def require_pin(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_PATHS:
            if not gate.cookie_ok(request.cookies.get(COOKIE_NAME)):
                return JSONResponse({"detail": "PIN required", "pin_required": True}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        st = controller.status()
        return {
            "app": "home-control",
            "version": __version__,
            "started_at": iso(started_at),
            "uptime_seconds": int(time.time() - started_at),
            "connections": {route: {"label": b["label"], "up": bool(b["connected"])}
                            for route, b in st["backends"].items()},
            "light_count": st["device_count"],
            "effects_running": controller.effects_running(),
            "last_event_at": iso(controller.last_activity),
        }

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"required": gate.required, "pin_set": gate.pin_set,
                "signed_in": gate.cookie_ok(request.cookies.get(COOKIE_NAME)), "locked_for": gate.locked_for()}

    @app.post("/api/auth/login")
    def login(body: PinBody, response: Response):
        try:
            gate.check_pin(body.pin)
        except PinError as e:
            raise HTTPException(429 if e.locked_for else 401, str(e))
        response.set_cookie(COOKIE_NAME, gate.issue_cookie(), max_age=REMEMBER_SECONDS, httponly=True,
                            samesite="strict", path="/")
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    def run(fn, *args):
        try:
            errors = fn(*args)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": not errors, "errors": errors or {}}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/status")
    def status():
        return controller.status()

    @app.post("/api/reconnect")
    def reconnect():
        return controller.connect()

    @app.get("/api/devices")
    def devices():
        return controller.snapshot()

    @app.post("/api/devices/refresh")
    def refresh():
        try:
            controller.refresh()
            controller.poll_state()
        except BackendError as e:
            raise HTTPException(502, str(e))
        return controller.snapshot()

    @app.get("/api/devices/{key}/scenes")
    def device_scenes(key: str):
        try:
            return controller.scenes(key)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except BackendError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/devices/{key}/rename")
    def rename(key: str, body: RenameBody):
        dev = controller.device(key)
        if dev is None:
            raise HTTPException(404, "no such light")
        store.set_alias(dev.id, body.name.strip())
        controller.refresh()
        return controller.snapshot()

    @app.post("/api/control/power")
    def power(body: PowerBody):
        return run(controller.power, body.target, body.on)

    @app.post("/api/control/brightness")
    def brightness(body: BrightnessBody):
        return run(controller.brightness, body.target, body.value)

    @app.post("/api/control/color")
    def color(body: ColorBody):
        return run(controller.color, body.target, body.color)

    @app.post("/api/control/scene")
    def scene(body: SceneBody):
        return run(controller.scene, body.target, body.scene)

    @app.post("/api/control/effect")
    def effect(body: EffectBody):
        if body.preset:
            preset = store.get("presets").get(body.preset)
            if preset is None:
                raise HTTPException(404, f"no effect named {body.preset!r}")
            return run(controller.start_effect, body.target, preset["pattern"], preset["params"], body.fps)
        if not body.pattern:
            raise HTTPException(400, "pattern or preset is required")
        return run(controller.start_effect, body.target, body.pattern, body.params, body.fps)

    @app.post("/api/control/stop")
    def stop(body: Target):
        return run(controller.stop, body.target)

    @app.get("/api/patterns")
    def pattern_catalog():
        return patterns.catalog()

    @app.post("/api/preview")
    def preview(body: PreviewBody):
        if body.pattern not in patterns.PATTERNS:
            raise HTTPException(400, f"unknown pattern {body.pattern!r}")
        n = max(1, min(200, body.segments))
        count = max(1, min(240, int(body.seconds * body.fps)))
        try:
            return [[patterns.rgb_to_hex(c) for c in patterns.render(body.pattern, i / body.fps, n, body.params)]
                    for i in range(count)]
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"bad setting: {e}")

    @app.get("/api/groups")
    def groups():
        return store.get("groups")

    @app.post("/api/groups")
    def put_group(body: GroupBody):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "group name is required")
        try:
            store.put_group(name, body.members, body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return store.get("groups")

    @app.delete("/api/groups/{name}")
    def delete_group(name: str):
        store.delete_group(name)
        return store.get("groups")

    @app.get("/api/presets")
    def presets():
        return store.get("presets")

    @app.post("/api/presets")
    def put_preset(body: PresetBody):
        if body.pattern not in patterns.PATTERNS:
            raise HTTPException(400, f"unknown pattern {body.pattern!r}")
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "effect name is required")
        store.put_preset(name, body.pattern, body.params)
        return store.get("presets")

    @app.delete("/api/presets/{name}")
    def delete_preset(name: str):
        store.delete_preset(name)
        return store.get("presets")

    @app.get("/api/schedules")
    def schedules():
        settings = store.get("settings")
        out = []
        for s in store.get("schedules"):
            nxt = sched.next_run(s, settings) if s.get("enabled", True) else None
            out.append(dict(s, next_run=nxt.isoformat(timespec="minutes") if nxt else None))
        return out

    @app.post("/api/schedules")
    def put_schedule(body: ScheduleBody):
        entry = body.model_dump()
        if entry["id"] is None:
            entry.pop("id")
        if any(d not in range(7) for d in entry["days"]):
            raise HTTPException(400, "days must be 0 (Monday) to 6 (Sunday)")
        return store.put_schedule(entry)

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: str):
        store.delete_schedule(sid)
        return {"ok": True}

    @app.post("/api/schedules/{sid}/run")
    def run_schedule(sid: str):
        entry = next((s for s in store.get("schedules") if s["id"] == sid), None)
        if entry is None:
            raise HTTPException(404, "no such schedule")
        return run(controller.run_action, entry["target"], entry["action"])

    @app.get("/api/schedules/log")
    def schedule_log():
        return list(reversed(scheduler.log))

    @app.get("/api/settings")
    def settings():
        return store.get("settings")

    @app.post("/api/settings")
    def put_settings(body: dict):
        body = {k: v for k, v in body.items() if k in PAGE_SETTINGS}
        for key in ("latitude", "longitude"):
            if key not in body:
                continue
            if body[key] in ("", None):
                body[key] = None
            else:
                try:
                    body[key] = float(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be a number")
        return store.update_settings(body)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
