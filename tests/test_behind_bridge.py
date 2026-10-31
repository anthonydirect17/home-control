"""Home Control behind the Bridge: only requests the Bridge signed, the page under /home/, the start command."""

import os
import re
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from home_control.auth import PinGate, hash_pin
from home_control.controller import Controller
from home_control.fusionkit import link
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.scheduler import Scheduler
from home_control.server import create_app
from home_control.store import Store

PIN = "482916"
SECRET = b"b" * 32
STATIC = os.path.join(os.path.dirname(__file__), "..", "home_control", "static")


class SignedByBridge(httpx.Auth):
    """Signs each request the way the Bridge does: the raw path and query, as sent."""

    def __init__(self, secret=SECRET, route="tailscale"):
        self.secret, self.route = secret, route

    def auth_flow(self, request):
        request.headers.update(link.bridge_headers(self.secret, request.method, request.url.raw_path.decode("ascii"),
                                                   "/home", route=self.route))
        yield request


def signed(app, auth=None):
    c = TestClient(app)
    c.auth = auth or SignedByBridge()
    return c


@pytest.fixture
def home(tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    controller.connect()
    gate = PinGate(hash_pin(PIN), b"s" * 32, required=True)
    app = create_app(controller, store, Scheduler(controller, store), gate, bridge_secret=SECRET)
    yield app
    controller.stop_all()


def test_only_requests_the_bridge_signed(home):
    plain = TestClient(home)
    for path in ("/", "/static/app.js", "/api/devices", "/api/auth/status", "/api/stop/abc"):
        assert plain.get(path).status_code == 401, path
    assert plain.get("/api/health").status_code == 200        # the open health feed, for the Bridge and the Watcher
    assert signed(home, SignedByBridge(b"x" * 32)).get("/api/devices").status_code == 401
    assert signed(home, SignedByBridge(route="public")).get("/api/devices").status_code == 401


def test_signed_in_at_the_bridge_means_signed_in_here(home):
    c = signed(home)
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200
    assert c.get("/api/devices?x=a%20b").status_code == 200    # no Home Control cookie needed
    status = c.get("/api/auth/status").json()
    assert status["signed_in"] is True and status["via_bridge"] is True


def test_the_alarm_switch_still_needs_the_pin_every_time(home):
    c = signed(home)
    assert c.post("/api/mode/app-arming", json={"on": True, "pin": "000000"}).status_code in (401, 403)
    assert c.post("/api/mode/app-arming", json={"on": True, "pin": PIN}).status_code == 200


def test_the_page_uses_relative_urls():
    """So the same page works on its own at / and behind the Bridge at /home/."""
    html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    assert not re.search(r'(?:src|href|action)="/', html)
    assert "fetch(u(path)" in js and "fetch(\"/" not in js and "fetch('/" not in js
    assert not re.search(r'src: `/', js)


def test_the_start_command(tmp_path, monkeypatch, capsys):
    from home_control import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["home_control", "--data", str(tmp_path), "--host", "tailscale",
                                      "--bridge-link", str(tmp_path / "home.link")])
    assert cli.main() == 2 and "127.0.0.1" in capsys.readouterr().out
    assert cli.read_bridge_link(None) is None
    with pytest.raises(RuntimeError, match="missing"):
        cli.read_bridge_link(str(tmp_path / "home.link"))
    link.write_link(str(tmp_path / "home.link"), SECRET, "home", lock_down=lambda p: None)
    assert cli.read_bridge_link(str(tmp_path / "home.link")) == SECRET
    link.write_link(str(tmp_path / "studio.link"), SECRET, "studio", lock_down=lambda p: None)
    with pytest.raises(RuntimeError, match="something else"):
        cli.read_bridge_link(str(tmp_path / "studio.link"))
