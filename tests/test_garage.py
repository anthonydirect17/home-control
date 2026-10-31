"""Garage status from a camera picture: references, reading, can't tell, last seen, the timing."""

import io

import pytest
from PIL import Image, ImageDraw

from home_control import garage as g
from home_control.events import EventBus
from home_control.garage import GarageWatcher, classify, distance, patch_of
from home_control.services import ServiceError
from home_control.store import Store

BOX = [0.0, 0.0, 0.25, 0.4]


def picture(door, light=128, noise_seed=0):
    """A 320x180 frame: the door sits in the top-left corner; the rest is unrelated scenery."""
    img = Image.new("L", (320, 180), light)
    d = ImageDraw.Draw(img)
    if door == "closed":   # panel lines across the door
        for y in range(0, 72, 12):
            d.rectangle([0, y, 80, y + 5], fill=min(255, light + 60))
    elif door == "open":   # a dark opening with a bright floor
        d.rectangle([0, 0, 80, 50], fill=max(0, light - 100))
        d.rectangle([0, 50, 80, 72], fill=min(255, light + 80))
    d.ellipse([200 + noise_seed, 60, 260 + noise_seed, 120], fill=40)  # something moving elsewhere
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeRing:
    def __init__(self):
        self.frame = picture("closed")
        self.battery = False
        self.fail = False

    def request(self, method, path):
        if self.fail:
            raise RuntimeError("helper down")
        return "image/jpeg", self.frame

    def get_json(self, path):
        return {"cameras": [{"id": "7", "onBattery": self.battery}]}


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / "s.json"))
    bus = EventBus()
    ring = FakeRing()
    clock = Clock()
    timers = []

    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            timers.append(self)

        def start(self):
            pass

    w = GarageWatcher(store, ring, bus, str(tmp_path / "data"), timer=Timer, clock=clock, log=lambda m: None)
    return w, ring, bus, clock, timers, store


def ready(w, ring):
    w.setup("7", BOX)
    ring.frame = picture("open")
    w.save_reference("open")
    ring.frame = picture("closed")
    w.save_reference("closed")


def test_patch_and_distance():
    a = patch_of(picture("closed"), BOX)
    assert len(a) == g.PATCH * g.PATCH
    assert distance(a, a) == pytest.approx(0, abs=1e-9)
    brighter = patch_of(picture("closed", light=170), BOX)
    other = patch_of(picture("open"), BOX)
    assert distance(a, brighter) < distance(a, other)  # overall light matters less than the pattern
    with pytest.raises(ServiceError, match="couldn't be read"):
        patch_of(b"<svg/>", BOX)
    with pytest.raises(ValueError):
        patch_of(picture("closed"), [0.5, 0.5, 0.2, 0.9])


def test_classify_and_cant_tell():
    refs = {"open": [patch_of(picture("open"), BOX)], "closed": [patch_of(picture("closed"), BOX)]}
    assert classify(patch_of(picture("open", light=110, noise_seed=20), BOX), refs)[0] == "open"
    assert classify(patch_of(picture("closed", light=150, noise_seed=30), BOX), refs)[0] == "closed"
    flat = [128] * (g.PATCH * g.PATCH)  # neither: a blank corner (camera covered, fog)
    assert classify(flat, refs)[0] == "unknown"
    def z(p):
        m = sum(p) / len(p)
        sd = (sum((x - m) ** 2 for x in p) / len(p)) ** 0.5
        return [(x - m) / sd for x in p]
    half = [128 + 20 * (x + y) for x, y in zip(z(refs["open"][0]), z(refs["closed"][0]))]
    assert classify(half, refs)[0] == "unknown"  # matches both about equally
    assert classify(flat, {"open": refs["open"]})[0] == "unknown"  # needs both kinds


def test_not_set_up_then_references_then_reading(setup):
    w, ring, bus, clock, timers, store = setup
    assert w.poll_once() and w.health_text() == "not set up"
    w.setup("7", BOX)
    assert w.poll_once() and "reference" in w.status["note"]
    ring.frame = picture("open")
    assert w.save_reference("open") == 1
    ring.frame = picture("closed")
    w.save_reference("closed")
    assert w.poll_once()
    assert w.health_text() == "closed" and w.state()["references"] == {"open": 1, "closed": 1}
    assert [e["kind"] for e in bus.recent(source="garage")] == ["garage-closed"]
    ring.frame = picture("open", light=100)
    w.poll_once()
    assert w.health_text() == "open"
    assert [e["kind"] for e in bus.recent(source="garage")] == ["garage-open", "garage-closed"]


def test_references_are_capped_and_reset_when_the_corner_moves(setup):
    w, ring, bus, clock, timers, store = setup
    ready(w, ring)
    for _ in range(6):
        w.save_reference("closed")
    assert w.state()["references"]["closed"] == g.MAX_REFS
    w.setup("7", [0.1, 0.1, 0.3, 0.5])
    assert w.refs() == {}


def test_cant_tell_then_last_seen(setup):
    w, ring, bus, clock, timers, store = setup
    ready(w, ring)
    w.poll_once()
    ring.frame = picture("none")  # neither pattern
    w.poll_once()
    s = w.shown()
    assert s["shown"] == "can't tell" and s["state"] == "closed"
    clock.t += g.STALE_AFTER + 1
    assert w.health_text() == "last seen closed"


def test_battery_camera_reads_every_ten_minutes_and_after_motion(setup):
    w, ring, bus, clock, timers, store = setup
    ready(w, ring)
    ring.battery = True
    w.poll_once()
    assert w.interval == g.BATTERY_EVERY and w.state()["on_battery"] is True
    bus.publish({"source": "ring", "kind": "motion", "camera": {"id": "7"}})
    bus.publish({"source": "ring", "kind": "motion", "camera": {"id": "8"}})  # another camera: ignored
    assert [t.delay for t in timers] == [g.BATTERY_AFTER_MOTION]
    ring.frame = picture("open")
    timers[0].fn()
    assert w.health_text() == "open"
    ring.battery = False
    w.poll_once()
    assert w.interval == g.WIRED_EVERY


def test_camera_trouble_marks_it_down(setup):
    w, ring, bus, clock, timers, store = setup
    ready(w, ring)
    ring.fail = True
    assert not w.poll_once()
    assert "no picture from the garage camera" in w.status["error"]
