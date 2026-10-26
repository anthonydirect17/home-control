"""Timers. Checks every few seconds and runs whatever is due.

A schedule entry looks like:
  {
    "id": "a1b2c3d4",
    "name": "Porch on at sunset",
    "enabled": true,
    "days": [0, 1, 2, 3, 4, 5, 6],          # Monday = 0; empty means every day
    "when": {"type": "sunset", "offset": -15},   # or {"type": "time", "time": "18:30"}
    "target": "group:Outside",
    "action": {"type": "on"}                 # see Controller.run_action
  }

Timers only run while the app is running. If the PC was asleep at the
scheduled time, the entry still runs if the app wakes up within GRACE.
"""

import threading
from datetime import datetime, timedelta

from .sun import sun_times

GRACE = timedelta(minutes=10)


def fire_time(entry, day, settings):
    """When the entry should fire on the given date (local, aware), or None."""
    days = entry.get("days") or list(range(7))
    if day.weekday() not in days:
        return None
    when = entry.get("when") or {}
    kind = when.get("type", "time")
    if kind == "time":
        hh, mm = (int(x) for x in str(when.get("time", "00:00")).split(":")[:2])
        # A naive datetime's astimezone() uses the PC's own zone rules for that
        # date, so daylight-saving changes are handled.
        return datetime(day.year, day.month, day.day, hh, mm).astimezone()
    if kind in ("sunrise", "sunset"):
        lat, lon = settings.get("latitude"), settings.get("longitude")
        if lat is None or lon is None:
            return None
        rise, set_ = sun_times(day, float(lat), float(lon))
        base = rise if kind == "sunrise" else set_
        if base is None:
            return None
        return base.astimezone() + timedelta(minutes=float(when.get("offset", 0)))
    return None


def next_run(entry, settings, now=None):
    now = now or datetime.now().astimezone()
    for i in range(8):
        t = fire_time(entry, (now + timedelta(days=i)).date(), settings)
        if t and t > now:
            return t
    return None


def due(entry, settings, now):
    if not entry.get("enabled", True):
        return None
    t = fire_time(entry, now.date(), settings)
    if t is None or not (t <= now < t + GRACE):
        return None
    if entry.get("last_fired") == now.date().isoformat():
        return None
    return t


class Scheduler:
    def __init__(self, controller, store, interval=15):
        self.controller = controller
        self.store = store
        self.interval = interval
        self.log = []  # recent runs, newest last
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def tick(self, now=None):
        now = now or datetime.now().astimezone()
        settings = self.store.get("settings")
        for entry in self.store.get("schedules"):
            if due(entry, settings, now) is None:
                continue
            self.store.mark_fired(entry["id"], now.date().isoformat())
            try:
                errors = self.controller.run_action(entry["target"], entry["action"])
                result = "OK" if not errors else "; ".join(f"{k}: {v}" for k, v in errors.items())
            except (KeyError, ValueError) as e:
                result = f"failed: {e}"
            self.log.append({"at": now.isoformat(timespec="seconds"), "name": entry.get("name", entry["id"]),
                             "result": result})
            del self.log[:-50]

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # keep timers alive no matter what
                self.log.append({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                 "name": "scheduler", "result": f"error: {e}"})
