"""The middle layer: turns page, timer and rule commands into light commands.

A "target" is either "device:<id or name>" or "group:<name>". Groups have a mode:
  span   - an effect runs across all members as one long strip (linked)
  mirror - every member runs the same effect side by side

State comes from two places: what this app last sent, and (for lights on the
LAN) what the light itself reports when polled.
"""

import threading
import time

from . import patterns
from .govee.base import BackendError


def _blank_state():
    return {"on": None, "brightness": None, "color": None, "effect": None, "frame": None,
            "error": None, "reported": False, "scene": None}


class Effect:
    def __init__(self, controller, members, pattern, params, fps, mode):
        self.controller = controller
        self.members = members  # list of Device
        self.pattern = pattern
        self.params = dict(params or {})
        self.fps = max(0.5, min(20.0, float(fps)))
        self.mode = mode
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"effect-{pattern}")
        self._last = {}

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=2):
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def frames(self, t):
        """{device id: [hex, ...]} for time t."""
        out = {}
        if self.mode == "span" and len(self.members) > 1:
            total = sum(d.segments for d in self.members)
            colors = patterns.render(self.pattern, t, total, self.params)
            i = 0
            for d in self.members:
                out[d.id] = [patterns.rgb_to_hex(c) for c in colors[i:i + d.segments]]
                i += d.segments
        else:
            for d in self.members:
                colors = patterns.render(self.pattern, t, d.segments, self.params)
                out[d.id] = [patterns.rgb_to_hex(c) for c in colors]
        return out

    def _run(self):
        start = time.monotonic()
        static = patterns.is_static(self.pattern)
        while not self._stop.is_set():
            tick = time.monotonic()
            for dev_id, hexes in self.frames(tick - start).items():
                if self._stop.is_set():
                    break
                if self._last.get(dev_id) == hexes:
                    continue
                if self.controller._send_frame(dev_id, hexes):
                    self._last[dev_id] = hexes
            if static and len(self._last) == len(self.members):
                self._stop.wait()
                break
            self._stop.wait(max(0.0, 1.0 / self.fps - (time.monotonic() - tick)))


class Controller:
    def __init__(self, hub, store):
        self.hub = hub
        self.store = store
        self.state = {}
        self._lock = threading.RLock()
        self._effects = {}  # device id -> Effect

    @property
    def devices(self):
        return self.hub.devices

    # setup

    def connect(self):
        self.hub.connect()
        self.refresh()
        return self.status()

    def refresh(self):
        found = self.hub.refresh()
        with self._lock:
            for d in found:
                self.state.setdefault(d.id, _blank_state())
        return found

    def status(self):
        return {"connected": self.hub.any_connected(), "backends": self.hub.status,
                "device_count": len(self.devices)}

    # targets

    def device(self, key):
        if key in self.devices:
            return self.devices[key]
        for d in self.devices.values():
            if d.name.lower() == str(key).lower():
                return d
        return None

    def resolve(self, target):
        kind, _, name = (target or "").partition(":")
        if kind == "device":
            dev = self.device(name)
            if dev is None:
                raise KeyError(f"no light named {name!r}")
            return [dev], "single"
        if kind == "group":
            group = self.store.get("groups").get(name)
            if group is None:
                raise KeyError(f"no group named {name!r}")
            members = [d for d in (self.device(m) for m in group["members"]) if d is not None]
            if not members:
                raise KeyError(f"group {name!r} has no available lights")
            return members, group.get("mode", "span")
        raise KeyError(f"target must start with 'device:' or 'group:', got {target!r}")

    def _stop_effects_on(self, ids):
        stopped = []
        with self._lock:
            for i in ids:
                eff = self._effects.get(i)
                if eff and eff not in stopped:
                    eff.stop()
                    stopped.append(eff)
            for eff in stopped:
                for d in eff.members:
                    if self._effects.get(d.id) is eff:
                        del self._effects[d.id]
                        self.state[d.id]["effect"] = None
        for eff in stopped:
            eff.join()

    def _each(self, members, fn):
        """Run fn on each light; return {name: error} for the ones that failed."""
        errors = {}
        for d in members:
            try:
                fn(d)
                self.state[d.id]["error"] = None
            except BackendError as e:
                self.state[d.id]["error"] = str(e)
                errors[d.name] = str(e)
        return errors

    # commands

    def power(self, target, on):
        members, _ = self.resolve(target)
        if not on:
            self._stop_effects_on([d.id for d in members])

        def act(d):
            self.hub.do(d, "power", bool(on))
            self.state[d.id]["on"] = bool(on)
        return self._each(members, act)

    def brightness(self, target, value):
        members, _ = self.resolve(target)
        value = max(1, min(100, int(value)))

        def act(d):
            self.hub.do(d, "brightness", value)
            self.state[d.id]["brightness"] = value
        return self._each(members, act)

    def color(self, target, hex_color):
        rgb = patterns.hex_to_rgb(hex_color)
        hex_color = patterns.rgb_to_hex(rgb)
        members, _ = self.resolve(target)
        self._stop_effects_on([d.id for d in members])

        def act(d):
            self.hub.do(d, "color", rgb)
            self.state[d.id].update(on=True, color=hex_color, frame=[hex_color] * d.segments, scene=None)
        return self._each(members, act)

    def scene(self, target, scene_name):
        members, _ = self.resolve(target)
        self._stop_effects_on([d.id for d in members])

        def act(d):
            self.hub.run_scene(d, scene_name)
            self.state[d.id].update(on=True, color=None, frame=None, scene=scene_name)
        return self._each(members, act)

    def scenes(self, dev_key):
        dev = self.device(dev_key)
        if dev is None:
            raise KeyError(f"no light named {dev_key!r}")
        if not self.hub.can(dev, "scenes"):
            return []
        return [{"name": s["name"], "kind": s["kind"]} for s in self.hub.scenes(dev)]

    def start_effect(self, target, pattern, params=None, fps=None):
        if pattern not in patterns.PATTERNS:
            raise KeyError(f"unknown pattern {pattern!r}")
        members, mode = self.resolve(target)
        errors = {d.name: "effects need a LAN or Govee Desktop connection to this light"
                  for d in members if self.hub.effect_route(d) is None}
        members = [d for d in members if d.name not in errors]
        if not members:
            return errors
        self._stop_effects_on([d.id for d in members])
        # Govee ignores colors sent while a light is off, so switch it on first.
        errors.update(self._each(members, lambda d: (self.hub.do(d, "power", True),
                                                     self.state[d.id].update(on=True))))
        fps = fps or self.store.get("settings").get("default_fps") or 4
        eff = Effect(self, members, pattern, params, fps, mode)
        label = {"pattern": pattern, "params": eff.params, "target": target, "fps": eff.fps}
        with self._lock:
            for d in members:
                self._effects[d.id] = eff
                self.state[d.id]["effect"] = label
        eff.start()
        return errors

    def stop(self, target):
        members, _ = self.resolve(target)
        self._stop_effects_on([d.id for d in members])
        return {}

    def stop_all(self):
        self._stop_effects_on(list(self._effects))

    def _send_frame(self, dev_id, hexes):
        d = self.devices.get(dev_id)
        if d is None:
            return False
        try:
            self.hub.send_frame(d, hexes, patterns.hex_to_rgb)
            self.state[dev_id].update(frame=list(hexes), error=None)
            return True
        except BackendError as e:
            self.state[dev_id]["error"] = str(e)
            return False

    def poll_state(self):
        """Ask lights that can report their state (LAN, demo) what they're doing.

        Skips lights running an effect and never uses the cloud, which would
        eat into Govee's daily request limit.
        """
        for d in list(self.devices.values()):
            if d.id in self._effects:
                continue
            routes = [r for r in self.hub.routes_for(d, "state") if r != "cloud"]
            if not routes:
                continue
            try:
                st = self.hub.backends[routes[0]].state(d.routes[routes[0]])
            except BackendError:
                continue
            if st:
                s = self.state.setdefault(d.id, _blank_state())
                s.update(st, reported=True)
                if "color" in st:
                    s["frame"] = [st["color"]] * d.segments

    def run_action(self, target, action):
        """Run a saved action (used by timers and rules)."""
        kind = action.get("type")
        if kind == "on":
            return self.power(target, True)
        if kind == "off":
            return self.power(target, False)
        if kind == "brightness":
            return self.brightness(target, action["value"])
        if kind == "color":
            return self.color(target, action["color"])
        if kind == "scene":
            return self.scene(target, action["scene"])
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
            out = []
            for d in self.devices.values():
                row = dict(d.to_dict(), **self.state.get(d.id, _blank_state()))
                row["can_effects"] = self.hub.effect_route(d) is not None
                row["can_scenes"] = self.hub.can(d, "scenes")
                out.append(row)
            return sorted(out, key=lambda r: r["name"].lower())


class StatePoller:
    def __init__(self, controller, interval=20):
        self.controller = controller
        self.interval = interval
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="state-poller").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.controller.poll_state()
            except Exception:  # never let polling kill the app
                pass
