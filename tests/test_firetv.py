"""Fire TV: key, status parsing, actions, pairing, finding a moved stick; a stand-in replaces the stick."""

import pytest

from home_control import firetv
from home_control.firetv import FireTvService, describe, find_sticks, pair, parse_status
from home_control.services import ServiceError
from home_control.store import Store

AWAKE = """  mWakefulness=Awake
Display Power: state=ON
  mCurrentFocus=Window{1a2b3c u0 com.netflix.ninja/com.netflix.ninja.MainActivity}
    state=PlaybackState {state=3, position=1000, buffered position=0, speed=1.0}"""
ASLEEP = "  mWakefulness=Asleep\nDisplay Power: state=OFF\n  mCurrentFocus=Window{9 u0 com.amazon.tv.launcher/x.Home}"


class FakeStick:
    def __init__(self, ip, serial="G070VM0001", name="Living Room", status=AWAKE):
        self.ip, self.serial, self.name, self.status = ip, serial, name, status
        self.commands, self.alive, self.connected, self.approved = [], True, False, True

    def connect(self, rsa_keys=None, auth_timeout_s=None):
        if not self.alive:
            raise ConnectionRefusedError("refused")
        if not self.approved:
            raise TimeoutError("not approved on the TV")
        assert rsa_keys and auth_timeout_s
        self.connected = True

    def shell(self, cmd, timeout_s=None):
        if not self.alive:
            raise ConnectionResetError("gone")
        self.commands.append(cmd)
        if cmd == firetv.STATUS_CMD:
            return self.status
        if cmd == "pm list packages":
            return "package:com.netflix.ninja\npackage:com.amazon.avod\npackage:com.example.other\n"
        if cmd == firetv.PAIR_PROPS:
            return f"{self.serial}\n{self.name}\nAFTKA\n8.1.4.3\n"
        if cmd.startswith("input keyevent 223"):
            self.status = ASLEEP
        return ""

    def close(self):
        self.connected = False


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "settings.json"))


def paired_store(store, ip="10.0.0.50", udn="uuid:abc"):
    store.update_settings({"firetv": [{"serial": "G070VM0001", "name": "Living Room", "model": "AFTKA",
                                       "ip": ip, "udn": udn}]})
    return store


def service(store, sticks, find=lambda: []):
    by_ip = {s.ip: s for s in sticks}
    return FireTvService(store, get_key=lambda: "PEM", make_device=lambda ip: by_ip[ip], find=find,
                         make_signer=lambda pem: "signer", log=lambda m: None)


def test_parse_status():
    assert parse_status(AWAKE) == {"power": "on", "app_id": "com.netflix.ninja", "media": "playing"}
    assert parse_status(ASLEEP) == {"power": "off", "app_id": "com.amazon.tv.launcher", "media": None}
    assert parse_status("") == {"power": "unknown", "app_id": None, "media": None}
    assert firetv.app_label("com.amazon.tv.launcher") == "Home screen"
    assert firetv.app_label("com.netflix.ninja") == "Netflix"


def test_reads_a_paired_stick(store):
    stick = FakeStick("10.0.0.50")
    svc = service(paired_store(store), [stick])
    assert svc.poll_once()
    st = svc.state()["sticks"][0]
    assert st["name"] == "Living Room" and st["power"] == "on" and st["app"] == "Netflix"
    assert st["media"] == "playing" and st["apps"] == ["Netflix", "Prime Video"]
    assert svc.health_text() == "1 of 1 on"


def test_actions_send_only_known_commands(store):
    stick = FakeStick("10.0.0.50")
    svc = service(paired_store(store), [stick])
    svc.poll_once()
    svc.act(None, "home")
    svc.act("Living Room", "play_pause")
    svc.act("G070VM0001", "launch", {"app": "Prime Video"})
    svc.act(None, "off")
    sent = [c for c in stick.commands if c not in (firetv.STATUS_CMD, "pm list packages")]
    assert sent == ["input keyevent 3", "input keyevent 85",
                    "monkey -p com.amazon.avod -c android.intent.category.LAUNCHER 1", "input keyevent 223"]
    assert svc.state()["sticks"][0]["power"] == "off"
    with pytest.raises(ValueError, match="isn't an app name"):
        svc.act(None, "launch", {"app": "x; reboot"})
    with pytest.raises(ValueError, match="unknown Fire TV action"):
        svc.act(None, "reboot")
    with pytest.raises(KeyError):
        svc.act("Bedroom", "home")


def test_a_moved_stick_is_found_again_by_its_dial_id(store):
    moved = FakeStick("10.0.0.77")
    gone = FakeStick("10.0.0.50")
    gone.alive = False
    svc = service(paired_store(store), [gone, moved],
                  find=lambda: [{"name": "Living Room", "model": "AFTKA", "udn": "uuid:abc", "ip": "10.0.0.77"}])
    assert svc.poll_once()
    assert store.get("settings")["firetv"][0]["ip"] == "10.0.0.77"


def test_not_answering_and_not_paired(store):
    stick = FakeStick("10.0.0.50")
    stick.alive = False
    svc = service(paired_store(store), [stick])
    assert not svc.poll_once()
    assert svc.status["error"] == "the Fire TV isn't answering"
    assert svc.state()["sticks"][0]["answering"] is False
    with pytest.raises(ServiceError, match="didn't answer"):
        svc.act(None, "home")
    empty = service(Store(str(store.path) + ".2"), [])
    assert empty.poll_once() and "--firetv-pair" in empty.status["note"]
    assert empty.health_text() == "none paired"


def test_missing_key_is_reported(store):
    svc = FireTvService(paired_store(store), get_key=lambda: None, make_device=None, log=lambda m: None)
    assert not svc.poll_once()
    assert "key is missing" in svc.status["error"]


def test_key_is_made_once_and_kept_in_the_secrets_store(fake_dpapi):
    from home_control import secrets
    pem = firetv.load_or_make_key(secrets.get_secret, secrets.save_secret_value, {})
    assert pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert firetv.load_or_make_key(secrets.get_secret, secrets.save_secret_value, {}) == pem
    with open(secrets.secrets_file(), encoding="utf-8") as f:
        assert "PRIVATE KEY" not in f.read()  # encrypted on disk
    pub = firetv.android_public_key(pem)
    assert pub.endswith(" home-control") and len(pub.split()[0]) == 700


def test_pairing_by_search(store, fake_dpapi):
    from home_control import secrets
    stick = FakeStick("10.0.0.50")
    answers = iter(["1"])
    said = []
    code = pair(store, secrets.get_secret, secrets.save_secret_value, {}, ask=lambda q: next(answers),
                say=said.append, find=lambda: [{"name": "Living Room", "model": "AFTKA", "udn": "uuid:abc",
                                                "ip": "10.0.0.50"}],
                make_device=lambda ip: stick, make_signer=lambda pem: "signer")
    assert code == 0
    assert store.get("settings")["firetv"] == [{"serial": "G070VM0001", "name": "Living Room", "model": "AFTKA",
                                                "fire_os": "8.1.4.3", "ip": "10.0.0.50", "udn": "uuid:abc"}]
    assert any("Always allow" in line for line in said)
    assert not any("10.0.0.50" in line for line in said)


def test_pairing_by_typed_address_and_failures(store, fake_dpapi):
    from home_control import secrets
    stick = FakeStick("10.0.0.60")
    stick.approved = False
    said = []
    answers = iter(["10.0.0.60"])
    code = pair(store, secrets.get_secret, secrets.save_secret_value, {}, ask=lambda q: next(answers),
                say=said.append, find=lambda: [], make_device=lambda ip: stick, make_signer=lambda pem: "s")
    assert code == 1 and any("Vega OS" in line for line in said)
    assert store.get("settings")["firetv"] == []
    answers = iter(["not-an-address"])
    assert pair(store, secrets.get_secret, secrets.save_secret_value, {}, ask=lambda q: next(answers),
                say=said.append, find=lambda: []) == 1


def test_describe_keeps_only_amazon_devices():
    amazon = (b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>Living Room</friendlyName>'
              b'<manufacturer>Amazon</manufacturer><modelName>AFTKA</modelName><UDN>uuid:abc</UDN></device></root>')
    other = b'<root><device><friendlyName>TV</friendlyName><manufacturer>Roku</manufacturer></device></root>'
    pages = {"http://10.0.0.50:60000/dd.xml": amazon, "http://10.0.0.9:8060/dial": other}
    found = find_sticks(locate=lambda: list(pages), fetch=pages.__getitem__)
    assert found == [{"name": "Living Room", "model": "AFTKA", "udn": "uuid:abc", "ip": "10.0.0.50"}]
    assert describe("http://x/broken", fetch=lambda url: b"<not xml") is None
