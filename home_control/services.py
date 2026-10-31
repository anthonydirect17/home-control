"""The shared shape of V2's device services (Sonos, Fire TV, PC status, and those to come).

Each service keeps its own state up to date in a background thread, so the page
and the health feed read it at once and never wait on a device. It has:

    key, label                  stable id and display name
    status                      {"label", "up", "error", "note", "checked_at"} (no addresses or names)
    start(), stop()
    state()                     what the page shows (may hold device names; behind the PIN)
    health_text()               a few words for the health feed: counts and states only, never names
    act(target, action, args)   one action, e.g. ("soundbar", "volume", {"level": 30})

A device that stops answering marks the service down with a plain error, and the
loop keeps trying with a growing pause. Nothing here ever raises out of the loop.
"""

import threading
import time


class ServiceError(Exception):
    """A message fit for the page: no addresses, no secrets."""


class PollingService:
    key = "service"
    label = "Service"
    interval = 10        # seconds between polls while things work
    max_backoff = 300    # longest pause after repeated failures

    def __init__(self, log=print, clock=time.time):
        self.log = log
        self.clock = clock
        self.status = {"label": self.label, "up": False, "error": None, "note": None, "checked_at": None}
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0

    # --- for subclasses ---

    def poll(self):
        """Refresh state from the devices. Raise ServiceError (or any error) when they can't be reached."""
        raise NotImplementedError

    def state(self):
        return {}

    def health_text(self):
        return None

    def act(self, target, action, args=None):
        raise ServiceError(f"{self.label} has no action {action!r}")

    # --- the loop ---

    def poll_once(self):
        """One poll; updates status. Returns True when it worked."""
        try:
            self.poll()
        except Exception as e:  # a device problem must never end the loop
            self._failures += 1
            message = str(e) if isinstance(e, ServiceError) else f"{type(e).__name__}: {e}"
            if self.status["error"] != message:
                self.log(f"{self.label}: {message}")
            self.status.update(up=False, error=message, checked_at=self.clock())
            return False
        if self._failures or not self.status["up"]:
            if self._failures:
                self.log(f"{self.label}: answering again")
        self._failures = 0
        self.status.update(up=True, error=None, checked_at=self.clock())
        return True

    def next_delay(self):
        if not self._failures:
            return self.interval
        return min(self.max_backoff, self.interval * (2 ** min(self._failures - 1, 6)))

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.next_delay())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"svc-{self.key}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
