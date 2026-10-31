"""The Fusion kit's health feed: one small route and one shape for every app (docs/HEALTH_FEED.md).

    GET /api/health  ->  {
      "schema": "fusion-health/1",
      "app": "home-control",            stable id: lowercase letters, digits and hyphens
      "name": "Home Control",           what a tile shows
      "version": "0.6.0",
      "status": "ok" | "warn" | "down",
      "summary": "3 of 5 lights on; all devices answering",     one line, at most 120 characters
      "values": [{"label": "Lights", "value": "3 of 5 on", "status": "ok"}, ...],   at most 6
      "checked_at": "2026-10-31T21:47:00+00:00",
      "started_at": "...", "uptime_seconds": 1234
    }

The Bridge's tiles and the Watcher read only these fields. An app may add its own fields
beside them; readers ignore what they don't know. The feed is open (no sign-in) and is
built from state the app already holds, so it answers at once. It never holds an address,
an account or device name, or a secret: check_feed() refuses one that does.

This file is meant to move into the shared kit unchanged, so it imports nothing from the app.
"""

import re
from datetime import datetime, timezone

SCHEMA = "fusion-health/1"
STATUSES = ("ok", "warn", "down")
MAX_SUMMARY = 120
MAX_VALUES = 6
MAX_LABEL = 24
MAX_VALUE = 40
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def worst(statuses):
    """The worst of several statuses: down beats warn beats ok. None of them is ok."""
    found = [s for s in statuses if s in STATUSES]
    return max(found, key=STATUSES.index) if found else "ok"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _looks_private(text):
    if _EMAIL.search(text) or _IPV4.search(text):
        return True
    return any("::" in m or m.count(":") >= 3 for m in _IPV6.findall(text))


def check_feed(feed, private_words=()):
    """Problems with a feed, as a list of plain sentences; empty when it is right.

    private_words: names the app knows must never appear (device names, for example).
    """
    problems = []
    if not isinstance(feed, dict):
        return ["the feed is not a JSON object"]
    if feed.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    if not _APP_ID.match(str(feed.get("app", ""))):
        problems.append("app must be a short lowercase id")
    for field in ("name", "version", "summary", "checked_at"):
        if not isinstance(feed.get(field), str) or not feed.get(field):
            problems.append(f"{field} is missing")
    if feed.get("status") not in STATUSES:
        problems.append(f"status must be one of {', '.join(STATUSES)}")
    if len(str(feed.get("summary", ""))) > MAX_SUMMARY:
        problems.append(f"summary is longer than {MAX_SUMMARY} characters")
    values = feed.get("values")
    if not isinstance(values, list) or len(values) > MAX_VALUES:
        problems.append(f"values must be a list of at most {MAX_VALUES}")
        values = []
    for v in values:
        if not isinstance(v, dict) or not isinstance(v.get("label"), str) or not isinstance(v.get("value"), str):
            problems.append("each value needs a label and a value, both text")
            continue
        if len(v["label"]) > MAX_LABEL or len(v["value"]) > MAX_VALUE:
            problems.append(f"value {v['label'][:MAX_LABEL]!r} is too long")
        if v.get("status") is not None and v["status"] not in STATUSES:
            problems.append(f"value {v['label']!r} has an unknown status")
    words = [feed.get("summary", "")] + [f"{v.get('label', '')} {v.get('value', '')}" for v in values
                                         if isinstance(v, dict)]
    text = " ".join(str(w) for w in words)
    if _looks_private(text):
        problems.append("the summary or values hold an address or an e-mail address")
    lowered = text.lower()
    for word in private_words:
        if word and str(word).lower() in lowered:
            problems.append("the summary or values hold a private name")
            break
    return problems


def make_feed(app, name, version, status, summary, values=(), started_at=None, now=None, **extra):
    """Build a feed; values are (label, value) or (label, value, status). Long text is trimmed."""
    rows = []
    for v in list(values)[:MAX_VALUES]:
        label, value, *rest = v
        row = {"label": str(label)[:MAX_LABEL], "value": str(value)[:MAX_VALUE]}
        if rest and rest[0] in STATUSES:
            row["status"] = rest[0]
        rows.append(row)
    summary = str(summary)
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY - 1] + "…"
    feed = {
        "schema": SCHEMA, "app": app, "name": name, "version": version,
        "status": status if status in STATUSES else "down", "summary": summary, "values": rows,
        "checked_at": iso(now) if now else datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if started_at is not None and now is not None:
        feed["started_at"] = iso(started_at)
        feed["uptime_seconds"] = int(now - started_at)
    feed.update(extra)
    return feed
