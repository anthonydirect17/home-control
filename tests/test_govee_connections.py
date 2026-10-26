"""The LAN and cloud connectors, tested against a pretend light and a pretend Govee server."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from home_control.controller import Controller
from home_control.govee.base import BackendError, Found
from home_control.govee.cloud import CloudBackend
from home_control.govee.lan import LanBackend
from home_control.hub import Hub, merge
from home_control.store import Store


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------- merging ----------

def test_merge_joins_cloud_lan_and_desktop_views_of_one_light():
    found = [
        Found("desktop", "Porch", name="Porch", model="H705A", segments=15),
        Found("lan", "192.168.1.40", id="AABB", model="H705A"),
        Found("cloud", {"sku": "H705A"}, id="AABB", name="Porch", model="H705A", segments=10),
        Found("lan", "192.168.1.41", id="CCDD", model="H6008"),
    ]
    devices = merge(found, aliases={"CCDD": "Hall bulb"})
    assert set(devices) == {"AABB", "CCDD"}
    porch = devices["AABB"]
    assert porch.routes == {"cloud": {"sku": "H705A"}, "lan": "192.168.1.40", "desktop": "Porch"}
    assert porch.segments == 15  # Govee Desktop's count wins: it's what segment commands use
    assert devices["CCDD"].name == "Hall bulb"


def test_unnamed_lan_light_gets_a_readable_name():
    devices = merge([Found("lan", "10.0.0.5", id="00112233AABB", model="H6199")])
    assert devices["00112233AABB"].name == "H6199 (AABB)"


# ---------- LAN ----------

class FakeLanLight:
    """Answers scan and devStatus the way a Govee light does, and records commands."""

    def __init__(self, reply_port):
        self.reply_port = reply_port
        self.scan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.scan.bind(("127.0.0.1", 0))
        self.ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl.bind(("127.0.0.1", 0))
        self.commands = []
        self.state = {"onOff": 0, "brightness": 50, "color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 0}
        for sock, fn in ((self.scan, self._on_scan), (self.ctrl, self._on_ctrl)):
            sock.settimeout(0.2)
            threading.Thread(target=self._loop, args=(sock, fn), daemon=True).start()
        self.stopped = False

    def _loop(self, sock, fn):
        while not getattr(self, "stopped", False):
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            fn(json.loads(data)["msg"])

    def _reply(self, cmd, data):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), ("127.0.0.1", self.reply_port))
        out.close()

    def _on_scan(self, msg):
        if msg["cmd"] == "scan":
            self._reply("scan", {"ip": "127.0.0.1", "device": "AA:BB:CC:DD:EE:FF:00:11", "sku": "H6199"})

    def _on_ctrl(self, msg):
        self.commands.append((msg["cmd"], msg["data"]))
        if msg["cmd"] == "turn":
            self.state["onOff"] = msg["data"]["value"]
        elif msg["cmd"] == "brightness":
            self.state["brightness"] = msg["data"]["value"]
        elif msg["cmd"] == "colorwc":
            self.state["color"] = msg["data"]["color"]
        elif msg["cmd"] == "devStatus":
            self._reply("devStatus", self.state)

    def close(self):
        self.stopped = True


@pytest.fixture
def lan(tmp_path):
    listen = free_udp_port()
    light = FakeLanLight(listen)
    store = Store(str(tmp_path / "settings.json"))
    backend = LanBackend(store, listen_port=listen, control_port=light.ctrl.getsockname()[1],
                         scan_target=("127.0.0.1", light.scan.getsockname()[1]), bind_host="127.0.0.1",
                         scan_seconds=0.4, multicast=False)
    yield backend, light, store
    backend.close()
    light.close()


def test_lan_discovers_controls_and_reads_state(lan):
    backend, light, store = lan
    backend.connect()
    found = backend.discover()
    assert [(f.id, f.address, f.model) for f in found] == [("AABBCCDDEEFF0011", "127.0.0.1", "H6199")]
    assert store.get("lan_cache")["AABBCCDDEEFF0011"]["ip"] == "127.0.0.1"

    backend.power("127.0.0.1", True)
    backend.brightness("127.0.0.1", 150)  # clamped to 100
    backend.color("127.0.0.1", (255, 10, 0))
    st = backend.state("127.0.0.1")
    assert st == {"on": True, "brightness": 100, "color": "#FF0A00"}
    assert ("turn", {"value": 1}) in light.commands
    assert ("colorwc", {"color": {"r": 255, "g": 10, "b": 0}, "colorTemInKelvin": 0}) in light.commands


def test_lan_remembers_lights_when_scanning_is_blocked(lan):
    backend, light, store = lan
    store.put("lan_cache", {"1234": {"ip": "10.1.1.9", "sku": "H6008"}})
    backend.listening = False  # as if another program held the port
    found = backend.discover()
    assert [(f.id, f.address) for f in found] == [("1234", "10.1.1.9")]
    assert backend.state("10.1.1.9") is None


def test_effect_over_lan_sends_whole_light_color(lan, tmp_path):
    backend, light, store = lan
    hub = Hub([backend], store)
    controller = Controller(hub, store)
    controller.connect()
    errors = controller.start_effect("device:AABBCCDDEEFF0011", "solid", {"color": "#00FF00"})
    assert errors == {}
    import time
    deadline = time.time() + 2
    while time.time() < deadline and ("colorwc", {"color": {"r": 0, "g": 255, "b": 0}, "colorTemInKelvin": 0}) not in light.commands:
        time.sleep(0.05)
    controller.stop_all()
    assert ("colorwc", {"color": {"r": 0, "g": 255, "b": 0}, "colorTemInKelvin": 0}) in light.commands


# ---------- cloud ----------

DEVICES = {
    "code": 200, "message": "success",
    "data": [{
        "sku": "H6008", "device": "11:22:33:44:55:66:77:88", "deviceName": "Bedroom", "type": "devices.types.light",
        "capabilities": [
            {"type": "devices.capabilities.on_off", "instance": "powerSwitch"},
            {"type": "devices.capabilities.range", "instance": "brightness"},
            {"type": "devices.capabilities.color_setting", "instance": "colorRgb"},
            {"type": "devices.capabilities.segment_color_setting", "instance": "segmentedColorRgb",
             "parameters": {"fields": [{"fieldName": "segment", "elementRange": {"min": 0, "max": 9}}]}},
        ],
    }],
}


class FakeGovee(BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Govee-API-Key") != "good-key":
            return self._send({"code": 401, "message": "bad key"}, 401)
        FakeGovee.log.append(("GET", self.path, None))
        self._send(DEVICES)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGovee.log.append(("POST", self.path, body))
        if self.path.endswith("/device/control"):
            return self._send({"code": 200, "msg": "success"})
        if self.path.endswith("/device/scenes"):
            return self._send({"code": 200, "payload": {"capabilities": [{
                "type": "devices.capabilities.dynamic_scene", "instance": "lightScene",
                "parameters": {"options": [{"name": "Sunrise", "value": {"paramId": 1, "id": 2}}]}}]}})
        if self.path.endswith("/device/diy-scenes"):
            return self._send({"code": 200, "payload": {"capabilities": [{
                "type": "devices.capabilities.diy_color_setting", "instance": "diyScene",
                "parameters": {"options": [{"name": "Candy", "value": 77}]}}]}})
        if self.path.endswith("/device/state"):
            return self._send({"code": 200, "payload": {"capabilities": [
                {"instance": "powerSwitch", "state": {"value": 1}},
                {"instance": "brightness", "state": {"value": 42}},
                {"instance": "colorRgb", "state": {"value": 0xFF8800}}]}})
        self._send({"code": 404}, 404)


@pytest.fixture
def cloud_url():
    FakeGovee.log = []
    server = HTTPServer(("127.0.0.1", 0), FakeGovee)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_cloud_rejects_bad_key(cloud_url):
    with pytest.raises(BackendError, match="rejected the API key"):
        CloudBackend("wrong", base_url=cloud_url).connect()


def test_cloud_lists_controls_scenes_and_state(cloud_url):
    b = CloudBackend("good-key", base_url=cloud_url)
    b.connect()
    (f,) = b.discover()
    assert (f.id, f.name, f.model, f.segments) == ("1122334455667788", "Bedroom", "H6008", 10)

    b.color(f.address, (255, 136, 0))
    control = next(x[2] for x in FakeGovee.log if x[1].endswith("/device/control"))
    assert control["payload"]["device"] == "11:22:33:44:55:66:77:88"
    assert control["payload"]["capability"] == {"type": "devices.capabilities.color_setting",
                                                "instance": "colorRgb", "value": 0xFF8800}

    scenes = b.scenes(f.address)
    assert [(s["name"], s["kind"]) for s in scenes] == [("Sunrise", "Govee scene"), ("Candy", "Your DIY scene")]
    b.run_scene(f.address, scenes[1])
    last = FakeGovee.log[-1][2]["payload"]["capability"]
    assert last == {"type": "devices.capabilities.diy_color_setting", "instance": "diyScene", "value": 77}

    assert b.state(f.address) == {"on": True, "brightness": 42, "color": "#FF8800"}
    assert b.requests_today >= 6


def test_cloud_refuses_what_the_light_cannot_do(cloud_url):
    b = CloudBackend("good-key", base_url=cloud_url)
    (f,) = b.discover()
    f.address["instances"].remove("colorRgb")
    with pytest.raises(BackendError, match="doesn't support colorRgb"):
        b.color(f.address, (1, 2, 3))


def test_cloud_only_light_works_but_refuses_effects(cloud_url, tmp_path):
    store = Store(str(tmp_path / "settings.json"))
    controller = Controller(Hub([CloudBackend("good-key", base_url=cloud_url)], store), store)
    controller.connect()
    assert controller.power("device:Bedroom", True) == {}
    errors = controller.start_effect("device:Bedroom", "rainbow")
    assert "LAN or Govee Desktop" in errors["Bedroom"]
    assert controller.scene("device:Bedroom", "sunrise") == {}
    row = controller.snapshot()[0]
    assert row["can_scenes"] and not row["can_effects"] and row["routes"] == ["cloud"]


def test_hub_falls_back_to_cloud_when_lan_command_fails(cloud_url, tmp_path):
    store = Store(str(tmp_path / "settings.json"))

    class BrokenLan(LanBackend):
        def discover(self):
            return [Found("lan", "10.9.9.9", id="1122334455667788", model="H6008")]

        def power(self, address, on):
            raise BackendError("unreachable")

    hub = Hub([BrokenLan(store, listen_port=free_udp_port(), bind_host="127.0.0.1"),
               CloudBackend("good-key", base_url=cloud_url)], store)
    controller = Controller(hub, store)
    controller.connect()
    (dev,) = controller.devices.values()
    assert sorted(dev.routes) == ["cloud", "lan"]
    assert controller.power("device:Bedroom", True) == {}
    assert any(x[1].endswith("/device/control") for x in FakeGovee.log)
