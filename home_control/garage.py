"""The garage door's status from a Ring camera's picture: open, closed, or "can't tell". Status only.

Setup (on the setup screen, by Anthony): pick the camera that sees the door, mark the corner
of the picture where the door is (a box, as fractions of the picture), and save at least one
"open" and one "closed" reference from that camera. More references (night, rain) make it
surer; up to four of each are kept.

Reading: the marked corner is cut out, made grey and shrunk to a small square. Its pattern
is compared with each reference by correlation, which ignores overall brightness and
contrast (day, dusk, a cloud). The kind it matches best wins. When the corner shows almost
no detail (the camera covered, fog, a dark night), when it matches neither kind well, or
when it matches both about equally, the answer is "can't tell", and the page shows the last
sure reading with its time instead. No AI model, nothing leaves the box. The thresholds
below are starting values, tuned on the real camera at install.

How often (Ring's limits, V2 plan section 3.5):
- a wired camera: a picture every minute, and one 15 seconds after motion;
- a camera on battery: every 10 minutes (Ring allows no more), and one 45 seconds after
  motion, once the clip has finished (none can be taken while it records).
A reading older than 30 minutes is shown as "last seen".
"""

import io
import json
import os
import threading
import time

from .services import PollingService, ServiceError

PATCH = 24                 # the corner is compared as a 24 x 24 grey square
MIN_CONTRAST = 6.0         # less spread than this (0-255 scale) is a blank corner: can't tell
MAX_DISTANCE = 0.5         # 1 - correlation: matching no reference better than this: can't tell
MARGIN = 0.15              # open and closed matched about as well: can't tell
MAX_REFS = 4
WIRED_EVERY, BATTERY_EVERY = 60, 600
WIRED_AFTER_MOTION, BATTERY_AFTER_MOTION = 15, 45
STALE_AFTER = 1800


def patch_of(jpeg, box):
    """The marked corner as PATCH x PATCH grey values (0-255)."""
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("L")
    except Exception:
        raise ServiceError("the camera's picture couldn't be read") from None
    w, h = img.size
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("the marked corner must lie inside the picture")
    crop = img.crop((int(x0 * w), int(y0 * h), max(int(x1 * w), int(x0 * w) + 1), max(int(y1 * h), int(y0 * h) + 1)))
    small = crop.resize((PATCH, PATCH))
    flat = getattr(small, "get_flattened_data", None) or small.getdata  # get_flattened_data: Pillow 13+
    return list(flat())


def _spread(p):
    m = sum(p) / len(p)
    return (sum((x - m) ** 2 for x in p) / len(p)) ** 0.5


def distance(a, b):
    """1 - correlation of two patches: 0 is the same pattern, 1 unrelated, 2 the opposite."""
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    sa, sb = _spread(a), _spread(b)
    if sa == 0 or sb == 0:
        return 1.0
    corr = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * sa * sb)
    return 1.0 - corr


def classify(sample, refs):
    """("open" | "closed" | "unknown", {"open": d, "closed": d}) for a patch against the references."""
    d = {}
    for kind in ("open", "closed"):
        if refs.get(kind):
            d[kind] = round(min(distance(sample, r) for r in refs[kind]), 3)
    if len(d) < 2 or _spread(sample) < MIN_CONTRAST:
        return "unknown", d
    nearest = min(d, key=d.get)
    other = "closed" if nearest == "open" else "open"
    if d[nearest] > MAX_DISTANCE or d[other] - d[nearest] < MARGIN:
        return "unknown", d
    return nearest, d


class GarageWatcher(PollingService):
    key = "garage"
    label = "Garage"
    interval = WIRED_EVERY

    def __init__(self, store, ring, bus, data_dir, timer=threading.Timer, clock=time.time, **kw):
        super().__init__(clock=clock, **kw)
        self.store = store
        self.ring = ring
        self.bus = bus
        self.refs_path = os.path.join(data_dir, "garage_refs.json")
        self.timer = timer
        self.on_battery = None
        self.reading = {"state": None, "checked_at": None, "distances": {}}
        self.last_sure = None      # {"state", "at"}
        self.since = None          # when the door last changed
        self._lock = threading.Lock()
        bus.subscribe(self._on_event)

    # --- setup ---

    def config(self):
        return self.store.get("settings").get("garage") or {}

    def refs(self):
        try:
            with open(self.refs_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if k in ("open", "closed") and isinstance(v, list)}

    def setup(self, camera_id, box):
        x0, y0, x1, y1 = (float(v) for v in box)
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("the marked corner must lie inside the picture")
        old = self.config()
        if old.get("camera_id") != camera_id or old.get("box") != [x0, y0, x1, y1]:
            self._write_refs({})  # references belong to one camera and one corner
        self.store.update_settings({"garage": {"camera_id": str(camera_id), "box": [x0, y0, x1, y1]}})

    def save_reference(self, kind, jpeg=None):
        """Save the camera's current picture (or the one given) as an "open" or "closed" reference."""
        if kind not in ("open", "closed"):
            raise ValueError("a reference is open or closed")
        cfg = self.config()
        if not cfg.get("camera_id"):
            raise ServiceError("pick the garage camera first")
        sample = patch_of(jpeg if jpeg is not None else self._snapshot(cfg["camera_id"]), cfg["box"])
        refs = self.refs()
        refs[kind] = (refs.get(kind) or [])[-(MAX_REFS - 1):] + [sample]
        self._write_refs(refs)
        return len(refs[kind])

    def _write_refs(self, refs):
        os.makedirs(os.path.dirname(self.refs_path), exist_ok=True)
        tmp = self.refs_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(refs, f)
        os.replace(tmp, self.refs_path)

    # --- reading ---

    def _snapshot(self, camera_id):
        from urllib.parse import quote
        try:
            ctype, jpeg = self.ring.request("GET", f"/cameras/{quote(str(camera_id), safe='')}/snapshot")
        except Exception as e:
            raise ServiceError(f"no picture from the garage camera ({e})") from None
        return jpeg

    def _battery(self, camera_id):
        try:
            cams = self.ring.get_json("/devices").get("cameras", [])
        except Exception:
            return self.on_battery
        cam = next((c for c in cams if str(c.get("id")) == str(camera_id)), None)
        return None if cam is None else bool(cam.get("onBattery"))

    def poll(self):
        cfg = self.config()
        refs = self.refs()
        if not cfg.get("camera_id"):
            self.status["note"] = "not set up"
            return
        if not refs.get("open") or not refs.get("closed"):
            self.status["note"] = "save an open and a closed reference"
            return
        self.status["note"] = None
        self.on_battery = self._battery(cfg["camera_id"])
        self.interval = BATTERY_EVERY if self.on_battery else WIRED_EVERY
        self.read_now(cfg, refs)

    def read_now(self, cfg=None, refs=None):
        cfg = cfg or self.config()
        refs = refs or self.refs()
        sample = patch_of(self._snapshot(cfg["camera_id"]), cfg["box"])
        state, d = classify(sample, refs)
        now = self.clock()
        with self._lock:
            self.reading = {"state": state, "checked_at": now, "distances": d}
            if state != "unknown":
                changed = self.last_sure is None or self.last_sure["state"] != state
                if changed:
                    self.since = now
                self.last_sure = {"state": state, "at": now}
            else:
                changed = False
        if changed:
            self.bus.publish({"source": "garage", "kind": f"garage-{state}", "state": state})
        return state

    def _on_event(self, event):
        cfg = self.config()
        cam = (event.get("camera") or {}).get("id")
        if event.get("source") != "ring" or event.get("kind") != "motion" or not cam or cam != cfg.get("camera_id"):
            return
        delay = BATTERY_AFTER_MOTION if self.on_battery else WIRED_AFTER_MOTION
        t = self.timer(delay, self._read_quietly)
        t.daemon = True
        t.start()

    def _read_quietly(self):
        try:
            if self.config().get("camera_id") and len(self.refs()) == 2:
                self.read_now()
        except Exception as e:
            self.log(f"Garage: {e}")

    # --- what the page and the feed show ---

    def shown(self):
        """What the page shows: the state, or "can't tell" / "last seen" with the last sure one."""
        now = self.clock()
        r, sure = self.reading, self.last_sure
        if not self.config().get("camera_id"):
            return {"shown": "not set up"}
        if r["state"] in ("open", "closed") and r["checked_at"] and now - r["checked_at"] <= STALE_AFTER:
            return {"shown": r["state"], "since": self.since}
        if sure and now - sure["at"] > STALE_AFTER:
            return {"shown": "last seen", "state": sure["state"], "at": sure["at"]}
        return {"shown": "can't tell", "state": sure and sure["state"], "at": sure and sure["at"]}

    def state(self):
        return dict(self.shown(), on_battery=self.on_battery, checked_at=self.reading["checked_at"],
                    every_seconds=self.interval, distances=self.reading["distances"],
                    references={k: len(v) for k, v in self.refs().items()})

    def health_text(self):
        s = self.shown()
        if s["shown"] == "last seen":
            return f"last seen {s['state']}"
        return s["shown"]
