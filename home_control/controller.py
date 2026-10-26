"""The middle layer: turns page and schedule commands into light commands.

A "target" is either "device:<name>" or "group:<name>". Groups have a mode:
  span   - an effect runs across all members as one long strip (linked)
  mirror - every member runs the same effect side by side

Govee Desktop can't report a light's current state, so the state shown here
is whatever this app last sent.
"""

import threading
import time

from . import patterns
from .govee.base import BackendError


class Effect:
    def __init__(self, controller, members, pattern, params, fps, mode):
        self.controller = controller
        self.members = members  # list of Device
        self.pattern = pattern
        self.params = dict(params or {})
        self.fps = max(0.5, min(20.0, float(fps)))
        self.mode = mode
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"effect-{pattern}")
        self._last = {}

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=2):
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def frames(self, t):
        """{device name: [hex, ...]} for time t."""
        out = {}
        if self.mode == "span" and len(self.members) > 1:
            total = sum(d.segments for d in self.members)
            colors = patterns.render(self.pattern, t, total, self.params)
            i = 0
            for d in self.members:
                out[d.name] = [patterns.rgb_to_hex(c) for c in colors[i:i + d.segments]]
                i += d.segments
        else:
            for d in self.members:
                colors = patterns.render(self.pattern, t, d.segments, self.params)
                out[d.name] = [patterns.rgb_to_hex(c) for c in colors]
        return out

    def _run(self):
        start = time.monotonic()
        static = patterns.is_static(self.pattern)
        while not self._stop.is_set():
            tick = time.monotonic()
            for name, hexes in self.frames(tick - start).items():
                if self._stop.is_set():
                    break
                if self._last.get(name) == hexes:
                    continue
                if self.controller._send_frame(name, hexes):
                    self._last[name] = hexes
            if static and len(self._last) == len(self.members):
                self._stop.wait()
                break
            self._stop.wait(max(0.0, 1.0 / self.fps - (time.monotonic() - tick)))


class Controller:
    def __init__(self, backend, store):
        self.backend = backend
        self.store = store
        self.devices = {}
        self.state = {}
        self.status = {"connected": False, "error": None, "backend": backend.label}
        self._lock = threading.RLock()
        self._effects = {}  # device name -> Effect

    # setup

    def connect(self):
        try:
            self.backend.connect()
            self.refresh()
            self.status.update(connected=True, error=None)
        except BackendError as e:
            self.status.update(connected=False, error=str(e))
        return dict(self.status)

    def refresh(self):
        found = self.backend.list_devices()
        with self._lock:
            self.devices = {d.name: d for d in found}
            for d in found:
                self.state.setdefault(d.name, {"on": None, "brightness": None, "color": None,
                                               "effect": None, "frame": None, "error": None})
        return found

    # targets

    def resolve(self, target):
        kind, _, name = (target or "").partition(":")
        if kind == "device":
            if name not in self.devices:
                raise KeyError(f"no device named {name!r}")
            return [self.devices[name]], "single"
        if kind == "group":
            group = self.store.get("groups").get(name)
            if group is None:
                raise KeyError(f"no group named {name!r}")
            members = [self.devices[m] for m in group["members"] if m in self.devices]
            if not members:
                raise KeyError(f"group {name!r} has no available devices")
            return members, group.get("mode", "span")
        raise KeyError(f"target must start with 'device:' or 'group:', got {target!r}")

    def _stop_effects_on(self, names):
        stopped = []
        with self._lock:
            for n in names:
                eff = self._effects.get(n)
                if eff and eff not in stopped:
                    eff.stop()
                    stopped.append(eff)
            for eff in stopped:
                for d in eff.members:
                    if self._effects.get(d.name) is eff:
                        del self._effects[d.name]
                        self.state[d.name]["effect"] = None
        for eff in stopped:
            eff.join()

    def _each(self, members, fn):
        """Run fn on each device; return {name: error} for the ones that failed."""
        errors = {}
        for d in members:
            try:
                fn(d)
                self.state[d.name]["error"] = None
            except BackendError as e:
                self.state[d.name]["error"] = str(e)
                errors[d.name] = str(e)
        return errors

    # commands

    def power(self, target, on):
        members, _ = self.resolve(target)
        if not on:
            self._stop_effects_on([d.name for d in members])

        def act(d):
            self.backend.power(d.name, on)
            self.state[d.name]["on"] = bool(on)
        return self._each(members, act)

    def brightness(self, target, value):
        members, _ = self.resolve(target)
        value = max(1, min(100, int(value)))

        def act(d):
            self.backend.brightness(d.name, value)
            self.state[d.name]["brightness"] = value
        return self._each(members, act)

    def color(self, target, hex_color):
        rgb = patterns.hex_to_rgb(hex_color)
        hex_color = patterns.rgb_to_hex(rgb)
        members, _ = self.resolve(target)
        self._stop_effects_on([d.name for d in members])

        def act(d):
            self.backend.color(d.name, rgb)
            self.state[d.name].update(on=True, color=hex_color, frame=[hex_color] * d.segments)
        return self._each(members, act)

    def start_effect(self, target, pattern, params=None, fps=None):
        if pattern not in patterns.PATTERNS:
            raise KeyError(f"unknown pattern {pattern!r}")
        members, mode = self.resolve(target)
        self._stop_effects_on([d.name for d in members])
        # Govee ignores segment colors while a light is off, so switch it on first.
        errors = self._each(members, lambda d: (self.backend.power(d.name, True),
                                                self.state[d.name].update(on=True)))
        fps = fps or self.store.get("settings").get("default_fps") or 4
        eff = Effect(self, members, pattern, params, fps, mode)
        label = {"pattern": pattern, "params": eff.params, "target": target, "fps": eff.fps}
        with self._lock:
            for d in members:
                self._effects[d.name] = eff
                self.state[d.name]["effect"] = label
        eff.start()
        return errors

    def stop(self, target):
        members, _ = self.resolve(target)
        self._stop_effects_on([d.name for d in members])
        return {}

    def stop_all(self):
        self._stop_effects_on(list(self._effects))

    def _send_frame(self, name, hexes):
        d = self.devices.get(name)
        if d is None:
            return False
        try:
            if d.segments > 1:
                self.backend.segments(name, hexes)
            else:
                self.backend.color(name, patterns.hex_to_rgb(hexes[0]))
            self.state[name].update(frame=list(hexes), error=None)
            return True
        except BackendError as e:
            self.state[name]["error"] = str(e)
            return False

    def run_action(self, target, action):
        """Run a saved action (used by schedules)."""
        kind = action.get("type")
        if kind == "on":
            return self.power(target, True)
        if kind == "off":
            return self.power(target, False)
        if kind == "brightness":
            return self.brightness(target, action["value"])
        if kind == "color":
            return self.color(target, action["color"])
        if kind == "preset":
            preset = self.store.get("presets").get(action["preset"])
            if preset is None:
                raise KeyError(f"no effect named {action['preset']!r}")
            return self.start_effect(target, preset["pattern"], preset["params"], action.get("fps"))
        if kind == "pattern":
            return self.start_effect(target, action["pattern"], action.get("params"), action.get("fps"))
        if kind == "stop":
            return self.stop(target)
        raise KeyError(f"unknown action type {kind!r}")

    def snapshot(self):
        with self._lock:
            return [dict(d.to_dict(), **self.state.get(d.name, {})) for d in self.devices.values()]
