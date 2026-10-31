"""House modes follow the Ring Alarm; changing it from the app waits for Anthony's decision."""

import pytest

from home_control.events import EventBus
from home_control.modes import HouseModes, ModeRefused, house_mode
from home_control.ring import DemoRing
from home_control.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / "s.json"))
    bus = EventBus()
    ring = DemoRing(bus)
    modes = HouseModes(store, bus, ring)
    modes.refresh()
    return store, bus, ring, modes


def test_mapping():
    assert house_mode("none") == "HOME" and house_mode("some") == "NIGHT"
    assert house_mode("all") == "AWAY" and house_mode("all", vacation=True) == "VACATION"
    assert house_mode(None) is None


def test_reads_ring_and_follows_changes_made_elsewhere(setup):
    store, bus, ring, modes = setup
    assert modes.mode == "HOME" and modes.location_id == "demo-home"
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": "some", "location": {"id": "demo-home"}})
    assert modes.mode == "NIGHT"
    events = [e for e in bus.recent() if e.get("kind") == "house-mode"]
    assert events[0]["mode"] == "NIGHT" and events[0]["was"] == "HOME" and events[0]["by"] == "ring"


def test_app_arming_is_off_by_default(setup):
    store, bus, ring, modes = setup
    with pytest.raises(ModeRefused, match="switched off"):
        modes.change("AWAY")
    assert not ring.calls and modes.mode == "HOME"


def test_vacation_is_a_local_flag_while_armed_away(setup):
    store, bus, ring, modes = setup
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": "all", "location": {"id": "demo-home"}})
    assert modes.mode == "AWAY"
    assert modes.change("VACATION") == "VACATION"   # no call to Ring: the alarm stays armed away
    assert not ring.calls and store.get("settings")["vacation"] is True
    assert modes.change("AWAY") == "AWAY" and store.get("settings")["vacation"] is False
    modes.change("VACATION")
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": "none", "location": {"id": "demo-home"}})
    assert modes.mode == "HOME" and store.get("settings")["vacation"] is False  # disarming ends a vacation


def test_when_allowed_the_app_arms_but_a_rule_never_disarms(setup):
    store, bus, ring, modes = setup
    store.update_settings({"allow_app_arming": True})
    assert modes.change("NIGHT") == "NIGHT"
    assert ring.calls[-1] == ("/locations/demo-home/mode", {"mode": "some"})
    with pytest.raises(ModeRefused, match="never disarm"):
        modes.change("HOME", from_rule=True)
    assert modes.change("AWAY", from_rule=True) == "AWAY"
    assert modes.change("HOME") == "HOME"  # the app itself may, once allowed (with the PIN, at the route)


def test_bad_and_unknown(tmp_path):
    store = Store(str(tmp_path / "s.json"))
    modes = HouseModes(store, EventBus(), ring=None)
    with pytest.raises(ValueError):
        modes.change("PARTY")
    with pytest.raises(ModeRefused, match="isn't known yet"):
        modes.change("AWAY")
    assert modes.state() == {"mode": None, "modes": ["HOME", "NIGHT", "AWAY", "VACATION"], "app_arming": False}


# ---------- the routes ----------

def app_with_modes(tmp_path, allow=False):
    from fastapi.testclient import TestClient

    from home_control.auth import PinGate, hash_pin
    from home_control.controller import Controller
    from home_control.govee.demo import DemoBackend
    from home_control.hub import Hub
    from home_control.scheduler import Scheduler
    from home_control.server import create_app
    store = Store(str(tmp_path / "app.json"))
    store.update_settings({"allow_app_arming": allow})
    controller = Controller(Hub([DemoBackend()], store), store)
    bus = EventBus()
    ring = DemoRing(bus)
    modes = HouseModes(store, bus, ring)
    gate = PinGate(hash_pin("482916"), b"k" * 32, required=True)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, ring=ring, bus=bus,
                                   modes=modes))
    client.post("/api/auth/login", json={"pin": "482916"})
    return client, ring, modes


def test_mode_routes_when_switched_off(tmp_path):
    client, ring, modes = app_with_modes(tmp_path)
    assert client.get("/api/mode").json()["mode"] == "HOME"
    r = client.post("/api/mode", json={"mode": "AWAY", "pin": "482916"})
    assert r.status_code == 403 and not ring.calls


def test_mode_routes_when_allowed_need_the_pin_every_time(tmp_path):
    client, ring, modes = app_with_modes(tmp_path, allow=True)
    client.get("/api/mode")
    assert client.post("/api/mode", json={"mode": "NIGHT"}).status_code == 403
    assert client.post("/api/mode", json={"mode": "NIGHT", "pin": "000000"}).status_code == 403
    assert not ring.calls
    r = client.post("/api/mode", json={"mode": "NIGHT", "pin": "482916"})
    assert r.status_code == 200 and r.json()["mode"] == "NIGHT"
    assert client.post("/api/mode", json={"mode": "PARTY", "pin": "482916"}).status_code == 400


def test_garage_routes_without_ring(tmp_path):
    client, ring, modes = app_with_modes(tmp_path)
    assert client.post("/api/garage/setup", json={"camera_id": "3", "box": [0, 0, 0.3, 0.3]}).status_code == 503


def test_reads_the_alarm_again_after_ring_reconnects(tmp_path):
    import time as _t
    store = Store(str(tmp_path / "r.json"))
    bus = EventBus()
    ring = DemoRing(bus)
    modes = HouseModes(store, bus, ring)
    assert modes.mode is None
    ring.state["locations"][0].update(mode="all", modeName="away")   # armed at the keypad while away
    bus.publish({"source": "ring", "kind": "ready"})
    for _ in range(50):
        if modes.mode:
            break
        _t.sleep(0.02)
    assert modes.mode == "AWAY"


def test_an_unknown_mode_changes_nothing(setup):
    store, bus, ring, modes = setup
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": "all", "location": {"id": "demo-home"}})
    modes.change("VACATION")
    bus.publish({"source": "ring", "kind": "alarm-mode", "mode": None, "location": {"id": "demo-home"}})
    assert modes.mode == "VACATION" and store.get("settings")["vacation"] is True
