"""What every light backend provides, and the device record the app keeps.

A backend is one way of reaching lights: Govee's LAN API, Govee's cloud API,
Govee Desktop, or the demo. Each one reports the lights it can see as
`Found` records; the hub merges those into one `Device` per physical light,
remembering every route to it.
"""

from dataclasses import dataclass, field


class BackendError(Exception):
    """A command or connection failed. The message is meant for the page."""


def norm_id(device_id) -> str:
    """Govee device ids look like 'AA:BB:CC:DD:EE:FF:00:11'; compare without colons."""
    return str(device_id or "").replace(":", "").upper()


@dataclass
class Found:
    route: str                # which backend saw it ("lan", "cloud", "desktop", "demo")
    address: object           # whatever that backend needs to reach the light
    id: str = ""              # Govee device id, normalized, if the backend knows it
    name: str = ""            # name from the Govee app, if the backend knows it
    model: str = ""
    segments: int = 1


@dataclass
class Device:
    id: str
    name: str
    model: str = ""
    segments: int = 1
    routes: dict = field(default_factory=dict)  # route -> address

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "segments": self.segments,
            "routes": sorted(self.routes),
        }


class Backend:
    """Interface for one way of driving Govee lights.

    `address` is the value this backend put in Found.address. Methods raise
    BackendError on failure. `capabilities` lists what the backend can do:
    power, brightness, color, segments, scenes, state.
    """

    route = ""
    label = "backend"
    capabilities = frozenset()

    def connect(self):
        raise NotImplementedError

    def discover(self):
        raise NotImplementedError

    def close(self):
        """Release sockets and threads. Called when the app stops or restarts."""

    def status_note(self):
        """Extra detail for the status panel, or None."""
        return None

    def power(self, address, on: bool):
        raise BackendError(f"{self.label} can't switch lights")

    def brightness(self, address, value: int):
        raise BackendError(f"{self.label} can't set brightness")

    def color(self, address, rgb):
        raise BackendError(f"{self.label} can't set color")

    def segments(self, address, hex_colors):
        raise BackendError(f"{self.label} can't color segments")

    def scenes(self, address):
        raise BackendError(f"{self.label} has no scenes")

    def run_scene(self, address, scene):
        raise BackendError(f"{self.label} can't run scenes")

    def state(self, address):
        """{"on": bool, "brightness": int, "color": "#RRGGBB"} (keys optional), or None."""
        return None
