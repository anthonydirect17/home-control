"""What every light backend provides, and the device record the app keeps."""

from dataclasses import dataclass, field


class BackendError(Exception):
    """A command or connection failed. The message is meant for the page."""


@dataclass
class Device:
    name: str
    model: str = ""
    segments: int = 1
    lan_on: bool = True
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "name": self.name,
            "model": self.model,
            "segments": self.segments,
            "lan_on": self.lan_on,
        }


class Backend:
    """Interface for something that can drive Govee lights.

    Devices are addressed by the name shown in Govee. Methods raise
    BackendError on failure.
    """

    label = "backend"

    def connect(self):
        raise NotImplementedError

    def list_devices(self):
        raise NotImplementedError

    def power(self, name: str, on: bool):
        raise NotImplementedError

    def brightness(self, name: str, value: int):
        raise NotImplementedError

    def color(self, name: str, rgb):
        raise NotImplementedError

    def segments(self, name: str, hex_colors):
        raise NotImplementedError
