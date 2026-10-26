"""Combines every light backend into one list of lights.

The same physical light can be reachable several ways: by name through
Govee Desktop, by device id through the cloud, by device id and ip on the
LAN. The hub merges those into one Device and, for each command, uses the
best route that can do it, falling back to the next one if it fails.
"""

import threading

from .govee.base import BackendError, Device

# Preferred route for each kind of command, best first.
PREFERENCE = {
    "power": ("lan", "desktop", "demo", "cloud"),
    "brightness": ("lan", "desktop", "demo", "cloud"),
    "color": ("lan", "desktop", "demo", "cloud"),
    "segments": ("desktop", "demo"),
    "scenes": ("cloud", "demo"),
    "state": ("lan", "demo", "cloud"),
}
# Routes fast enough for frame-by-frame effects.
EFFECT_ROUTES = ("desktop", "demo", "lan")
# Merge order: the cloud knows names and ids, so it goes first.
MERGE_ORDER = ("cloud", "demo", "lan", "desktop")


def merge(found, aliases=None):
    aliases = aliases or {}
    devices, by_name = {}, {}
    found = sorted(found, key=lambda f: MERGE_ORDER.index(f.route) if f.route in MERGE_ORDER else 99)
    for f in found:
        dev = devices.get(f.id) if f.id else None
        if dev is None and f.name:
            dev = by_name.get(f.name.strip().lower())
        if dev is None:
            dev_id = f.id or f"{f.route}:{f.name}"
            dev = Device(id=dev_id, name=f.name, model=f.model, segments=max(1, f.segments))
            devices[dev_id] = dev
        dev.routes[f.route] = f.address
        if f.name and not dev.name:
            dev.name = f.name
        if f.model and not dev.model:
            dev.model = f.model
        if f.route == "desktop":
            dev.segments = max(1, f.segments)  # what segment commands will actually use
        elif f.segments > dev.segments and "desktop" not in dev.routes:
            dev.segments = f.segments
        if dev.name:
            by_name[dev.name.strip().lower()] = dev
    for dev in devices.values():
        if aliases.get(dev.id):
            dev.name = aliases[dev.id]
        elif not dev.name:
            dev.name = f"{dev.model or 'Govee light'} ({dev.id[-4:]})"
    return devices


class Hub:
    def __init__(self, backends, store=None):
        self.backends = {b.route: b for b in backends}
        self.store = store
        self.devices = {}
        self.status = {route: {"label": b.label, "connected": False, "error": None, "note": None}
                       for route, b in self.backends.items()}
        self._lock = threading.Lock()

    def connect(self):
        for route, b in self.backends.items():
            try:
                b.connect()
                self.status[route].update(connected=True, error=None)
            except BackendError as e:
                self.status[route].update(connected=False, error=str(e))
            self.status[route]["note"] = b.status_note()
        return self.status

    def refresh(self):
        found = []
        for route, b in self.backends.items():
            if not self.status[route]["connected"]:
                continue
            try:
                found.extend(b.discover())
                self.status[route]["error"] = None
            except BackendError as e:
                self.status[route]["error"] = str(e)
            self.status[route]["note"] = b.status_note()
        aliases = self.store.get("aliases") if self.store else {}
        with self._lock:
            self.devices = merge(found, aliases)
        return list(self.devices.values())

    def any_connected(self):
        return any(s["connected"] for s in self.status.values())

    def routes_for(self, dev, capability):
        order = PREFERENCE.get(capability, ())
        return [r for r in order if r in dev.routes and r in self.backends
                and self.status[r]["connected"] and capability in self.backends[r].capabilities]

    def can(self, dev, capability):
        return bool(self.routes_for(dev, capability))

    def do(self, dev, capability, *args):
        """Run a command on the best route, trying the next on failure."""
        routes = self.routes_for(dev, capability)
        if not routes:
            raise BackendError(f"{dev.name}: no connection that can do {capability}")
        errors = []
        for r in routes:
            try:
                return getattr(self.backends[r], capability)(dev.routes[r], *args)
            except BackendError as e:
                errors.append(f"{self.backends[r].label}: {e}")
        raise BackendError(f"{dev.name}: " + "; ".join(errors))

    def effect_route(self, dev):
        for r in EFFECT_ROUTES:
            if r in dev.routes and r in self.backends and self.status[r]["connected"]:
                return r
        return None

    def send_frame(self, dev, hexes, rgb_of):
        """Send one effect frame. Lights without a segment route get the first color."""
        route = self.effect_route(dev)
        if route is None:
            raise BackendError("effects need a LAN or Govee Desktop connection to this light")
        b = self.backends[route]
        if "segments" in b.capabilities and dev.segments > 1:
            b.segments(dev.routes[route], hexes)
        else:
            b.color(dev.routes[route], rgb_of(hexes[0]))
        return route

    def scenes(self, dev):
        return self.do(dev, "scenes")

    def run_scene(self, dev, scene_name):
        for r in self.routes_for(dev, "scenes"):
            b = self.backends[r]
            match = next((s for s in b.scenes(dev.routes[r]) if s["name"].lower() == scene_name.lower()), None)
            if match:
                return b.run_scene(dev.routes[r], match)
        raise BackendError(f"{dev.name}: no scene named {scene_name!r}")
