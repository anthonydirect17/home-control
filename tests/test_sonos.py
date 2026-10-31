"""Sonos: found, remembered, polled, controlled; a stand-in replaces SoCo and the speaker."""

import types

import pytest

from home_control.services import ServiceError
from home_control.sonos import SonosService
from home_control.store import Store


class Fav:
    def __init__(self, title, uri=None):
        self.title, self._uri, self.resource_meta_data, self.reference = title, uri, "<meta/>", f"ref:{title}"

    def get_uri(self):
        if self._uri is None:
            raise ValueError("no uri")
        return self._uri


class FakeBar:
    """Plays a soundbar: the attributes and calls SoCo offers."""

    def __init__(self, uid="RINCON_1", ip="10.9.9.9", name="Soundbar"):
        self.uid, self.ip_address, self.player_name = uid, ip, name
        self.volume, self.mute = 20, False
        self.is_playing_tv, self.is_playing_line_in, self.is_playing_radio = False, False, False
        self.night_mode, self.dialog_mode = False, True
        self.transport = "PAUSED_PLAYBACK"
        self.calls = []
        self.alive = True
        self.group = types.SimpleNamespace(coordinator=self, members=[self])
        self.music_library = types.SimpleNamespace(
            get_sonos_favorites=lambda: [Fav("Jazz Radio", "x-rincon-mp3radio://jazz"), Fav("Road Trip")])

    def _check(self):
        if not self.alive:
            raise ConnectionError("no route")

    def get_current_transport_info(self):
        self._check()
        return {"current_transport_state": self.transport}

    def get_current_track_info(self):
        self._check()
        return {"title": "Song", "artist": "Band", "album": "", "position": "0:01:00", "duration": "0:03:00"}

    def __getattr__(self, name):  # play, pause, next, previous, switch_to_tv, play_uri, ...
        def call(*a):
            self._check()
            self.calls.append((name, a))
            if name == "play":
                self.transport = "PLAYING"
            if name == "pause":
                self.transport = "PAUSED_PLAYBACK"
            if name == "switch_to_tv":
                self.is_playing_tv = True
            if name == "play_uri" and a[0] is None:
                raise RuntimeError("bad uri")
        return call


def fake_soco(*bars):
    by_ip = {b.ip_address: b for b in bars}
    return types.SimpleNamespace(
        discover=lambda timeout=5: {b for b in bars if b.alive} or None,
        SoCo=lambda ip: by_ip[ip] if ip in by_ip and by_ip[ip].alive else (_ for _ in ()).throw(OSError("down")))


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "settings.json"))


def make(store, *bars):
    return SonosService(store, soco_module=fake_soco(*bars), discover_timeout=0, log=lambda m: None)


def test_finds_remembers_and_reads_the_soundbar(store):
    bar = FakeBar()
    svc = make(store, bar)
    assert svc.poll_once()
    sp = svc.state()["speakers"][0]
    assert sp["name"] == "Soundbar" and sp["state"] == "paused" and sp["volume"] == 20
    assert sp["source"] == "music" and sp["night_mode"] is False and sp["dialog_mode"] is True
    assert sp["favorites"] == ["Jazz Radio", "Road Trip"]
    assert store.get("settings")["sonos"] == [{"uid": "RINCON_1", "name": "Soundbar", "ip": "10.9.9.9"}]
    assert svc.health_text() == "paused"


def test_remembered_address_is_used_before_searching(store):
    bar = FakeBar()
    store.update_settings({"sonos": [{"uid": "RINCON_1", "name": "Soundbar", "ip": "10.9.9.9"}]})
    soco = fake_soco(bar)
    soco.discover = lambda timeout=5: pytest.fail("should not search when the remembered address works")
    svc = SonosService(store, soco_module=soco, log=lambda m: None)
    assert svc.poll_once()


def test_a_new_address_is_found_by_searching(store):
    old = FakeBar(ip="10.9.9.9")
    svc = make(store, old)
    svc.poll_once()
    old.alive = False
    moved = FakeBar(ip="10.9.9.20")
    svc._soco = fake_soco(moved)
    assert svc.poll_once()
    assert store.get("settings")["sonos"][0]["ip"] == "10.9.9.20"


def test_not_answering_marks_it_down_with_a_plain_message(store):
    bar = FakeBar()
    svc = make(store, bar)
    svc.poll_once()
    bar.alive = False
    assert not svc.poll_once()
    assert svc.status["error"] == "the Sonos speaker isn't answering"
    assert "10.9.9.9" not in svc.status["error"]


def test_nothing_on_the_network(store):
    svc = SonosService(store, soco_module=types.SimpleNamespace(discover=lambda timeout=5: None, SoCo=None),
                       log=lambda m: None)
    assert not svc.poll_once()
    assert "no Sonos speaker found" in svc.status["error"]


def test_controls(store):
    bar = FakeBar()
    svc = make(store, bar)
    svc.poll_once()
    svc.act(None, "play")
    assert svc.state()["speakers"][0]["state"] == "playing"
    svc.act(None, "toggle")
    assert svc.state()["speakers"][0]["state"] == "paused"
    svc.act("Soundbar", "next")
    svc.act("RINCON_1", "previous")
    svc.act(None, "volume", {"level": 150})
    assert bar.volume == 100
    store.update_settings({"sonos_max_volume": 40})
    svc.act(None, "volume", {"level": 75})
    assert bar.volume == 40
    svc.act(None, "mute", {"on": True})
    assert bar.mute is True
    svc.act(None, "source", {"name": "tv"})
    assert svc.state()["speakers"][0]["source"] == "tv"
    svc.act(None, "night_mode", {"on": True})
    assert bar.night_mode is True
    assert [c[0] for c in bar.calls][:4] == ["play", "pause", "next", "previous"]


def test_favorites_play_by_uri_or_through_the_queue(store):
    bar = FakeBar()
    svc = make(store, bar)
    svc.poll_once()
    svc.act(None, "favorite", {"title": "Jazz Radio"})
    assert ("play_uri", ("x-rincon-mp3radio://jazz", "<meta/>")) in bar.calls
    bar.calls.clear()
    svc.act(None, "favorite", {"title": "Road Trip"})  # no direct uri: goes through the queue
    assert [c[0] for c in bar.calls] == ["clear_queue", "add_to_queue", "play_from_queue"]
    with pytest.raises(KeyError, match="no Sonos favorite"):
        svc.act(None, "favorite", {"title": "Nope"})


def test_bad_requests(store):
    bar = FakeBar()
    svc = make(store, bar)
    with pytest.raises(ServiceError, match="no Sonos speaker found yet"):
        svc.act(None, "play")
    svc.poll_once()
    with pytest.raises(ValueError, match="unknown Sonos action"):
        svc.act(None, "explode")
    with pytest.raises(ValueError, match="source must be"):
        svc.act(None, "source", {"name": "hdmi2"})
    with pytest.raises(KeyError):
        svc.act("Kitchen", "play")
    bar.alive = False
    with pytest.raises(ServiceError, match="didn't take that"):
        svc.act(None, "play")


def test_a_speaker_without_night_mode(store):
    bar = FakeBar()
    bar.night_mode = None
    svc = make(store, bar)
    svc.poll_once()
    with pytest.raises(ServiceError, match="no night mode"):
        svc.act(None, "night_mode", {"on": True})


def test_routes_need_the_pin_and_act(tmp_path):
    from fastapi.testclient import TestClient

    from home_control.auth import PinGate, hash_pin
    from home_control.controller import Controller
    from home_control.govee.demo import DemoBackend
    from home_control.hub import Hub
    from home_control.scheduler import Scheduler
    from home_control.server import create_app
    store = Store(str(tmp_path / "s.json"))
    controller = Controller(Hub([DemoBackend()], store), store)
    controller.connect()
    bar = FakeBar()
    svc = make(store, bar)
    svc.poll_once()
    gate = PinGate(hash_pin("482916"), b"k" * 32, required=True)
    client = TestClient(create_app(controller, store, Scheduler(controller, store), gate, services={"sonos": svc}))
    assert client.get("/api/services").status_code == 401
    client.post("/api/auth/login", json={"pin": "482916"})
    body = client.get("/api/services").json()
    assert body["sonos"]["status"]["up"] is True and body["sonos"]["state"]["speakers"][0]["name"] == "Soundbar"
    r = client.post("/api/services/sonos/act", json={"action": "volume", "args": {"level": 33}})
    assert r.status_code == 200 and bar.volume == 33
    assert client.post("/api/services/sonos/act", json={"action": "explode"}).status_code == 400
    assert client.post("/api/services/nope/act", json={"action": "play"}).status_code == 404
    h = client.get("/api/health").json()
    assert {"label": "Sonos", "value": "paused", "status": "ok"} in h["values"]
    assert "Soundbar" not in str(h["values"]) and "10.9.9.9" not in str(h)
