"""Pretend lights, for trying the app without Govee Desktop (python -m home_control --demo)."""

import threading

from .base import Backend, BackendError, Device

DEMO_DEVICES = [
    Device("Demo Porch Strip", "demo strip", 20),
    Device("Demo Spotlights", "demo spotlights", 4),
    Device("Demo Lamp", "demo bulb", 1),
    Device("Demo Offline Light", "demo bulb", 1, lan_on=False),
]


class DemoBackend(Backend):
    label = "Demo (no real lights)"

    def __init__(self, devices=None):
        self._devices = list(devices or DEMO_DEVICES)
        self._lock = threading.Lock()
        self.calls = []  # every command, for tests

    def connect(self):
        pass

    def list_devices(self):
        return list(self._devices)

    def _record(self, *call):
        name = call[1]
        dev = next((d for d in self._devices if d.name == name), None)
        if dev is None:
            raise BackendError(f"{name}: no device with that name")
        if not dev.lan_on:
            raise BackendError(f"{name}: device is offline")
        with self._lock:
            self.calls.append(call)
        return 0

    def power(self, name, on):
        return self._record("power", name, bool(on))

    def brightness(self, name, value):
        return self._record("brightness", name, int(value))

    def color(self, name, rgb):
        return self._record("color", name, tuple(rgb))

    def segments(self, name, hex_colors):
        return self._record("segments", name, tuple(hex_colors))
