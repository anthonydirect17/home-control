"""Pretend lights, for trying the app without any Govee setup (python -m home_control --demo)."""

import threading

from .base import Backend, BackendError, Found

DEMO_DEVICES = [
    # (id, name, model, segments, reachable)
    ("DEMO0001", "Demo Porch Strip", "demo strip", 20, True),
    ("DEMO0002", "Demo Spotlights", "demo spotlights", 4, True),
    ("DEMO0003", "Demo Lamp", "demo bulb", 1, True),
    ("DEMO0004", "Demo Offline Light", "demo bulb", 1, False),
]

DEMO_SCENES = [
    {"name": "Sunrise", "kind": "Govee scene", "type": "demo", "instance": "lightScene", "value": 1},
    {"name": "Aurora", "kind": "Govee scene", "type": "demo", "instance": "lightScene", "value": 2},
    {"name": "My candy cane", "kind": "Your DIY scene", "type": "demo", "instance": "diyScene", "value": 3},
]


class DemoBackend(Backend):
    route = "demo"
    label = "Demo (no real lights)"
    capabilities = frozenset({"power", "brightness", "color", "segments", "scenes", "state"})

    def __init__(self, devices=None):
        self._devices = list(devices or DEMO_DEVICES)
        self._lock = threading.Lock()
        self._state = {}
        self.calls = []  # every command, for tests

    def connect(self):
        pass

    def discover(self):
        return [Found(route=self.route, address=name, id=dev_id, name=name, model=model, segments=segs)
                for dev_id, name, model, segs, _ in self._devices]

    def _record(self, *call):
        name = call[1]
        dev = next((d for d in self._devices if d[1] == name), None)
        if dev is None:
            raise BackendError(f"{name}: no device with that name")
        if not dev[4]:
            raise BackendError(f"{name}: device is offline")
        with self._lock:
            self.calls.append(call)
        return 0

    def power(self, address, on):
        self._record("power", address, bool(on))
        self._state.setdefault(address, {})["on"] = bool(on)

    def brightness(self, address, value):
        self._record("brightness", address, int(value))
        self._state.setdefault(address, {})["brightness"] = int(value)

    def color(self, address, rgb):
        self._record("color", address, tuple(rgb))
        self._state.setdefault(address, {})["color"] = "#{:02X}{:02X}{:02X}".format(*rgb)

    def segments(self, address, hex_colors):
        self._record("segments", address, tuple(hex_colors))

    def scenes(self, address):
        return list(DEMO_SCENES)

    def run_scene(self, address, scene):
        self._record("scene", address, scene["name"])

    def state(self, address):
        return dict(self._state.get(address, {}))
