"""Local web server: the JSON API plus the control page at http://127.0.0.1:8765/."""

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import patterns, scheduler as sched
from .govee.base import BackendError

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Target(BaseModel):
    target: str


class PowerBody(Target):
    on: bool


class BrightnessBody(Target):
    value: int = Field(ge=1, le=100)


class ColorBody(Target):
    color: str


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


class ScheduleBody(BaseModel):
    id: Optional[str] = None
    name: str
    enabled: bool = True
    days: list[int] = []
    when: dict
    target: str
    action: dict


def create_app(controller, store, scheduler):
    app = FastAPI(title="Home Control")

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
        return dict(controller.status, device_count=len(controller.devices))

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
        except BackendError as e:
            raise HTTPException(502, str(e))
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
        for key in ("latitude", "longitude"):
            if body.get(key) in ("", None):
                body[key] = None
            elif key in body:
                try:
                    body[key] = float(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be a number")
        return store.update_settings(body)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
