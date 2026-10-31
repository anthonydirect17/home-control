"""House modes: HOME, NIGHT, AWAY, VACATION, following the Ring Alarm.

    HOME      Ring Alarm disarmed      (mode "none")
    NIGHT     Ring Alarm armed home    (mode "some")
    AWAY      Ring Alarm armed away    (mode "all")
    VACATION  Ring Alarm armed away    (mode "all"), plus this app's own vacation flag

The Ring Alarm is the truth: whatever arms or disarms it (the keypad, the Ring app, this
app) changes the house mode here, and a "house-mode" event goes on the bus for the rules.
VACATION is AWAY with a flag kept here, so the rules can tell them apart.

Arming and disarming from this app is Anthony's decision (V2 plan, section 3.5). Until he
decides, the "allow_app_arming" setting stays off and change() refuses; only the vacation
flag can be set, and only while the alarm is armed away. A rule may never disarm.
"""

import threading

MODES = ("HOME", "NIGHT", "AWAY", "VACATION")
RING_MODE = {"HOME": "none", "NIGHT": "some", "AWAY": "all", "VACATION": "all"}


class ModeRefused(Exception):
    """A plain reason the mode can't be changed from here."""


def house_mode(ring_mode, vacation=False):
    if ring_mode == "none":
        return "HOME"
    if ring_mode == "some":
        return "NIGHT"
    if ring_mode == "all":
        return "VACATION" if vacation else "AWAY"
    return None


class HouseModes:
    def __init__(self, store, bus, ring=None):
        self.store = store
        self.bus = bus
        self.ring = ring
        self.mode = None           # None until the Ring Alarm has been read
        self.location_id = None
        self._lock = threading.Lock()
        bus.subscribe(self._on_event)

    # --- reading ---

    def _vacation(self):
        return bool(self.store.get("settings").get("vacation"))

    def refresh(self):
        """Read the alarm's mode from Ring (call at start and after a reconnect)."""
        if self.ring is None:
            return self.mode
        try:
            devices = self.ring.get_json("/devices")
        except Exception:
            return self.mode
        alarm = next((loc for loc in devices.get("locations", []) if loc.get("hasAlarm")), None)
        if alarm is not None:
            self.location_id = alarm["id"]
            self._set(alarm.get("mode"), source="ring")
        return self.mode

    def _on_event(self, event):
        if event.get("source") == "ring" and event.get("kind") == "alarm-mode":
            loc = event.get("location") or {}
            if self.location_id in (None, loc.get("id")):
                self.location_id = loc.get("id") or self.location_id
                self._set(event.get("mode"), source="ring")

    def _set(self, ring_mode, source):
        if ring_mode != "all" and self._vacation():
            self.store.update_settings({"vacation": False})  # disarming or arming home ends a vacation
        new = house_mode(ring_mode, self._vacation())
        with self._lock:
            old, self.mode = self.mode, new
        if new is not None and new != old:
            self.bus.publish({"source": "house", "kind": "house-mode", "mode": new, "was": old, "by": source})

    def state(self):
        return {"mode": self.mode, "modes": list(MODES),
                "app_arming": bool(self.store.get("settings").get("allow_app_arming"))}

    # --- changing ---

    def change(self, mode, from_rule=False):
        """Change the house mode. Refuses unless Anthony has allowed arming from the app."""
        mode = str(mode).upper()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if self.mode is None:
            raise ModeRefused("the Ring Alarm's mode isn't known yet")
        if from_rule and mode == "HOME" and self.mode != "HOME":
            raise ModeRefused("a rule may never disarm the alarm")
        same_alarm = RING_MODE[mode] == RING_MODE[self.mode]
        if same_alarm:  # only the vacation flag changes: kept here, no call to Ring
            if mode in ("AWAY", "VACATION"):
                self.store.update_settings({"vacation": mode == "VACATION"})
                self._set("all", source="app")
            return self.mode
        if not self.store.get("settings").get("allow_app_arming"):
            raise ModeRefused("arming and disarming from this app is switched off (Anthony's decision)")
        if self.ring is None or self.location_id is None:
            raise ModeRefused("the Ring Alarm isn't connected")
        self.ring.post(f"/locations/{self.location_id}/mode", {"mode": RING_MODE[mode]})
        self.store.update_settings({"vacation": mode == "VACATION"})
        self._set(RING_MODE[mode], source="rule" if from_rule else "app")
        return self.mode
