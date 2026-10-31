"""Local web server: the JSON API plus the control page.

On its own, everything under /api/ needs a remembered device (the page PIN), except
/api/health (for ServerWatch; no names, addresses or secrets) and /api/auth/*.

Behind the Bridge (--bridge-link), the app listens on 127.0.0.1 only and every request except
/api/health must carry the Bridge's signature (fusion-link/1, fusionkit/link.py). The Bridge
holds the one sign-in, so a signed request counts as signed in. The PIN for each alarm or
siren action is still asked for every time, as before.
"""

import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, health_feed, patterns, scheduler as sched
from .fusionkit import link
from .auth import COOKIE_NAME, REMEMBER_SECONDS, PinError
from .govee.base import BackendError
from .ring import CameraSirens, RingError
from .services import ServiceError

# Settings the page may change. Others (secrets store, DLL path, credential
# names) are only set from the command line on the server itself.
PAGE_SETTINGS = {"latitude", "longitude", "default_fps", "use_govee_desktop"}
OPEN_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}
# The phone's STOP button (a push alert's action) has no cookie: its random per-run token is the key.
# Its path is shared with no other route, and only this exact shape is let through without the PIN.
OPEN_STOP = re.compile(r"/api/stop/[A-Za-z0-9_-]{1,64}")

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


class OnBody(BaseModel):
    on: bool


class OnPinBody(OnBody):
    pin: str = ""


class ModeBody(BaseModel):
    mode: str  # "none" (disarmed), "some" (home) or "all" (away)
    pin: str = ""


class ScheduleBody(BaseModel):
    id: Optional[str] = None
    name: str
    enabled: bool = True
    days: list[int] = []
    when: dict
    target: str
    action: dict


class PinOnlyBody(BaseModel):
    pin: Optional[str] = None


class ModeChangeBody(BaseModel):
    mode: str = Field(..., max_length=12)
    pin: Optional[str] = None


class GarageSetupBody(BaseModel):
    camera_id: str = Field(..., max_length=40)
    box: list[float] = Field(..., min_length=4, max_length=4)


class GarageRefBody(BaseModel):
    kind: str = Field(..., max_length=8)


class ActBody(BaseModel):
    target: Optional[str] = None
    action: str = Field(..., max_length=40)
    args: Optional[dict] = None


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def confirm_with_pin(gate, pin):
    """For arm, disarm and sirens: the PIN is required on every request, even on a remembered device."""
    try:
        gate.check_action_pin(pin)
    except PinError as e:
        raise HTTPException(429 if e.locked_for else 403, str(e))


def home_feed(controller, ring=None, services=(), started_at=None, now=None):
    """Home Control's answer in the kit's health feed (health_feed.py): counts and states only."""
    now = now or time.time()
    st = controller.status()
    backends = st["backends"].values()
    lights_up = [b for b in backends if b["connected"]]
    total = st["device_count"]
    on = sum(1 for s in controller.state.values() if s.get("on")) if total else 0
    if not backends or not lights_up:
        light_status = "down"
    elif len(lights_up) < len(backends):
        light_status = "warn"
    else:
        light_status = "ok"
    parts = [("Lights", f"{on} of {total} on" if lights_up else "not answering", light_status)]
    if ring is not None:
        up = bool(ring.status.get("connected"))
        cams = ring.status.get("cameras") or 0
        parts.append(("Ring", f"{cams} cameras" if up else "not connected", "ok" if up else "down"))
    for svc in services:
        try:
            text = svc.health_text()
        except Exception:  # a service's own text must never break the feed
            text = None
        up = bool(svc.status.get("up"))
        parts.append((svc.label, text or ("answering" if up else "not answering"), "ok" if up else "down"))
    statuses = [p[2] for p in parts]
    if all(s == "down" for s in statuses):
        status = "down"
    elif any(s != "ok" for s in statuses):
        status = "warn"
    else:
        status = "ok"
    first = f"{on} of {total} lights on" if lights_up else "Lights not answering"
    trouble = [p[0] for p in parts[1:] if p[2] != "ok"]
    if status == "ok":
        summary = f"{first}; all devices answering"
    elif trouble:
        summary = f"{first}; not answering: {', '.join(trouble)}"
    else:
        summary = first
    if light_status == "warn":
        summary += "; a light connection is down"
    return health_feed.make_feed("home-control", "Home Control", __version__, status, summary, parts,
                                 started_at=started_at, now=now)


def create_app(controller, store, scheduler, gate, started_at=None, ring=None, bus=None, services=None,
               modes=None, sirens=None, rules=None, bridge_secret=None):
    app = FastAPI(title="Home Control")
    started_at = started_at or time.time()
    services = dict(services or {})  # key -> PollingService (sonos, firetv, pcs, ...)
    if sirens is None and ring is not None:
        sirens = CameraSirens(ring)
    app.state.sirens = sirens

    def ring_call(method, *args):
        if ring is None:
            raise HTTPException(503, "Ring isn't set up on this server")
        try:
            return getattr(ring, method)(*args)
        except RingError as e:
            raise HTTPException(502, str(e))

    @app.middleware("http")
    async def require_pin(request: Request, call_next):
        path = request.scope["path"]   # the same path the router uses
        if bridge_secret is not None and path != "/api/health":
            who = link.verify(bridge_secret, request.method, link.signed_path(request.scope), request.headers)
            if who is None or who["route"] != "tailscale":
                return JSONResponse({"detail": "Home Control is reached through the Bridge"}, status_code=401)
            request.state.who = who
            return await call_next(request)   # signed in at the Bridge
        if path.startswith("/api/") and path not in OPEN_PATHS and not OPEN_STOP.fullmatch(path):
            if not gate.cookie_ok(request.cookies.get(COOKIE_NAME)):
                return JSONResponse({"detail": "PIN required", "pin_required": True}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        """The kit's health feed (schema fusion-health/1), plus V1's fields for older readers."""
        now = time.time()
        st = controller.status()
        connections = {route: {"label": b["label"], "up": bool(b["connected"])} for route, b in st["backends"].items()}
        if ring is not None:
            connections["ring"] = {"label": ring.status["label"], "up": bool(ring.status["connected"])}
        for key, svc in services.items():
            connections[key] = {"label": svc.label, "up": bool(svc.status.get("up"))}
        feed = home_feed(controller, ring, services.values(), started_at, now)
        feed.update({
            "connections": connections,
            "light_count": st["device_count"],
            "effects_running": controller.effects_running(),
            "last_event_at": iso(controller.last_activity),
        })
        return feed

    # ---------- V2 device services (Sonos, Fire TV, PC status, ...) ----------

    def service(key):
        svc = services.get(key)
        if svc is None:
            raise HTTPException(404, f"no service {key!r} on this server")
        return svc

    @app.get("/api/services")
    def services_state():
        out = {}
        for key, svc in services.items():
            out[key] = {"label": svc.label, "status": {k: v for k, v in svc.status.items() if k != "checked_at"},
                        "checked_at": iso(svc.status.get("checked_at")), "state": svc.state()}
        return out

    @app.post("/api/services/{key}/act")
    def service_act(key: str, body: ActBody):
        svc = service(key)
        try:
            result = svc.act(body.target, body.action, body.args or {})
        except ServiceError as e:
            raise HTTPException(502, str(e))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        return {"ok": True, "result": result, "state": svc.state()}

    # ---------- house modes and the garage setup ----------

    @app.get("/api/mode")
    def mode_state():
        if modes is None:
            return {"mode": None, "modes": [], "app_arming": False}
        if modes.mode is None:
            modes.refresh()
        return modes.state()

    @app.post("/api/mode")
    def mode_change(body: ModeChangeBody):
        """Changing the house mode. Arming or disarming needs the PIN every time (and Anthony's switch)."""
        if modes is None:
            raise HTTPException(503, "Ring isn't set up on this server")
        from .modes import ModeRefused
        target = str(body.mode).upper()
        try:
            # The PIN is checked inside change(), on the same reading of the alarm as the change.
            modes.change(target, confirm=lambda: confirm_with_pin(gate, body.pin))
        except ModeRefused as e:
            raise HTTPException(403 if "switched off" in str(e) else 409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RingError as e:
            raise HTTPException(502, str(e))
        return modes.state()

    @app.post("/api/mode/app-arming")
    def set_app_arming(body: OnPinBody):
        """Anthony's switch for arming and disarming from the app (off at first). The PIN, every time."""
        confirm_with_pin(gate, body.pin)
        store.update_settings({"allow_app_arming": bool(body.on)})
        return {"app_arming": bool(body.on)}

    def garage():
        svc = services.get("garage")
        if svc is None:
            raise HTTPException(503, "Ring isn't set up on this server")
        return svc

    @app.post("/api/garage/setup")
    def garage_setup(body: GarageSetupBody):
        try:
            garage().setup(body.camera_id, body.box)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return garage().state()

    @app.post("/api/garage/reference")
    def garage_reference(body: GarageRefBody):
        try:
            count = garage().save_reference(body.kind)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except ServiceError as e:
            raise HTTPException(502, str(e))
        return dict(garage().state(), saved=count)

    # ---------- rules ----------

    def engine():
        if rules is None:
            raise HTTPException(503, "rules aren't running on this server")
        return rules

    @app.get("/api/rules")
    def rules_state():
        return engine().state()

    @app.post("/api/rules")
    def rules_put(body: dict):
        """Save a rule. One that sounds sirens or arms the alarm needs the PIN, every time."""
        from .rules import needs_pin
        pin = body.pop("pin", None)
        if needs_pin(body):
            confirm_with_pin(gate, pin)
        try:
            return engine().put(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/rules/{rid}")
    def rules_delete(rid: str):
        try:
            engine().delete(rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        return {"ok": True}

    def rule_run(fn, rid):
        try:
            run = fn(rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"started": run is not None, "run": run.to_dict() if run else None}

    @app.post("/api/rules/{rid}/test")
    def rules_test(rid: str):
        """The safe test: only the lights and the phone alert; it ends by itself after 20 seconds."""
        return rule_run(engine().test, rid)

    @app.post("/api/rules/{rid}/press")
    def rules_press(rid: str, body: Optional[PinOnlyBody] = None):
        """A button rule. One that sounds sirens or arms the alarm needs the PIN, every time."""
        from .rules import needs_pin
        try:
            rule = engine().rule(rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        if needs_pin(rule):
            confirm_with_pin(gate, body.pin if body else None)
        return rule_run(engine().press, rid)

    @app.post("/api/rules/{rid}/stop")
    def rules_stop(rid: str):
        return {"stopped": engine().stop_rule(rid, "STOP on the page")}

    @app.post("/api/rules-stop-all")
    def rules_stop_all():
        engine().stop_all("STOP ALL on the page")
        return {"ok": True}

    @app.get("/api/rules/snapshots/{cid}")
    def rules_snapshot(cid: str):
        """The latest picture a rule took from this camera (kept in memory only, one per camera)."""
        shot = engine().snapshots.get(cid)
        if shot is None:
            raise HTTPException(404, "no snapshot from this camera yet")
        taken, ctype, data = shot
        return Response(content=data, media_type=ctype or "image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Taken-At": iso(taken) or ""})

    @app.post("/api/stop/{token}")
    def rules_stop_token(token: str):
        """For the STOP button on a push alert. No cookie; the run's own random token, single use."""
        if not OPEN_STOP.fullmatch(f"/api/stop/{token}") or not engine().stop_by_token(token):
            raise HTTPException(404, "nothing to stop")
        return {"stopped": True}

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        via_bridge = getattr(request.state, "who", None) is not None
        return {"required": gate.required, "pin_set": gate.pin_set, "via_bridge": via_bridge,
                "signed_in": via_bridge or gate.cookie_ok(request.cookies.get(COOKIE_NAME)),
                "locked_for": gate.locked_for()}

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

    # ---------- Ring ----------
    # Arming, disarming and any siren need the PIN on every request (confirm_with_pin),
    # even from a remembered device. Nothing here can contact Ring's monitoring center.

    @app.get("/api/ring/status")
    def ring_status():
        if ring is None:
            return {"label": "Ring", "connected": False, "signed_in": False, "error": "Ring isn't set up on this server"}
        return dict(ring.status)

    @app.get("/api/ring/devices")
    def ring_devices():
        return ring_call("get_json", "/devices")

    @app.get("/api/ring/events")
    def ring_events(limit: int = 50):
        return bus.recent(source="ring", limit=max(1, min(200, limit))) if bus else []

    @app.get("/api/ring/cameras/{cid}/snapshot")
    def ring_snapshot(cid: str):
        content_type, data = ring_call("request", "GET", f"/cameras/{quote(cid, safe='')}/snapshot")
        return Response(content=data, media_type=content_type or "image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/ring/cameras/{cid}/history")
    def ring_history(cid: str, limit: int = 20):
        return ring_call("get_json", f"/cameras/{quote(cid, safe='')}/events?limit={max(1, min(50, limit))}")

    @app.get("/api/ring/cameras/{cid}/recording/{ding}")
    def ring_recording(cid: str, ding: str):
        if not ding.isdigit():
            raise HTTPException(400, "bad recording id")
        return ring_call("get_json", f"/cameras/{quote(cid, safe='')}/recording/{ding}")

    @app.post("/api/ring/cameras/{cid}/light")
    def ring_light(cid: str, body: OnBody):
        return ring_call("post", f"/cameras/{quote(cid, safe='')}/light", {"on": body.on})

    @app.post("/api/ring/cameras/{cid}/siren")
    def ring_camera_siren(cid: str, body: OnPinBody):
        """A camera siren: on runs for camera_siren_seconds (30) and then this app stops it."""
        confirm_with_pin(gate, body.pin)
        if ring is None:
            raise HTTPException(503, "Ring isn't set up on this server")
        try:
            if body.on:
                seconds = sirens.start(cid, store.get("settings").get("camera_siren_seconds") or 30)
                return {"ok": True, "seconds": seconds}
            sirens.stop(cid)
            return {"ok": True}
        except RingError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/ring/locations/{lid}/mode")
    def ring_mode(lid: str, body: ModeBody):
        if body.mode not in ("none", "some", "all"):
            raise HTTPException(400, "mode must be none (disarmed), some (home) or all (away)")
        if not store.get("settings").get("allow_app_arming"):
            raise HTTPException(403, "Arming and disarming from this app is switched off. Turn it on in the "
                                     "app with your PIN, or use the Ring app or the keypad.")
        confirm_with_pin(gate, body.pin)
        return ring_call("post", f"/locations/{quote(lid, safe='')}/mode", {"mode": body.mode})

    @app.post("/api/ring/locations/{lid}/siren")
    def ring_location_siren(lid: str, body: OnPinBody):
        confirm_with_pin(gate, body.pin)
        return ring_call("post", f"/locations/{quote(lid, safe='')}/siren", {"on": body.on})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
