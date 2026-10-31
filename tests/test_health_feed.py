"""The Fusion kit's health feed: the shape every app answers, and HOME's own answer."""

import pytest

from home_control import health_feed as hf
from home_control.controller import Controller
from home_control.govee.demo import DemoBackend
from home_control.hub import Hub
from home_control.server import home_feed
from home_control.services import PollingService
from home_control.store import Store


def good(**over):
    feed = hf.make_feed("watcher", "Watcher", "1.0", "ok", "All normal", [("CPU", "6%"), ("Memory", "9.1 GB", "ok")],
                        started_at=1000, now=1100)
    feed.update(over)
    return feed


def test_a_good_feed_passes_and_has_every_field():
    f = good()
    assert hf.check_feed(f) == []
    assert f["schema"] == "fusion-health/1" and f["uptime_seconds"] == 100
    assert f["values"] == [{"label": "CPU", "value": "6%"}, {"label": "Memory", "value": "9.1 GB", "status": "ok"}]


@pytest.mark.parametrize("change, problem", [
    ({"schema": "other/1"}, "schema"),
    ({"app": "Home Control"}, "lowercase id"),
    ({"status": "fine"}, "status must be"),
    ({"summary": ""}, "summary is missing"),
    ({"summary": "x" * 121}, "longer than 120"),
    ({"values": [{"label": "a", "value": "b"}] * 7}, "at most 6"),
    ({"values": [{"label": "a"}]}, "label and a value"),
    ({"values": [{"label": "a", "value": "b", "status": "meh"}]}, "unknown status"),
    ({"summary": "Light at 192.168.1.44 is off"}, "address"),
    ({"summary": "fd7a:115c:a1e0::12 down"}, "address"),
    ({"summary": "Signed in as someone@example.com"}, "e-mail"),
])
def test_check_feed_names_each_problem(change, problem):
    assert any(problem in p for p in hf.check_feed(good(**change)))


def test_check_feed_finds_private_names_and_leaves_times_alone():
    assert hf.check_feed(good(summary="Porch Light is off"), private_words=["porch light"])
    assert hf.check_feed(good(summary="Last backup 02:30:15, next 22:30")) == []


def test_make_feed_trims_long_text_and_bad_status():
    f = hf.make_feed("x", "X", "1", "great", "y" * 200, [("L" * 30, "V" * 50)] * 8)
    assert f["status"] == "down" and len(f["summary"]) == 120 and len(f["values"]) == 6
    assert len(f["values"][0]["label"]) == 24 and len(f["values"][0]["value"]) == 40
    assert hf.check_feed(f) == []


def test_worst():
    assert hf.worst(["ok", "warn", "ok"]) == "warn"
    assert hf.worst(["ok", "down", "warn"]) == "down"
    assert hf.worst([]) == "ok"


class FakeService(PollingService):
    key, label = "sonos", "Sonos"

    def __init__(self, up, text=None):
        super().__init__(log=lambda m: None)
        self.status["up"] = up
        self._text = text

    def health_text(self):
        return self._text


@pytest.fixture
def controller(tmp_path):
    store = Store(str(tmp_path / "s.json"))
    c = Controller(Hub([DemoBackend()], store), store)
    c.connect()
    return c


def test_home_feed_all_ok(controller):
    f = home_feed(controller, services=[FakeService(True, "playing")], started_at=1, now=61)
    assert f["status"] == "ok" and f["summary"] == "0 of 4 lights on; all devices answering"
    assert {"label": "Sonos", "value": "playing", "status": "ok"} in f["values"]
    assert hf.check_feed(f, private_words=[d.name for d in controller.devices.values()]) == []


def test_home_feed_warns_when_a_device_is_down(controller):
    controller.power("device:Demo Lamp", True)
    f = home_feed(controller, services=[FakeService(False)], started_at=1, now=61)
    assert f["status"] == "warn"
    assert f["summary"] == "1 of 4 lights on; not answering: Sonos"
    assert {"label": "Sonos", "value": "not answering", "status": "down"} in f["values"]
    assert hf.check_feed(f, private_words=["Demo Lamp"]) == []


def test_home_feed_down_when_nothing_answers(controller):
    controller.hub.status["demo"]["connected"] = False
    f = home_feed(controller, services=[FakeService(False)], started_at=1, now=61)
    assert f["status"] == "down" and f["summary"].startswith("Lights not answering")
