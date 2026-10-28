"""Page PIN, remembered devices, lockout, health endpoint, and the secrets file fallback."""

import json
import os
import sys
import traceback

import pytest
from fastapi import Body
from fastapi.testclient import TestClient

from home_control import secrets
from home_control.auth import (COOKIE_NAME, LOCKOUT_SECONDS, REMEMBER_SECONDS, PinError, PinGate,
                               hash_pin, load_or_create_session_key, pin_matches)
from home_control.controller import Controller
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.scheduler import Scheduler
from home_control.server import confirm_with_pin, create_app
from home_control.store import Store

PIN = "482916"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def app_env(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    controller.connect()
    clock = Clock()
    gate = PinGate(hash_pin(PIN), b"s" * 32, required=True, clock=clock)
    app = create_app(controller, store, Scheduler(controller, store), gate, started_at=clock.t - 100)
    yield app, TestClient(app), gate, clock, controller, store
    controller.stop_all()


# ---------- hashing ----------

def test_pin_is_stored_only_as_salted_hash():
    h1, h2 = hash_pin(PIN), hash_pin(PIN)
    assert PIN not in h1 and h1 != h2  # salted
    assert h1.startswith("scrypt$")
    assert pin_matches(PIN, h1) and not pin_matches("482917", h1)


@pytest.mark.parametrize("bad", ["12345", "1234567", "12a456", "", "123 45"])
def test_pin_must_be_exactly_six_digits(bad):
    with pytest.raises(ValueError):
        hash_pin(bad)


# ---------- page access ----------

def test_api_locked_until_pin_entered_and_device_remembered(app_env):
    app, client, gate, clock, *_ = app_env
    r = client.get("/api/devices")
    assert r.status_code == 401 and r.json()["pin_required"]
    assert client.get("/").status_code == 200  # the page itself loads, showing the PIN screen
    assert client.get("/api/auth/status").json() == {"required": True, "pin_set": True,
                                                      "signed_in": False, "locked_for": 0}

    r = client.post("/api/auth/login", json={"pin": PIN})
    assert r.status_code == 200
    cookie = r.cookies.get(COOKIE_NAME)
    assert cookie and PIN not in cookie
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and f"max-age={REMEMBER_SECONDS}" in set_cookie
    assert client.get("/api/devices").status_code == 200

    clock.t += REMEMBER_SECONDS - 60
    assert client.get("/api/devices").status_code == 200
    clock.t += 120  # 30 days are up
    assert client.get("/api/devices").status_code == 401


def test_forged_or_tampered_cookie_is_refused(app_env):
    app, client, gate, *_ = app_env
    good = gate.issue_cookie()
    body, sig = good.rsplit(".", 1)
    for bad in ("nonsense", f"{body}.AAAA", f"{body[:-2]}xx.{sig}", ""):
        client.cookies.set(COOKIE_NAME, bad)
        assert client.get("/api/devices").status_code == 401
    other_key_gate = PinGate(gate.pin_hash, b"x" * 32)
    client.cookies.set(COOKIE_NAME, other_key_gate.issue_cookie())
    assert client.get("/api/devices").status_code == 401


def test_new_pin_signs_every_device_out(app_env):
    app, client, gate, *_ = app_env
    client.post("/api/auth/login", json={"pin": PIN})
    assert client.get("/api/devices").status_code == 200
    gate.pin_hash = hash_pin("111222")
    assert client.get("/api/devices").status_code == 401


def test_logout_forgets_device(app_env):
    app, client, *_ = app_env
    client.post("/api/auth/login", json={"pin": PIN})
    client.post("/api/auth/logout")
    assert client.get("/api/devices").status_code == 401


def test_five_wrong_pins_lock_for_fifteen_minutes(app_env):
    app, client, gate, clock, *_ = app_env
    for i in range(4):
        r = client.post("/api/auth/login", json={"pin": "000000"})
        assert r.status_code == 401 and f"{4 - i} " in r.json()["detail"]
    r = client.post("/api/auth/login", json={"pin": "000000"})
    assert r.status_code == 429 and "15 min" in r.json()["detail"]
    assert client.post("/api/auth/login", json={"pin": PIN}).status_code == 429  # even the right PIN
    assert client.get("/api/auth/status").json()["locked_for"] == LOCKOUT_SECONDS
    clock.t += LOCKOUT_SECONDS + 1
    assert client.post("/api/auth/login", json={"pin": PIN}).status_code == 200


def test_correct_pin_resets_the_failure_count(app_env):
    app, client, gate, *_ = app_env
    for _ in range(4):
        client.post("/api/auth/login", json={"pin": "000000"})
    assert client.post("/api/auth/login", json={"pin": PIN}).status_code == 200
    for _ in range(4):
        assert client.post("/api/auth/login", json={"pin": "000000"}).status_code == 401


def test_no_pin_set_keeps_page_locked(tmp_path):
    gate = PinGate(None, b"s" * 32, required=True)
    assert not gate.cookie_ok(gate.issue_cookie())
    with pytest.raises(PinError, match="--set-pin"):
        gate.check_pin("123456")


def test_demo_without_pin_is_open_but_demo_with_pin_is_not():
    assert PinGate(None, b"s" * 32, required=False).cookie_ok(None)
    assert not PinGate(hash_pin(PIN), b"s" * 32, required=False).cookie_ok(None)


def test_sensitive_actions_need_the_pin_every_time(app_env):
    app, client, gate, *_ = app_env

    @app.post("/api/test-siren")  # stands in for the phase 2 arm/disarm/siren routes
    def siren(pin: str = Body(..., embed=True)):
        confirm_with_pin(gate, pin)
        return {"ok": True}

    client.post("/api/auth/login", json={"pin": PIN})  # remembered device...
    assert client.post("/api/test-siren", json={"pin": ""}).status_code == 403  # ...still asked
    assert client.post("/api/test-siren", json={"pin": "999999"}).status_code == 403
    assert client.post("/api/test-siren", json={"pin": PIN}).json() == {"ok": True}


def test_page_cannot_change_server_only_settings(app_env):
    app, client, gate, clock, controller, store = app_env
    client.post("/api/auth/login", json={"pin": PIN})
    s = client.post("/api/settings", json={"govee_dll": "C:/evil.dll", "secrets_store": "credential-manager",
                                           "guid_credential": "x", "default_fps": 6}).json()
    assert s["govee_dll"] is None and s["secrets_store"] == "file"
    assert s["guid_credential"] is None and s["default_fps"] == 6


# ---------- health ----------

def test_health_is_open_and_reveals_nothing_private(app_env):
    app, client, gate, clock, controller, store = app_env
    controller.last_activity = None
    h = client.get("/api/health").json()
    assert h["app"] == "home-control" and h["version"]
    assert h["uptime_seconds"] >= 100
    assert h["connections"] == {"demo": {"label": "Demo (no real lights)", "up": True}}
    assert h["light_count"] == 4 and h["effects_running"] == 0
    assert h["last_event_at"] is None
    assert set(h) == {"app", "version", "started_at", "uptime_seconds", "connections", "light_count",
                      "effects_running", "last_event_at"}
    text = json.dumps(h)
    for private in ("Demo Porch Strip", "Demo Lamp", "DEMO0001", PIN, gate.pin_hash):
        assert private not in text


def test_health_last_event_follows_activity(app_env):
    app, client, gate, clock, controller, store = app_env
    client.post("/api/auth/login", json={"pin": PIN})
    client.post("/api/control/power", json={"target": "device:Demo Lamp", "on": True})
    assert client.get("/api/health").json()["last_event_at"] is not None
    client.post("/api/control/effect", json={"target": "device:Demo Porch Strip", "pattern": "rainbow"})
    assert client.get("/api/health").json()["effects_running"] == 1


# ---------- cookie key ----------

def test_session_key_created_once_and_reused():
    vault = {}

    def get(name, settings):
        return vault.get(name), "test"

    def save(name, value, settings):
        vault[name] = value

    k1, saved1 = load_or_create_session_key(get, save, {})
    k2, saved2 = load_or_create_session_key(get, save, {})
    assert saved1 and saved2 and k1 == k2 and len(k1) == 32


def test_session_key_falls_back_to_temporary_when_store_is_unwritable():
    def get(name, settings):
        return None, "missing"

    def save(name, value, settings):
        raise OSError("store unavailable")

    key, saved = load_or_create_session_key(get, save, {})
    assert len(key) == 32 and not saved


# ---------- secrets store ----------

def test_data_folder_file_is_the_default_everywhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert secrets.secrets_store({}) == "file"
    assert secrets.secrets_store({"secrets_store": "file"}) == "file"
    assert secrets.secrets_store({"secrets_store": "credential-manager"}) == "credential-manager"
    assert secrets.secrets_store({"secrets_store": "anything-else"}) == "file"
    monkeypatch.setattr(sys, "platform", "linux")
    assert secrets.secrets_store({"secrets_store": "credential-manager"}) == "file"  # Windows only


def test_secrets_file_follows_the_data_folder(tmp_path):
    secrets.set_data_dir(str(tmp_path / "data"))
    assert secrets.secrets_file() == os.path.join(str(tmp_path), "data", "secrets.json")


def test_icacls_leaves_only_this_account():
    sid = "S-1-5-21-1-2-3-1001"
    assert secrets.icacls_commands(r"C:\x\s.json", False, sid) == [
        ["icacls", r"C:\x\s.json", "/inheritance:r", "/grant:r", f"*{sid}:F"]]


def test_windows_file_store_round_trip_and_lockdown(monkeypatch, tmp_path):
    """Simulates Windows: the data-folder file is the default, and every write locks the file down."""
    monkeypatch.setattr(sys, "platform", "win32")
    secrets.set_data_dir(str(tmp_path / "data"))
    calls = []
    monkeypatch.setattr(secrets, "_current_user_sid", lambda: "S-1-5-21-9")
    monkeypatch.setattr(secrets.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(secrets, "read_credential",
                        lambda target: {"home-control/govee-api-key": "from-credman"}.get(target))

    assert secrets.get_secret("govee-api-key", {})[0] is None  # default file is empty; Credential Manager not read
    assert secrets.get_secret("govee-api-key", {"secrets_store": "credential-manager"})[0] == "from-credman"

    assert secrets.copy_secrets_to_file({}) == ["govee-api-key"]
    value, where = secrets.get_secret("govee-api-key", {})
    assert value == "from-credman" and where == secrets.secrets_file()

    secrets.save_secret_value("session-key", "abcd", {})
    assert secrets.get_secret("session-key", {})[0] == "abcd"
    locked = {c[0][1] for c in calls}
    assert locked == {secrets.secrets_file()}  # only the file; the rest of data/ is left alone
    assert all(kw.get("creationflags", 0) == secrets.NO_WINDOW for _, kw in calls)


def test_lockdown_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    secrets.set_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(secrets, "_current_user_sid", lambda: None)
    with pytest.raises(OSError, match="SID"):
        secrets.save_secret_value("session-key", "abcd", {})


# ---------- auto-start helpers ----------

def test_keep_running_restarts_after_crash_and_stops_on_ctrl_c(monkeypatch, tmp_path):
    from home_control import __main__ as cli
    runs = []

    def fake_serve(args, store):
        runs.append(1)
        if len(runs) == 1:
            raise SystemExit(1)  # e.g. port busy
        if len(runs) == 2:
            return 0             # stopped cleanly but unexpectedly
        raise KeyboardInterrupt

    sleeps = []
    monkeypatch.setattr(cli, "serve", fake_serve)
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(sys, "argv", ["home_control", "--keep-running", "--data", str(tmp_path),
                                      "--log-file", str(tmp_path / "logs" / "hc.log")])
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert cli.main() == 0
    finally:
        sys.stdout.close()
        sys.stdout, sys.stderr = stdout, stderr
    assert len(runs) == 3 and sleeps == [cli.RESTART_DELAY, cli.RESTART_DELAY]
    log = (tmp_path / "logs" / "hc.log").read_text()
    assert "crashed" in log and "stopped; starting again" in log


def test_log_rotation_keeps_three(tmp_path):
    from home_control import __main__ as cli
    path = tmp_path / "hc.log"
    stdout, stderr = sys.stdout, sys.stderr
    try:
        for i in range(5):
            path.write_text("x" * (cli.LOG_MAX_BYTES + 1))
            cli.open_log(str(path))
            sys.stdout.close()
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["hc.log", "hc.log.1", "hc.log.2", "hc.log.3"]


def test_log_file_never_holds_an_address(tmp_path):
    from home_control import __main__ as cli
    path = tmp_path / "hc.log"
    stdout, stderr = sys.stdout, sys.stderr
    try:
        cli.open_log(str(path))
        print("Home Control is running at http://100.101.102.103:8780/")
        try:
            raise OSError("error while attempting to bind on address ('100.101.102.103', 8780)")
        except OSError:
            traceback.print_exc()
        sys.stdout.close()
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    log = path.read_text()
    assert "100.101.102.103" not in log
    assert log.count("<address>") >= 2 and "Traceback" in log


def test_default_port_never_collides_with_the_trading_desk(monkeypatch, tmp_path):
    from home_control import __main__ as cli
    seen = {}
    monkeypatch.setattr(cli, "serve", lambda args, store: seen.setdefault("port", args.port) and 0)
    monkeypatch.setattr(sys, "argv", ["home_control", "--data", str(tmp_path)])
    cli.main()
    assert seen["port"] == 8780
