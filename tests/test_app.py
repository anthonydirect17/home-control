"""End-to-end tests through the HTTP API, using the demo backend."""

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from home_control.auth import PinGate
from home_control.controller import Controller
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.scheduler import Scheduler
from home_control.server import create_app
from home_control.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    backend = DemoBackend()
    controller = Controller(Hub([backend], store), store)
    controller.connect()
    sched = Scheduler(controller, store)
    open_gate = PinGate(None, b"k" * 32, required=False)  # demo-style: no PIN; PIN tests are in test_security.py
    client = TestClient(create_app(controller, store, sched, open_gate))
    yield client, backend, controller, store, sched
    controller.stop_all()


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_page_and_status(env):
    client, *_ = env
    assert "Home Control" in client.get("/").text
    s = client.get("/api/status").json()
    assert s["connected"] and s["device_count"] == 4


def test_power_brightness_color(env):
    client, backend, *_ = env
    t = "device:Demo Lamp"
    assert client.post("/api/control/power", json={"target": t, "on": True}).json()["ok"]
    assert client.post("/api/control/brightness", json={"target": t, "value": 40}).json()["ok"]
    assert client.post("/api/control/color", json={"target": t, "color": "#00ff00"}).json()["ok"]
    assert ("color", "Demo Lamp", (0, 255, 0)) in backend.calls
    lamp = next(d for d in client.get("/api/devices").json() if d["name"] == "Demo Lamp")
    assert lamp["id"] == "DEMO0003" and lamp["routes"] == ["demo"]
    assert lamp["on"] is True and lamp["brightness"] == 40 and lamp["color"] == "#00FF00"


def test_offline_device_reports_error(env):
    client, *_ = env
    r = client.post("/api/control/power", json={"target": "device:Demo Offline Light", "on": True}).json()
    assert not r["ok"] and "offline" in r["errors"]["Demo Offline Light"]


def test_unknown_target_is_404(env):
    client, *_ = env
    assert client.post("/api/control/power", json={"target": "device:Nope", "on": True}).status_code == 404
    assert client.post("/api/control/power", json={"target": "Nope", "on": True}).status_code == 404


def test_effect_runs_sends_segments_and_stops(env):
    client, backend, *_ = env
    t = "device:Demo Porch Strip"
    r = client.post("/api/control/effect", json={"target": t, "pattern": "chase",
                                                 "params": {"colors": "#FF0000,#FFFFFF", "speed": 10}, "fps": 10})
    assert r.status_code == 200
    assert wait_for(lambda: sum(1 for c in backend.calls if c[0] == "segments") >= 3)
    seg_call = next(c for c in backend.calls if c[0] == "segments")
    assert len(seg_call[2]) == 20
    strip = next(d for d in client.get("/api/devices").json() if d["name"] == "Demo Porch Strip")
    assert strip["effect"]["pattern"] == "chase" and len(strip["frame"]) == 20

    client.post("/api/control/stop", json={"target": t})
    count = len(backend.calls)
    time.sleep(0.4)
    assert len(backend.calls) == count


def test_manual_color_stops_running_effect(env):
    client, backend, controller, *_ = env
    t = "device:Demo Porch Strip"
    client.post("/api/control/effect", json={"target": t, "pattern": "rainbow", "fps": 10})
    assert wait_for(lambda: any(c[0] == "segments" for c in backend.calls))
    client.post("/api/control/color", json={"target": t, "color": "#0000FF"})
    assert controller.state["DEMO0001"]["effect"] is None


def test_linked_group_spans_segments_across_members(env):
    client, backend, *_ = env
    client.post("/api/groups", json={"name": "Outside", "members": ["Demo Porch Strip", "Demo Spotlights"], "mode": "span"})
    client.post("/api/control/effect", json={"target": "group:Outside", "pattern": "solid", "params": {"color": "#112233"}})
    assert wait_for(lambda: {c[1] for c in backend.calls if c[0] == "segments"} >= {"Demo Porch Strip", "Demo Spotlights"})
    spot = next(c for c in backend.calls if c[0] == "segments" and c[1] == "Demo Spotlights")
    assert spot[2] == ("#112233",) * 4


def test_single_segment_lights_get_plain_color_from_effects(env):
    client, backend, *_ = env
    client.post("/api/control/effect", json={"target": "device:Demo Lamp", "pattern": "solid", "params": {"color": "#FF0000"}})
    assert wait_for(lambda: ("color", "Demo Lamp", (255, 0, 0)) in backend.calls)


def test_presets_and_preview(env):
    client, backend, *_ = env
    assert client.post("/api/presets", json={"name": "Cane", "pattern": "candy_cane", "params": {"speed": 3}}).status_code == 200
    assert "Cane" in client.get("/api/presets").json()
    assert client.post("/api/presets", json={"name": "Bad", "pattern": "nope"}).status_code == 400
    frames = client.post("/api/preview", json={"pattern": "rainbow", "segments": 10, "seconds": 1, "fps": 5}).json()
    assert len(frames) == 5 and len(frames[0]) == 10
    r = client.post("/api/control/effect", json={"target": "device:Demo Porch Strip", "preset": "Cane"})
    assert r.status_code == 200


def test_schedule_crud_and_firing(env):
    client, backend, controller, store, sched = env
    body = {"name": "Lamp on", "days": [], "when": {"type": "time", "time": "18:30"},
            "target": "device:Demo Lamp", "action": {"type": "on"}}
    saved = client.post("/api/schedules", json=body).json()
    listed = client.get("/api/schedules").json()
    assert listed[0]["id"] == saved["id"] and listed[0]["next_run"]

    sched.tick(datetime(2026, 10, 26, 18, 32).astimezone())
    assert ("power", "Demo Lamp", True) in backend.calls
    assert sched.log[-1]["result"] == "OK"
    # Same day again: no repeat.
    n = len(backend.calls)
    sched.tick(datetime(2026, 10, 26, 18, 35).astimezone())
    assert len(backend.calls) == n

    assert client.post(f"/api/schedules/{saved['id']}/run").json()["ok"]
    client.delete(f"/api/schedules/{saved['id']}")
    assert client.get("/api/schedules").json() == []


def test_settings_validate_location(env):
    client, *_ = env
    assert client.post("/api/settings", json={"latitude": "abc"}).status_code == 400
    s = client.post("/api/settings", json={"latitude": "40.71", "longitude": "-74.01", "default_fps": 5}).json()
    assert s["latitude"] == 40.71 and s["default_fps"] == 5


def test_store_survives_restart(tmp_path):
    path = str(tmp_path / "settings.json")
    s1 = Store(path)
    s1.put_group("G", ["A", "B"], "mirror")
    s1.update_settings({"latitude": 1.5})
    s2 = Store(path)
    assert s2.get("groups")["G"]["mode"] == "mirror"
    assert s2.get("settings")["latitude"] == 1.5 and s2.get("settings")["default_fps"] == 4


def test_scenes_rename_and_partial_settings(env):
    client, backend, *_ = env
    scenes = client.get("/api/devices/DEMO0001/scenes").json()
    assert {"name": "Aurora", "kind": "Govee scene"} in scenes
    r = client.post("/api/control/scene", json={"target": "device:DEMO0001", "scene": "aurora"}).json()
    assert r["ok"] and ("scene", "Demo Porch Strip", "Aurora") in backend.calls
    r = client.post("/api/control/scene", json={"target": "device:DEMO0001", "scene": "Nope"}).json()
    assert not r["ok"]

    devices = client.post("/api/devices/DEMO0003/rename", json={"name": "Desk lamp"}).json()
    assert any(d["id"] == "DEMO0003" and d["name"] == "Desk lamp" for d in devices)
    assert client.post("/api/control/power", json={"target": "device:Desk lamp", "on": True}).json()["ok"]

    client.post("/api/settings", json={"latitude": 40.71, "longitude": -74.01})
    s = client.post("/api/settings", json={"use_govee_desktop": False}).json()
    assert s["latitude"] == 40.71 and s["use_govee_desktop"] is False


def test_state_poll_reports_light_state(env):
    client, backend, controller, *_ = env
    client.post("/api/control/color", json={"target": "device:Demo Lamp", "color": "#123456"})
    controller.state["DEMO0003"].update(color=None, reported=False)
    controller.poll_state()
    lamp = next(d for d in client.get("/api/devices").json() if d["id"] == "DEMO0003")
    assert lamp["reported"] and lamp["color"] == "#123456"


def test_secrets_file_on_non_windows(tmp_path, monkeypatch):
    import os
    import sys
    from home_control import secrets
    if sys.platform == "win32":
        pytest.skip("Windows uses Credential Manager")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOVEE_API_KEY", raising=False)
    assert secrets.get_secret("govee-api-key")[0] is None
    where = secrets.save_secret_value("govee-api-key", "abc123")
    assert oct(os.stat(where).st_mode & 0o777) == "0o600"
    assert secrets.get_secret("govee-api-key") == ("abc123", where)
    monkeypatch.setenv("GOVEE_API_KEY", "from-env")
    assert secrets.get_secret("govee-api-key")[0] == "from-env"
