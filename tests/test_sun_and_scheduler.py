from datetime import date, datetime, timedelta, timezone

from home_control import scheduler
from home_control.sun import sun_times

NEW_YORK = (40.7128, -74.0060)


def test_new_york_winter_solstice():
    rise, set_ = sun_times(date(2026, 12, 21), *NEW_YORK)
    # Published times: about 7:16 and 16:32 EST (12:16 and 21:32 UTC).
    assert abs(rise - datetime(2026, 12, 21, 12, 16, tzinfo=timezone.utc)) < timedelta(minutes=3)
    assert abs(set_ - datetime(2026, 12, 21, 21, 32, tzinfo=timezone.utc)) < timedelta(minutes=3)


def test_polar_night_has_no_sunrise():
    assert sun_times(date(2026, 12, 21), 80.0, 15.0) == (None, None)


def _entry(**kw):
    e = {"id": "t1", "name": "test", "enabled": True, "days": [], "target": "device:A",
         "when": {"type": "time", "time": "18:30"}, "action": {"type": "on"}}
    e.update(kw)
    return e


def test_due_inside_grace_window_only():
    now = datetime(2026, 10, 26, 18, 31).astimezone()
    assert scheduler.due(_entry(), {}, now) is not None
    assert scheduler.due(_entry(), {}, now - timedelta(minutes=5)) is None   # before
    assert scheduler.due(_entry(), {}, now + timedelta(minutes=30)) is None  # too late


def test_not_due_twice_or_when_disabled_or_wrong_day():
    now = datetime(2026, 10, 26, 18, 31).astimezone()  # a Monday
    assert scheduler.due(_entry(last_fired="2026-10-26"), {}, now) is None
    assert scheduler.due(_entry(enabled=False), {}, now) is None
    assert scheduler.due(_entry(days=[5, 6]), {}, now) is None
    assert scheduler.due(_entry(days=[0]), {}, now) is not None


def test_sunset_needs_location_and_applies_offset():
    entry = _entry(when={"type": "sunset", "offset": -15})
    day = date(2026, 12, 21)
    assert scheduler.fire_time(entry, day, {}) is None
    settings = {"latitude": NEW_YORK[0], "longitude": NEW_YORK[1]}
    t = scheduler.fire_time(entry, day, settings)
    _, sunset = sun_times(day, *NEW_YORK)
    assert t == sunset.astimezone() - timedelta(minutes=15)


def test_next_run_finds_tomorrow_after_todays_time_passed():
    now = datetime(2026, 10, 26, 20, 0).astimezone()
    nxt = scheduler.next_run(_entry(), {}, now)
    assert nxt.date() == date(2026, 10, 27) and (nxt.hour, nxt.minute) == (18, 30)
