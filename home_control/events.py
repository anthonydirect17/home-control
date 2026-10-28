"""A small in-memory event bus.

Ring events (motion, doorbell, alarm, sensors) are published here. The page
shows the recent ones, the health endpoint uses the latest time, and the
rules engine (phase 3) will subscribe. Nothing is written to disk.
"""

import threading
from collections import deque
from datetime import datetime, timezone

KEEP = 200


class EventBus:
    def __init__(self, keep=KEEP):
        self._recent = deque(maxlen=keep)
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self, fn):
        with self._lock:
            self._subscribers.append(fn)

    def publish(self, event: dict):
        event = dict(event)
        event.setdefault("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self._lock:
            self._recent.append(event)
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:  # one bad subscriber must not stop the others
                pass
        return event

    def recent(self, source=None, limit=50):
        with self._lock:
            items = [e for e in self._recent if source is None or e.get("source") == source]
        return list(reversed(items))[:limit]
