"""Ring: the Python side of the helper protocol, the page's Ring API, and the PIN on sensitive actions."""

import json
import os
import socket
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from home_control import ring as ring_mod
from home_control.auth import PinGate, hash_pin
from home_control.controller import Controller
from home_control.events import EventBus
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.ring import DemoRing, RingBridge, RingError
from home_control.scheduler import Scheduler
from home_control.server import create_app
from home_control.store import Store

FAKE = os.path.join(os.path.dirname(__file__), "fake_ring_helper.py")
PIN = "482916"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A RingBridge running the fake helper. Yields (bridge, bus, saved_tokens, log_path, set_mode)."""
    log = tmp_path / "helper.log"
    monkeypatch.setenv("FAKE_RING_LOG", str(log))
    saved = []
    bus = EventBus()
    token = {"value": "tok-1"}
    bridges = []

    def make(mode="normal", **kw):
        monkeypatch.setenv("FAKE_RING_MODE", mode)
        b = RingBridge([sys.executable, FAKE], get_token=lambda: (token["value"], "test"),
                       save_token=saved.append, bus=bus, port=free_port(), cwd=str(tmp_path),
                       log=lambda msg: None, **kw)
        bridges.append(b)
        return b

    yield make, bus, saved, log, token
    for b in bridges:
        b.stop()


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_helper_connects_saves_rotated_token_and_publishes_events(helper):
    make, bus, saved, log, _ = helper
    b = make()
    b.start()
    assert wait_for(lambda: b.status["connected"])
    assert wait_for(lambda: saved == ["tok-1-rotated"])  # rotated token saved at once
    assert b.status["cameras"] == 1 and b.status["error"] is None
    assert wait_for(lambda: bus.recent(source="ring"))
    ev = bus.recent(source="ring")[0]
    assert ev["kind"] == "motion" and ev["camera"]["name"] == "Front Door" and ev["source"] == "ring"

    started = read_log(log)[0]
    assert started["token"] == "tok-1"                      # arrived over stdin...
    assert not any("tok-1" in a for a in started["argv"])   # ...never on the command line

    assert b.get_json("/devices")["locations"][0]["modeName"] == "disarmed"
    assert b.request("GET", "/cameras/101/snapshot") == ("image/jpeg", b"\xff\xd8JPEG")
    assert b.post("/locations/L1/mode", {"mode": "some"}) == {"ok": True}
    assert {"path": "/locations/L1/mode", "body": {"mode": "some"}} in read_log(log)
    with pytest.raises(RingError, match="no such camera"):
        b.get_json("/cameras/999/events")


def test_signed_out_says_how_to_sign_in_and_starts_nothing(helper):
    make, bus, saved, log, token = helper
    token["value"] = None
    b = make(signed_out_delay=0.1)
    b.start()
    assert wait_for(lambda: b.status["error"] and "--ring-login" in b.status["error"])
    assert not b.status["connected"] and not b.status["signed_in"]
    assert read_log(log) == []
    with pytest.raises(RingError, match="--ring-login"):
        b.get_json("/devices")
    token["value"] = "tok-2"  # signed in meanwhile: picked up without a restart
    assert wait_for(lambda: b.status["connected"])


def test_helper_is_restarted_after_it_stops(helper):
    make, bus, saved, log, _ = helper
    b = make("crash", retry_delay=0.2)
    b.start()
    assert wait_for(lambda: sum(1 for e in read_log(log) if e.get("started")) >= 2)


def test_connect_and_errors_are_logged_without_the_token(helper, monkeypatch):
    make, bus, saved, log, _ = helper
    lines = []
    b = make()
    b.log = lines.append
    b.start()
    assert wait_for(lambda: any(line.startswith("Ring: connected, 1 cameras") for line in lines))
    assert not any("tok-1" in line for line in lines)
    b.stop()
    lines.clear()
    b2 = make("fail", retry_delay=5)
    b2.log = lines.append
    b2.start()
    assert wait_for(lambda: "Ring: Refresh token is not valid" in lines)


def test_helper_error_is_shown(helper):
    make, bus, saved, log, _ = helper
    b = make("fail", retry_delay=5)
    b.start()
    assert wait_for(lambda: b.status["error"] == "Ring: Refresh token is not valid")
    assert not b.status["connected"]


def test_ring_login_saves_the_token(monkeypatch):
    monkeypatch.setattr(ring_mod, "setup_problem", lambda node: None)
    saved = []

    def fake_run(cmd, **kw):
        assert cmd[-1].endswith("login.mjs") and kw["stdout"] == subprocess.PIPE
        return subprocess.CompletedProcess(cmd, 0, stdout='noise\n{"type":"token","refreshToken":"abc"}\n')

    assert ring_mod.ring_login("node", lambda t: saved.append(t) or "data/secrets.json", run=fake_run) == 0
    assert saved == ["abc"]


def test_ring_login_explains_missing_setup(monkeypatch, capsys):
    monkeypatch.setattr(ring_mod, "setup_problem", lambda node: "Node.js was not found")
    assert ring_mod.ring_login("node", lambda t: None) == 2
    assert "Node.js was not found" in capsys.readouterr().out


# ---------- the page's Ring API (with the demo Ring) ----------

@pytest.fixture
def page(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    store.update_settings({"allow_app_arming": True})  # as if Anthony allowed it; the default is off
    controller = Controller(Hub([DemoBackend()], store), store)
    controller.connect()
    bus = EventBus()
    bus.subscribe(lambda e: controller.touch())
    ring = DemoRing(bus)
    ring.start()
    gate = PinGate(hash_pin(PIN), b"s" * 32, required=True)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, ring=ring, bus=bus))
    client.post("/api/auth/login", json={"pin": PIN})
    yield client, ring, controller
    controller.stop_all()


def test_ring_pages_need_the_pin_cookie(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    gate = PinGate(hash_pin(PIN), b"s" * 32)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, ring=DemoRing(EventBus())))
    assert client.get("/api/ring/devices").status_code == 401
    assert client.get("/api/ring/cameras/1/snapshot").status_code == 401


def test_devices_events_snapshot_history(page):
    client, ring, controller = page
    d = client.get("/api/ring/devices").json()
    assert [c["name"] for c in d["cameras"]][0] == "Demo Doorbell" and d["locations"][0]["modeName"] == "disarmed"
    kinds = [e["kind"] for e in client.get("/api/ring/events").json()]
    assert kinds == ["doorbell", "sensor-open", "motion"]  # newest first
    snap = client.get("/api/ring/cameras/1/snapshot")
    assert snap.status_code == 200 and snap.headers["cache-control"] == "no-store"
    assert client.get("/api/ring/cameras/1/history").json()[0]["dingId"] == "9001"
    assert client.get("/api/ring/cameras/1/recording/9001").json()["url"].startswith("https://")
    assert client.get("/api/ring/cameras/1/recording/abc").status_code == 400
    assert client.get("/api/ring/cameras/nope/history").status_code == 502


def test_light_needs_no_pin_but_mode_and_sirens_need_it_every_time(page):
    client, ring, controller = page
    assert client.post("/api/ring/cameras/2/light", json={"on": True}).status_code == 200
    assert client.post("/api/ring/cameras/1/light", json={"on": True}).status_code == 502  # no light

    # Remembered device, but no PIN in the request: refused, and nothing reaches Ring.
    # (4 refusals here; a 5th wrong PIN in a row would start the lockout.)
    for path, body in (("/api/ring/locations/demo-home/mode", {"mode": "all"}),
                       ("/api/ring/locations/demo-home/siren", {"on": True}),
                       ("/api/ring/cameras/2/siren", {"on": True})):
        assert client.post(path, json=body).status_code == 403
    assert client.post("/api/ring/locations/demo-home/mode", json={"mode": "all", "pin": "000000"}).status_code == 403
    assert not any("/mode" in p or "/siren" in p for p, _ in ring.calls)

    assert client.post("/api/ring/locations/demo-home/mode", json={"mode": "all", "pin": PIN}).status_code == 200
    assert client.get("/api/ring/devices").json()["locations"][0]["modeName"] == "away"
    assert client.post("/api/ring/locations/demo-home/mode", json={"mode": "panic", "pin": PIN}).status_code == 400
    assert client.post("/api/ring/locations/demo-home/siren", json={"on": False, "pin": PIN}).status_code == 200


def test_wrong_pins_on_sensitive_actions_count_toward_the_lockout(page):
    client, ring, controller = page
    for _ in range(4):
        assert client.post("/api/ring/locations/demo-home/mode", json={"mode": "none", "pin": "111111"}).status_code == 403
    r = client.post("/api/ring/locations/demo-home/mode", json={"mode": "none", "pin": "111111"})
    assert r.status_code == 429
    assert client.post("/api/ring/locations/demo-home/mode", json={"mode": "none", "pin": PIN}).status_code == 429


def test_health_shows_ring_and_ring_events_count_as_activity(page):
    client, ring, controller = page
    h = client.get("/api/health").json()
    assert h["connections"]["ring"] == {"label": "Ring (demo)", "up": True}
    assert h["last_event_at"] is not None
    assert "Demo Doorbell" not in json.dumps(h)


def test_without_ring_the_page_says_so(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    gate = PinGate(None, b"s" * 32, required=False)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate))
    assert client.get("/api/ring/status").json()["connected"] is False
    assert client.get("/api/ring/devices").status_code == 503
    assert "ring" not in client.get("/api/health").json()["connections"]


def test_arming_from_the_app_is_off_until_anthony_decides(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    bus = EventBus()
    ring = DemoRing(bus)
    gate = PinGate(hash_pin(PIN), b"s" * 32, required=True)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, ring=ring, bus=bus))
    client.post("/api/auth/login", json={"pin": PIN})
    r = client.post("/api/ring/locations/demo-home/mode", json={"mode": "all", "pin": PIN})
    assert r.status_code == 403 and "switched off" in r.json()["detail"]
    assert not any("/mode" in p for p, _ in ring.calls)
    assert store.get("settings")["allow_app_arming"] is False


def test_camera_siren_stops_itself(page):
    client, ring, controller = page
    r = client.post("/api/ring/cameras/2/siren", json={"on": True, "pin": PIN})
    assert r.status_code == 200 and r.json()["seconds"] == 30
    sirens = client.app.state.sirens
    assert sirens.running() == ["2"]
    token = sirens._timers["2"][1]
    sirens._off("2", token)  # what the 30-second timer does
    assert [b for p, b in ring.calls if p == "/cameras/2/siren"] == [{"on": True}, {"on": False}]
    assert sirens.running() == []
    assert client.post("/api/ring/cameras/1/siren", json={"on": True, "pin": PIN}).status_code == 502  # no siren


def test_camera_sirens_timer_and_stop_all():
    from home_control.ring import CameraSirens

    class FakeTimer:
        made = []

        def __init__(self, seconds, fn, args=()):
            self.seconds, self.fn, self.args, self.cancelled = seconds, fn, args, False
            FakeTimer.made.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    posts = []
    ring = type("R", (), {"post": lambda self, path, body: posts.append((path, body))})()
    s = CameraSirens(ring, seconds=30, timer=FakeTimer, log=lambda m: None)
    assert s.start("cam 7", 500) == 120          # capped at 120 s
    assert s.start("cam 7") == 30                 # restarting replaces the old timer
    assert FakeTimer.made[0].cancelled and FakeTimer.made[1].seconds == 30
    FakeTimer.made[0].fn(*FakeTimer.made[0].args)  # the old timer firing late changes nothing
    assert s.running() == ["cam 7"] and ("/cameras/cam%207/siren", {"on": False}) not in posts
    s.start("9")
    s.stop_all()
    assert s.running() == []
    assert ("/cameras/cam%207/siren", {"on": False}) in posts and ("/cameras/9/siren", {"on": False}) in posts


def test_siren_off_is_retried():
    from home_control.ring import CameraSirens
    tries = []

    class Flaky:
        def post(self, path, body):
            tries.append(body)
            if body == {"on": False} and len(tries) < 3:
                raise RingError("helper busy")

    s = CameraSirens(Flaky(), timer=lambda *a, **k: type("T", (), {"start": lambda self: None,
                                                                   "cancel": lambda self: None,
                                                                   "daemon": True})(),
                     sleep=lambda s: None, log=lambda m: None)
    s.start("1")
    s.stop("1")
    assert tries == [{"on": True}, {"on": False}, {"on": False}]


class _T:
    made = []

    def __init__(self, seconds, fn, args=()):
        self.seconds, self.fn, self.args = seconds, fn, args
        _T.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


def test_a_failed_on_call_still_gets_its_off():
    from home_control.ring import CameraSirens
    posts = []

    class Late:
        def post(self, path, body):
            posts.append(body)
            if body == {"on": True}:
                raise RingError("timed out")      # but Ring may have started the siren

    _T.made = []
    s = CameraSirens(Late(), timer=_T, log=lambda m: None, sleep=lambda x: None)
    with pytest.raises(RingError):
        s.start("4")
    assert s.running() == ["4"]                    # the stop timer was set first
    _T.made[0].fn(*_T.made[0].args)
    assert posts == [{"on": True}, {"on": False}]


def test_sirens_left_on_by_a_stopped_app_are_turned_off_after_restart(tmp_path):
    from home_control.ring import CameraSirens
    path = str(tmp_path / "data" / "sirens.json")
    clock = {"t": 1000.0}
    posts = []
    ring = type("R", (), {"post": lambda self, p, b: posts.append((p, b))})()
    _T.made = []
    first = CameraSirens(ring, timer=_T, state_path=path, clock=lambda: clock["t"], log=lambda m: None)
    first.start("1", 30)
    first.start("2", 120)                          # the app is then killed: its timers are gone
    clock["t"] += 60
    posts.clear()
    bus = EventBus()
    second = CameraSirens(ring, timer=_T, state_path=path, bus=bus, clock=lambda: clock["t"], log=lambda m: None)
    second.recover()                               # what the "ready" event does, off its thread
    assert posts == [("/cameras/1/siren", {"on": False})]   # past due: off at once
    assert second.running() == ["2"] and abs(_T.made[-1].seconds - 60) < 1   # timed again for the rest
    _T.made[-1].fn(*_T.made[-1].args)
    second.recover()
    assert posts[-1] == ("/cameras/2/siren", {"on": False}) and second.running() == []


def test_the_app_arming_switch_needs_the_pin(page):
    client, ring, controller = page
    assert client.post("/api/mode/app-arming", json={"on": False}).status_code == 403
    r = client.post("/api/mode/app-arming", json={"on": False, "pin": PIN})
    assert r.status_code == 200 and r.json() == {"app_arming": False}
