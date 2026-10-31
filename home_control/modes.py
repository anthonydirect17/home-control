"""House modes: HOME, NIGHT, AWAY, VACATION, following the Ring Alarm.

    HOME      Ring Alarm disarmed      (mode "none")
    NIGHT     Ring Alarm armed home    (mode "some")
    AWAY      Ring Alarm armed away    (mode "all")
    VACATION  Ring Alarm armed away    (mode "all"), plus this app's own vacation flag

The Ring Alarm is the truth: whatever arms or disarms it (the keypad, the Ring app, this
app) changes the house mode here, and a "house-mode" event goes on the bus for the rules.
VACATION is AWAY with a flag kept here, so the rules can tell them apart.

Arming and disarming from this app (Anthony, 2026-10-31): yes, with the PIN. The
"allow_app_arming" switch starts off; Anthony turns it on in the app with his PIN. While it
is off, change() refuses everything but the vacation flag (which needs the alarm armed
away). Once on, the page may arm and disarm with the PIN every time, and a rule saved with
the PIN may too (rules.py).

Each house-mode event says who made the change ("by": ring, app or rule). When Ring echoes a
change this app has just asked for, the event keeps the app's or rule's name, so the rules'
loop guard (a rule's own change never fires another rule) holds.
"""

import threading
import time

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
        self._expect = None        # (ring mode, who asked, until when): the echo of our own change
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
        if event.get("source") == "ring" and event.get("kind") == "ready":
            # Read the alarm again after every (re)connect: a change made while Ring was away
            # (the keypad, the Ring app) sends no event. Off the helper's reader thread.
            threading.Thread(target=self.refresh, daemon=True, name="mode-refresh").start()
            return
        if event.get("source") == "ring" and event.get("kind") == "alarm-mode":
            loc = event.get("location") or {}
            if self.location_id in (None, loc.get("id")):
                self.location_id = loc.get("id") or self.location_id
                self._set(event.get("mode"), source=self._who(event.get("mode")))

    def _who(self, ring_mode):
        """Who made this change: the app or a rule that just asked for it, or else Ring itself."""
        expect = self._expect
        if expect and expect[0] == ring_mode and time.monotonic() < expect[2]:
            return expect[1]
        return "ring"

    def _set(self, ring_mode, source):
        if ring_mode not in RING_MODE.values():  # unknown (panel data missing): keep what we knew
            return
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

    def change(self, mode, from_rule=False, confirm=None):
        """Change the house mode. Refuses unless Anthony has allowed arming from the app.

        confirm: called (and may raise) just before the alarm itself would change, so the page's
        PIN check is decided on the same reading of the mode as the change.
        """
        mode = str(mode).upper()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if self.mode is None:
            raise ModeRefused("the Ring Alarm's mode isn't known yet")
        source = "rule" if from_rule else "app"
        same_alarm = RING_MODE[mode] == RING_MODE[self.mode]
        if same_alarm:  # only the vacation flag changes: kept here, no call to Ring
            if mode in ("AWAY", "VACATION"):
                self.store.update_settings({"vacation": mode == "VACATION"})
                self._set("all", source=source)
            return self.mode
        if not self.store.get("settings").get("allow_app_arming"):
            raise ModeRefused("arming and disarming from this app is switched off; turn it on in the app "
                              "with your PIN")
        if confirm is not None:
            confirm()
        if self.ring is None or self.location_id is None:
            raise ModeRefused("the Ring Alarm isn't connected")
        self.store.update_settings({"vacation": mode == "VACATION"})
        self._expect = (RING_MODE[mode], source, time.monotonic() + 30)
        self.ring.post(f"/locations/{self.location_id}/mode", {"mode": RING_MODE[mode]})
        self._set(RING_MODE[mode], source=source)
        return self.mode
