"""Govee's official cloud API (openapi.api.govee.com).

Reaches every light on the account, including ones without LAN Control, and
can run the light's built-in scenes and your DIY scenes from the Govee app.
It is slower than LAN (about a second per command) and Govee caps how many
requests an account can make, so it's used for commands, never for
frame-by-frame effects.

Needs the API key requested in the Govee Home app (Profile -> Settings ->
Apply for API Key). The key is read from Windows Credential Manager or the
GOVEE_API_KEY environment variable, never from this folder.
"""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date

from .base import Backend, BackendError, Found, norm_id

BASE_URL = "https://openapi.api.govee.com/router/api/v1"
SCENE_CACHE_SECONDS = 1800


def segment_count(capabilities):
    for cap in capabilities or []:
        if cap.get("instance") != "segmentedColorRgb":
            continue
        for f in (cap.get("parameters") or {}).get("fields") or []:
            if f.get("fieldName") == "segment":
                rng = f.get("elementRange") or {}
                if "max" in rng:
                    return int(rng["max"]) - int(rng.get("min", 0)) + 1
                if f.get("options"):
                    return len(f["options"])
    return 1


class CloudBackend(Backend):
    route = "cloud"
    label = "Govee cloud"
    capabilities = frozenset({"power", "brightness", "color", "scenes", "state"})

    def __init__(self, api_key, base_url=BASE_URL, timeout=15):
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._scene_cache = {}
        self._lock = threading.Lock()
        self.requests_today = 0
        self._day = date.today()

    def status_note(self):
        return f"{self.requests_today} requests to Govee's cloud today"

    def _request(self, method, path, payload=None):
        with self._lock:
            if date.today() != self._day:
                self._day, self.requests_today = date.today(), 0
            self.requests_today += 1
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={"Govee-API-Key": self._key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise BackendError("Govee rejected the API key") from e
            if e.code == 429:
                raise BackendError("Govee's request limit was reached; try again in a bit") from e
            raise BackendError(f"Govee cloud error {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BackendError(f"couldn't reach Govee's cloud: {getattr(e, 'reason', e)}") from e
        except ValueError as e:
            raise BackendError("Govee's cloud sent an unreadable reply") from e
        if data.get("code") not in (200, None):
            raise BackendError(f"Govee cloud: {data.get('msg') or data.get('message') or data.get('code')}")
        return data

    def connect(self):
        self._request("GET", "/user/devices")

    def discover(self):
        data = self._request("GET", "/user/devices")
        found = []
        for d in data.get("data") or []:
            caps = d.get("capabilities") or []
            address = {
                "sku": d.get("sku", ""),
                "device": d.get("device", ""),
                "instances": sorted({c.get("instance") for c in caps if c.get("instance")}),
            }
            found.append(Found(route=self.route, address=address, id=norm_id(d.get("device")),
                               name=d.get("deviceName", ""), model=d.get("sku", ""),
                               segments=segment_count(caps)))
        return found

    def _control(self, address, cap_type, instance, value):
        if instance not in address.get("instances", []):
            raise BackendError(f"this light doesn't support {instance} through Govee's cloud")
        self._request("POST", "/device/control", {
            "requestId": uuid.uuid4().hex,
            "payload": {
                "sku": address["sku"],
                "device": address["device"],
                "capability": {"type": cap_type, "instance": instance, "value": value},
            },
        })

    def power(self, address, on):
        self._control(address, "devices.capabilities.on_off", "powerSwitch", 1 if on else 0)

    def brightness(self, address, value):
        self._control(address, "devices.capabilities.range", "brightness", max(1, min(100, int(value))))

    def color(self, address, rgb):
        r, g, b = (max(0, min(255, int(c))) for c in rgb)
        self._control(address, "devices.capabilities.color_setting", "colorRgb", (r << 16) + (g << 8) + b)

    def scenes(self, address):
        key = address["device"]
        cached = self._scene_cache.get(key)
        if cached and time.time() - cached[0] < SCENE_CACHE_SECONDS:
            return cached[1]
        out = []
        payload = {"requestId": uuid.uuid4().hex, "payload": {"sku": address["sku"], "device": address["device"]}}
        for path, kind in (("/device/scenes", "Govee scene"), ("/device/diy-scenes", "Your DIY scene")):
            try:
                data = self._request("POST", path, payload)
            except BackendError:
                continue
            for cap in (data.get("payload") or {}).get("capabilities") or []:
                for opt in (cap.get("parameters") or {}).get("options") or []:
                    out.append({"name": opt.get("name", "?"), "kind": kind, "type": cap.get("type"),
                                "instance": cap.get("instance"), "value": opt.get("value")})
        self._scene_cache[key] = (time.time(), out)
        return out

    def run_scene(self, address, scene):
        self._request("POST", "/device/control", {
            "requestId": uuid.uuid4().hex,
            "payload": {
                "sku": address["sku"],
                "device": address["device"],
                "capability": {"type": scene["type"], "instance": scene["instance"], "value": scene["value"]},
            },
        })

    def state(self, address):
        data = self._request("POST", "/device/state", {
            "requestId": uuid.uuid4().hex,
            "payload": {"sku": address["sku"], "device": address["device"]},
        })
        out = {}
        for cap in (data.get("payload") or {}).get("capabilities") or []:
            value = (cap.get("state") or {}).get("value")
            inst = cap.get("instance")
            if inst == "powerSwitch" and value is not None:
                out["on"] = bool(value)
            elif inst == "brightness" and isinstance(value, (int, float)):
                out["brightness"] = int(value)
            elif inst == "colorRgb" and isinstance(value, int) and value > 0:
                out["color"] = "#{:06X}".format(value)
        return out
