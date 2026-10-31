"""Rules: when / only if / then / until, the safe test, STOP, and the safety limits."""

from datetime import datetime

import pytest

from home_control.controller import Controller
from home_control.events import EventBus
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.modes import HouseModes
from home_control.ring import CameraSirens, DemoRing
from home_control.rules import RulesEngine, in_window, red_alert_template, validate
from home_control.store import Store


class Timer:
    made = []

    def __init__(self, seconds, fn, args=()):
        self.seconds, self.fn, self.args, self.cancelled = seconds, fn, args, False
        Timer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn(*self.args)


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, title, text, stop_url=None):
        self.sent.append((title, text, stop_url))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    Timer.made = []
    store = Store(str(tmp_path / "s.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    controller.connect()
    bus = EventBus()
    ring = DemoRing(bus)
    modes = HouseModes(store, bus, ring)
    modes.refresh()
    sirens = CameraSirens(ring, timer=Timer, log=lambda m: None)
    notifier = Notifier()
    clock = Clock()
    now = {"t": datetime(2026, 10, 31, 23, 0).astimezone()}
    engine = RulesEngine(store, bus, controller, sirens=sirens, ring=ring, modes=modes, notifier=notifier,
                         stop_url=lambda token: f"/api/rules/stop/{token}", clock=clock,
                         now=lambda: now["t"], timer=Timer, log=lambda m: None)
    yield engine, ring, modes, bus, notifier, clock, now, controller, store
    controller.stop_all()


def set_mode(bus, mode):
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": mode, "location": {"id": "demo-home"}})


def motion(bus, cam="2"):
    bus.publish({"source": "ring", "kind": "motion", "camera": {"id": cam, "name": "x"}})


def test_validate():
    assert validate(red_alert_template())["name"] == "Red Alert"
    for bad, msg in [({}, "name"), ({"name": "x", "when": {"kind": "rain"}}, "'when'"),
                     ({"name": "x", "when": {"kind": "time", "time": "25:00"}, "then": [{"do": "alert"}]}, "time"),
                     ({"name": "x", "when": {"kind": "motion"}}, "at least one action"),
                     ({"name": "x", "when": {"kind": "motion"}, "then": [{"do": "explode"}]}, "'do'"),
                     ({"name": "x", "when": {"kind": "motion"}, "then": [{"do": "mode", "mode": "home"}]},
                      "never disarm"),
                     ({"name": "x", "when": {"kind": "motion"}, "then": [{"do": "alert"}],
                       "only_if": {"modes": ["PARTY"]}}, "house mode"),
                     ({"name": "x", "when": {"kind": "motion"}, "then": [{"do": "alert"}],
                       "until": {"minutes": 600}}, "minutes")]:
        with pytest.raises(ValueError, match=msg):
            validate(bad)


def test_time_window():
    late, noon = datetime(2026, 1, 1, 23, 30), datetime(2026, 1, 1, 12, 0)
    assert in_window(["22:00", "06:00"], late) and not in_window(["22:00", "06:00"], noon)
    assert in_window(["09:00", "17:00"], noon)


def test_red_alert_runs_until_stop_and_cleans_up(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    rule = dict(red_alert_template(), enabled=True)
    engine.put(rule)
    motion(bus)                       # HOME: nothing happens
    assert not engine.runs
    set_mode(bus, "all")              # AWAY
    motion(bus)
    run = engine.runs[0]
    steps = {s["do"]: s for s in run.steps}
    assert steps["lights"]["ok"] is True and controller.effects_running() == 1
    assert steps["camera_siren"]["ok"] and steps["camera_siren"]["note"] == "1 siren(s) for 30 s"
    assert steps["sonos_sound"]["ok"] is None and "phase 2" in steps["sonos_sound"]["note"]
    assert steps["snapshot"]["ok"] and "2" in engine.snapshots
    title, text, stop_url = notifier.sent[0]
    assert title == "Red Alert" and stop_url == f"/api/rules/stop/{run.stop_token}"
    motion(bus)                       # already running: not started twice
    assert len(engine.runs) == 1
    assert engine.stop_by_token("wrong") is False
    assert engine.stop_by_token(run.stop_token) is True
    assert run.end_reason == "STOP from the phone" and controller.effects_running() == 0
    assert ("/cameras/2/siren", {"on": False}) in ring.calls
    assert engine.stop_by_token(run.stop_token) is False      # single use


def test_time_limit_mode_change_and_cooldown(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    engine.put(dict(red_alert_template(), enabled=True))
    set_mode(bus, "some")             # NIGHT
    motion(bus)
    run = engine.runs[0]
    limit = next(t for t in Timer.made if t.fn == engine.end)
    assert limit.seconds == 300
    limit.fire()
    assert run.end_reason == "time limit"
    clock.t += 60
    motion(bus)                       # cooling down (600 s)
    assert len(engine.runs) == 1
    clock.t += 600
    motion(bus)
    second = engine.runs[0]
    assert second is not run and second.ended is None
    set_mode(bus, "none")             # disarmed: "until mode_is HOME" ends it
    assert second.end_reason == "house mode became HOME"


def test_safe_test_runs_only_lights_and_alert(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    rule = engine.put(red_alert_template())   # off: tests still run
    run = engine.test(rule["id"])
    done = [s["do"] for s in run.steps if s["ok"]]
    skipped = [s["do"] for s in run.steps if s["note"] == "skipped in the safe test"]
    assert done == ["lights", "alert"]
    assert skipped == ["camera_siren", "sonos_sound", "snapshot"]
    assert not any("/siren" in p for p, _ in ring.calls)
    assert notifier.sent[0][0] == "Test: Red Alert"
    assert next(t for t in Timer.made if t.fn == engine.end).seconds == 20


def test_disabled_rules_and_windows(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    engine.put({"name": "Night porch", "enabled": True, "when": {"kind": "motion", "camera": "1"},
                "only_if": {"between": ["22:00", "06:00"]}, "then": [{"do": "lights", "target": "all", "on": True}]})
    engine.put({"name": "Off", "enabled": False, "when": {"kind": "motion"}, "then": [{"do": "alert"}]})
    motion(bus, cam="2")              # wrong camera
    assert not engine.runs
    motion(bus, cam="1")
    assert engine.runs[0].rule["name"] == "Night porch" and engine.runs[0].end_reason == "done"
    now["t"] = datetime(2026, 10, 31, 12, 0).astimezone()
    clock.t += 1
    motion(bus, cam="1")
    assert len(engine.runs) == 1      # outside its window


def test_clock_triggers_fire_once_a_day(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    engine.put({"name": "Lights out", "when": {"kind": "time", "time": "23:00"},
                "then": [{"do": "lights", "target": "all", "on": False}]})
    engine.tick(datetime(2026, 10, 31, 22, 59).astimezone())
    assert not engine.runs
    engine.tick(datetime(2026, 10, 31, 23, 1).astimezone())
    engine.tick(datetime(2026, 10, 31, 23, 3).astimezone())
    assert len(engine.runs) == 1


def test_garage_and_house_mode_triggers_and_buttons(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    g = engine.put({"name": "Garage left open", "when": {"kind": "garage", "state": "open"},
                    "then": [{"do": "alert", "text": "The garage is open"}]})
    b = engine.put({"name": "Movie", "when": {"kind": "button"}, "then": [{"do": "lights", "target": "all", "on": False}]})
    bus.publish({"source": "garage", "kind": "garage-closed"})
    assert not engine.runs
    bus.publish({"source": "garage", "kind": "garage-open"})
    assert notifier.sent[-1][1] == "The garage is open"
    assert engine.press(b["id"]).steps[0]["ok"] is True
    with pytest.raises(ValueError):
        engine.press(g["id"])


def test_rules_never_disarm_and_arm_only_when_allowed(env):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    engine.put({"name": "Arm at night", "when": {"kind": "button"}, "then": [{"do": "mode", "mode": "NIGHT"}]})
    run = engine.press(engine.rules()[0]["id"])
    assert run.steps[0]["ok"] is False and "switched off" in run.steps[0]["note"]
    store.update_settings({"allow_app_arming": True})
    clock.t += 1
    run = engine.press(engine.rules()[0]["id"])
    assert run.steps[0]["ok"] is True and modes.mode == "NIGHT"


def test_push_alerts_not_set_up_is_reported_not_fatal(env, tmp_path):
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    plain = RulesEngine(store, EventBus(), controller, timer=Timer, log=lambda m: None)
    plain.put({"name": "Ping", "when": {"kind": "button"},
               "then": [{"do": "alert", "text": "hi"}, {"do": "lights", "target": "all", "on": True}]})
    run = plain.press(plain.rules()[0]["id"])
    assert run.steps[0]["ok"] is None and "aren't set up" in run.steps[0]["note"]
    assert run.steps[1]["ok"] is True


def test_routes(env):
    from fastapi.testclient import TestClient

    from home_control.auth import PinGate, hash_pin
    from home_control.scheduler import Scheduler
    from home_control.server import create_app
    engine, ring, modes, bus, notifier, clock, now, controller, store = env
    gate = PinGate(hash_pin("482916"), b"k" * 32, required=True)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, ring=ring, bus=bus,
                                   modes=modes, sirens=engine.sirens, rules=engine))
    assert client.get("/api/rules").status_code == 401
    client.post("/api/auth/login", json={"pin": "482916"})
    r = client.post("/api/rules", json=dict(red_alert_template(), enabled=True))
    assert r.status_code == 200
    rid = r.json()["id"]
    assert client.post("/api/rules", json={"name": "x"}).status_code == 400
    body = client.get("/api/rules").json()
    assert body["template"]["name"] == "Red Alert" and "motion" in body["triggers"]
    t = client.post(f"/api/rules/{rid}/test").json()
    assert t["started"] and t["run"]["test"] is True
    assert client.post(f"/api/rules/{rid}/stop").json()["stopped"] is True
    set_mode(bus, "all")
    motion(bus)
    token = engine.runs[0].stop_token
    anon = TestClient(client.app)        # the phone: no cookie
    assert anon.post("/api/rules/stop/not-the-token").status_code == 404
    assert anon.post(f"/api/rules/stop/{token}").json() == {"stopped": True}
    assert anon.get("/api/rules").status_code == 401
    assert client.delete(f"/api/rules/{rid}").status_code == 200
    assert client.delete(f"/api/rules/{rid}").status_code == 404
