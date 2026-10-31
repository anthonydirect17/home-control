"""Saved groups, effect presets, schedules and settings.

Everything lives in one JSON file under data/ (ignored by git, since it holds
device names, network addresses and your location). Writes go to a temp file first so a crash
can't leave a half-written file.
"""

import copy
import json
import os
import threading
import uuid

DEFAULTS = {
    "settings": {
        "latitude": None,
        "longitude": None,
        "guid_credential": None,
        "govee_dll": None,
        "default_fps": 4,
        "use_govee_desktop": True,
        "secrets_store": "file",  # data/secrets.json (default) or "credential-manager"; see secrets.py
        "node_path": "node",       # for the Ring helper
        "ring_helper_port": 8782,  # 127.0.0.1 only
        # V2 device layer (private: names and addresses stay in data/)
        "pcs": [],             # [{"label", "this_machine": true} or {"label", "tailscale_name"}]; see pcstatus.py
        "sonos": [],           # [{"uid", "name", "ip"}] remembered from discovery; see sonos.py
        "sonos_max_volume": 100,  # a ceiling for the volume slider and rules; 100 = none
        "vacation": False,     # VACATION is AWAY plus this flag; see modes.py
        "allow_app_arming": False,  # arming/disarming from the app; the owner turns it on with the PIN
        "camera_siren_seconds": 30,  # the app stops a camera siren itself after this long
        "garage": {},          # {"camera_id", "box": [x0, y0, x1, y1] as fractions}; see garage.py
    },
    "groups": {},      # name -> {"members": [device names], "mode": "span" | "mirror"}
    "presets": {},     # name -> {"pattern": str, "params": dict}
    "schedules": [],   # see scheduler.py
    "aliases": {},     # device id -> name you gave it here
    "lan_cache": {},   # device id -> {"ip", "sku"} from the last LAN scan
    "rules": [],       # see rules.py: when / only if / then / until
}


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            for key, default in DEFAULTS.items():
                value = loaded.get(key, copy.deepcopy(default))
                if isinstance(default, dict) and key == "settings":
                    merged = copy.deepcopy(default)
                    merged.update(value or {})
                    value = merged
                self._data[key] = value

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._data[key])

    def put(self, key, value):
        with self._lock:
            if key not in DEFAULTS:
                raise KeyError(key)
            self._data[key] = copy.deepcopy(value)
            self._save()

    # settings
    def update_settings(self, changes: dict):
        with self._lock:
            for k, v in changes.items():
                if k in DEFAULTS["settings"]:
                    self._data["settings"][k] = v
            self._save()
            return copy.deepcopy(self._data["settings"])

    def set_alias(self, device_id, name):
        with self._lock:
            if name:
                self._data["aliases"][device_id] = name
            else:
                self._data["aliases"].pop(device_id, None)
            self._save()

    # groups
    def put_group(self, name, members, mode="span"):
        if mode not in ("span", "mirror"):
            raise ValueError("mode must be 'span' or 'mirror'")
        with self._lock:
            self._data["groups"][name] = {"members": list(members), "mode": mode}
            self._save()

    def delete_group(self, name):
        with self._lock:
            self._data["groups"].pop(name, None)
            self._save()

    # presets
    def put_preset(self, name, pattern, params):
        with self._lock:
            self._data["presets"][name] = {"pattern": pattern, "params": dict(params or {})}
            self._save()

    def delete_preset(self, name):
        with self._lock:
            self._data["presets"].pop(name, None)
            self._save()

    # schedules
    def put_schedule(self, entry: dict):
        with self._lock:
            entry = dict(entry)
            entry.setdefault("id", uuid.uuid4().hex[:8])
            items = self._data["schedules"]
            for i, existing in enumerate(items):
                if existing["id"] == entry["id"]:
                    entry.setdefault("last_fired", existing.get("last_fired"))
                    items[i] = entry
                    break
            else:
                items.append(entry)
            self._save()
            return copy.deepcopy(entry)

    def delete_schedule(self, sid):
        with self._lock:
            self._data["schedules"] = [s for s in self._data["schedules"] if s["id"] != sid]
            self._save()

    def mark_fired(self, sid, stamp):
        with self._lock:
            for s in self._data["schedules"]:
                if s["id"] == sid:
                    s["last_fired"] = stamp
            self._save()
