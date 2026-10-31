"""Rules: "when, only if, then, until" (V2 plan, section 4).

A rule is data, kept in data/settings.json under "rules":

    {
      "id": "a1b2c3d4", "name": "Red Alert", "enabled": true,
      "when":    {"kind": "motion", "camera": "any"},
      "only_if": {"modes": ["AWAY", "NIGHT"], "between": ["22:00", "06:00"], "cooldown_s": 300},
      "then":    [{"do": "lights", "target": "all", "pattern": "pulse", "params": {"color": "#FF0000"}},
                  {"do": "camera_siren", "cameras": "all", "seconds": 30},
                  {"do": "snapshot", "cameras": "all"},
                  {"do": "alert", "text": "Motion while away", "stop_button": true}],
      "until":   {"stop": true, "minutes": 5, "mode_is": ["HOME"]}
    }

Triggers ("when"): motion, doorbell, sensor-open, alarm (the Ring Alarm going off), garage (open or
closed), house-mode, time, sunrise, sunset, button. Conditions ("only if"): house modes, a time
window (it may cross midnight) and a cool-down. Actions ("then"): lights (a pattern, a scene, a
color, on or off), camera_siren, sonos, sonos_sound (phase 2), snapshot, firetv, mode (arming
only, and only once Anthony allows arming from the app), alert. A run lasts ("until") until STOP,
a time limit, or a house mode; while it runs, the same rule doesn't start again.

The safe test runs only the lights and the phone alert, and ends by itself after 20 seconds.

Safety: a rule never disarms the alarm (modes.py refuses), never sounds the base station siren
(there is no action for it), and a camera siren always stops after its time (CameraSirens).
"""

import secrets as pysecrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from .scheduler import due

TRIGGERS = ("motion", "doorbell", "sensor-open", "alarm", "garage", "house-mode",
            "time", "sunrise", "sunset", "button")
ACTIONS = ("lights", "camera_siren", "sonos", "sonos_sound", "snapshot", "firetv", "mode", "alert")
TEST_ACTIONS = ("lights", "alert")
TEST_SECONDS = 20
MAX_MINUTES = 60
RUNS_KEPT = 100
CLOCK_TRIGGERS = ("time", "sunrise", "sunset")


class NotSetUp(Exception):
    pass


class NoNotifier:
    """Push alerts wait for Anthony's choice of service (ntfy or Pushover)."""

    def send(self, title, text, stop_url=None):
        raise NotSetUp("push alerts aren't set up yet (Anthony chooses the service)")


def _hm(text):
    hh, mm = (int(x) for x in str(text).split(":")[:2])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError
    return hh * 60 + mm


def in_window(between, now):
    """True when now (a datetime) falls in ["HH:MM", "HH:MM"]; the window may cross midnight."""
    start, end = (_hm(x) for x in between)
    m = now.hour * 60 + now.minute
    return start <= m < end if start <= end else (m >= start or m < end)


def validate(rule):
    """A cleaned copy of the rule, or ValueError with a plain reason."""
    if not isinstance(rule, dict):
        raise ValueError("a rule is an object")
    name = str(rule.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("a rule needs a name")
    when = dict(rule.get("when") or {})
    if when.get("kind") not in TRIGGERS:
        raise ValueError(f"'when' must be one of: {', '.join(TRIGGERS)}")
    if when["kind"] == "time":
        try:
            _hm(when.get("time"))
        except (ValueError, TypeError):
            raise ValueError("a time trigger needs a time like 22:30") from None
    if when["kind"] == "garage" and when.get("state") not in ("open", "closed"):
        raise ValueError("a garage trigger needs state open or closed")
    only_if = dict(rule.get("only_if") or {})
    if only_if.get("between"):
        try:
            between = list(only_if["between"])
            in_window(between, datetime.now())
            only_if["between"] = between[:2]
        except (ValueError, TypeError):
            raise ValueError("'between' needs two times like [\"22:00\", \"06:00\"]") from None
    from .modes import MODES
    for m in only_if.get("modes") or []:
        if m not in MODES:
            raise ValueError(f"unknown house mode {m!r}")
    then = list(rule.get("then") or [])
    if not then:
        raise ValueError("a rule needs at least one action")
    for step in then:
        if not isinstance(step, dict) or step.get("do") not in ACTIONS:
            raise ValueError(f"each action's 'do' must be one of: {', '.join(ACTIONS)}")
        if step["do"] == "mode" and str(step.get("mode", "")).upper() == "HOME":
            raise ValueError("a rule may never disarm the alarm")
    until = dict(rule.get("until") or {})
    minutes = until.get("minutes")
    if minutes is not None and not (0 < float(minutes) <= MAX_MINUTES):
        raise ValueError(f"'until' minutes must be between 1 and {MAX_MINUTES}")
    return {"id": str(rule.get("id") or uuid.uuid4().hex[:8]), "name": name,
            "enabled": bool(rule.get("enabled", True)), "when": when, "only_if": only_if,
            "then": then, "until": until}


def red_alert_template():
    """Anthony's example, as a starting point on the Alerts screen (off until he turns it on)."""
    return validate({
        "name": "Red Alert", "enabled": False,
        "when": {"kind": "motion", "camera": "any"},
        "only_if": {"modes": ["AWAY", "NIGHT", "VACATION"], "cooldown_s": 600},
        "then": [
            {"do": "lights", "target": "all", "pattern": "pulse", "params": {"color": "#FF0000", "rate": 1.5}},
            {"do": "camera_siren", "cameras": "all", "seconds": 30},
            {"do": "sonos_sound", "sound": "alarm"},
            {"do": "snapshot", "cameras": "all"},
            {"do": "alert", "text": "Motion at home while the alarm is set", "stop_button": True},
        ],
        "until": {"stop": True, "minutes": 5, "mode_is": ["HOME"]},
    })


class Run:
    def __init__(self, rule, event, test, started):
        self.id = uuid.uuid4().hex[:10]
        self.rule = rule
        self.event = {k: v for k, v in (event or {}).items() if k in ("kind", "source", "at", "mode", "state")}
        self.test = test
        self.started = started
        self.ended = None
        self.end_reason = None
        self.steps = []
        self.stop_token = pysecrets.token_urlsafe(24)
        self.cleanups = []
        self.timer = None

    def to_dict(self):
        return {"id": self.id, "rule_id": self.rule["id"], "rule": self.rule["name"], "test": self.test,
                "trigger": self.event, "started": self.started, "ended": self.ended,
                "end_reason": self.end_reason, "running": self.ended is None, "steps": list(self.steps)}


class RulesEngine:
    def __init__(self, store, bus, controller, services=None, sirens=None, ring=None, modes=None,
                 notifier=None, stop_url=None, clock=time.time, now=lambda: datetime.now().astimezone(),
                 timer=threading.Timer, tick_every=20, log=print):
        self.store = store
        self.bus = bus
        self.controller = controller
        self.services = services or {}
        self.sirens = sirens
        self.ring = ring
        self.modes = modes
        self.notifier = notifier or NoNotifier()
        self.stop_url = stop_url  # function(token) -> URL the phone's STOP button calls, or None
        self.clock = clock
        self.now = now
        self.timer = timer
        self.tick_every = tick_every
        self.log = log
        self.runs = deque(maxlen=RUNS_KEPT)
        self.snapshots = {}          # camera id -> (when, content type, bytes): the latest only
        self._active = {}            # rule id -> Run
        self._last_fired = {}        # rule id -> epoch seconds (cool-down)
        self._fired_on = {}          # rule id -> date for clock triggers
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        bus.subscribe(self.on_event)

    # --- rules as data ---

    def rules(self):
        return list(self.store.get("rules"))

    def put(self, rule):
        rule = validate(rule)
        rules = [r for r in self.rules() if r["id"] != rule["id"]] + [rule]
        self.store.put("rules", rules)
        return rule

    def delete(self, rule_id):
        rules = [r for r in self.rules() if r["id"] != rule_id]
        if len(rules) == len(self.rules()):
            raise KeyError(f"no rule {rule_id!r}")
        self.stop_rule(rule_id, "rule deleted")
        self.store.put("rules", rules)

    def rule(self, rule_id):
        r = next((r for r in self.rules() if r["id"] == rule_id), None)
        if r is None:
            raise KeyError(f"no rule {rule_id!r}")
        return r

    # --- triggers ---

    @staticmethod
    def matches(when, event):
        """Does this event fire this trigger?"""
        kind, src, ek = when.get("kind"), event.get("source"), event.get("kind")
        if src == "ring" and kind in ("motion", "doorbell") and ek == kind:
            want = when.get("camera", "any")
            return want in (None, "any") or want == (event.get("camera") or {}).get("id")
        if src == "ring" and kind == "sensor-open" and ek == "sensor-open":
            want = when.get("sensor", "any")
            return want in (None, "any") or want == (event.get("sensor") or {}).get("id")
        if src == "ring" and kind == "alarm" and ek == "alarm":
            return True
        if src == "garage" and kind == "garage":
            return ek == f"garage-{when.get('state')}"
        if src == "house" and kind == "house-mode" and ek == "house-mode":
            return when.get("mode") in (None, "any", event.get("mode"))
        return False

    def on_event(self, event):
        if event.get("source") == "house" and event.get("kind") == "house-mode":
            self._end_on_mode(event.get("mode"))
        if event.get("source") not in ("ring", "garage", "house"):
            return
        for rule in self.rules():
            if rule.get("enabled") and self.matches(rule["when"], event):
                self.fire(rule, event)

    def tick(self, now=None):
        """Clock triggers (time, sunrise, sunset); run every few seconds by the engine's thread."""
        now = now or self.now()
        settings = self.store.get("settings")
        for rule in self.rules():
            when = rule["when"]
            if not rule.get("enabled") or when["kind"] not in CLOCK_TRIGGERS:
                continue
            entry = {"enabled": True, "days": when.get("days") or [],
                     "when": {"type": when["kind"], "time": when.get("time"), "offset": when.get("offset", 0)},
                     "last_fired": self._fired_on.get(rule["id"])}
            if due(entry, settings, now):
                self._fired_on[rule["id"]] = now.date().isoformat()
                self.fire(rule, {"source": "clock", "kind": when["kind"]})

    def press(self, rule_id):
        rule = self.rule(rule_id)
        if rule["when"]["kind"] != "button":
            raise ValueError("this rule isn't started by a button")
        return self.fire(rule, {"source": "page", "kind": "button"}, check=True)

    # --- conditions ---

    def why_not(self, rule):
        """The reason a rule should not run now, or None."""
        only = rule.get("only_if") or {}
        if only.get("modes"):
            mode = self.modes.mode if self.modes else None
            if mode not in only["modes"]:
                return f"house mode is {mode or 'unknown'}"
        if only.get("between") and not in_window(only["between"], self.now()):
            return "outside its time window"
        cooldown = float(only.get("cooldown_s") or 0)
        last = self._last_fired.get(rule["id"])
        if cooldown and last is not None and self.clock() - last < cooldown:
            return "cooling down"
        return None

    # --- running ---

    def fire(self, rule, event, test=False, check=True):
        with self._lock:
            if rule["id"] in self._active:
                return None
            if check and not test:
                reason = self.why_not(rule)
                if reason:
                    return None
            run = Run(rule, event, test, self.clock())
            self._active[rule["id"]] = run
            self.runs.appendleft(run)
            if not test:
                self._last_fired[rule["id"]] = self.clock()
        self.bus.publish({"source": "rules", "kind": "rule-started", "rule": rule["name"], "test": test})
        for step in rule["then"]:
            self._do(run, step)
        until = rule.get("until") or {}
        seconds = TEST_SECONDS if test else 60 * float(until.get("minutes") or 0)
        if seconds:
            run.timer = self.timer(seconds, self.end, args=(run, "time limit"))
            run.timer.daemon = True
            run.timer.start()
        elif not until.get("stop") and not until.get("mode_is"):
            self.end(run, "done")  # nothing keeps it going
        return run

    def _note(self, run, step, ok, note=None):
        run.steps.append({"do": step["do"], "ok": ok, "note": note})

    def _do(self, run, step):
        kind = step["do"]
        if run.test and kind not in TEST_ACTIONS:
            self._note(run, step, None, "skipped in the safe test")
            return
        try:
            note = getattr(self, f"_act_{kind}")(run, step)
            self._note(run, step, True, note)
        except NotSetUp as e:
            self._note(run, step, None, str(e))
        except Exception as e:  # one failed action never stops the rest
            self._note(run, step, False, str(e) or type(e).__name__)
            self.log(f"Rule {run.rule['name']!r}: {kind} failed: {e}")

    def _lights_targets(self, target):
        return ["all" if target in (None, "") else target]

    def _act_lights(self, run, step):
        targets = self._lights_targets(step.get("target"))
        before = {}
        for t in targets:
            members, _ = self.controller.resolve(t)
            for d in members:
                on = self.controller.state.get(d.id, {}).get("on")
                if on is not None:
                    before[d.id] = on
        for t in targets:
            if step.get("pattern"):
                self.controller.start_effect(t, step["pattern"], step.get("params"), step.get("fps"))
            elif step.get("scene"):
                self.controller.scene(t, step["scene"])
            elif step.get("color"):
                self.controller.color(t, step["color"])
            elif "on" in step:
                self.controller.power(t, bool(step["on"]))
            else:
                raise ValueError("a lights action needs a pattern, scene, color or on")

        def undo():
            for t in targets:
                self.controller.stop(t)
            for dev_id, on in before.items():
                try:
                    self.controller.power(f"device:{dev_id}", on)
                except Exception:
                    pass
        if step.get("pattern"):
            run.cleanups.append(undo)
        return f"{len(targets)} light target(s)"

    def _cameras(self, which, need_siren=False):
        if self.ring is None:
            raise NotSetUp("Ring isn't set up")
        cams = self.ring.get_json("/devices").get("cameras", [])
        if need_siren:
            cams = [c for c in cams if c.get("hasSiren")]
        if which not in (None, "", "all"):
            wanted = set(which if isinstance(which, list) else [which])
            cams = [c for c in cams if c.get("id") in wanted]
        return [c["id"] for c in cams if not c.get("offline")]

    def _act_camera_siren(self, run, step):
        if self.sirens is None:
            raise NotSetUp("Ring isn't set up")
        cams = self._cameras(step.get("cameras", "all"), need_siren=True)
        seconds = step.get("seconds") or self.store.get("settings").get("camera_siren_seconds") or 30
        for cid in cams:
            self.sirens.start(cid, seconds)
            run.cleanups.append(lambda cid=cid: self.sirens.stop(cid))
        return f"{len(cams)} siren(s) for {int(seconds)} s" if cams else "no camera with a siren"

    def _act_snapshot(self, run, step):
        taken = 0
        for cid in self._cameras(step.get("cameras", "all")):
            try:
                ctype, data = self.ring.request("GET", f"/cameras/{cid}/snapshot")
            except Exception:
                continue
            self.snapshots[cid] = (self.clock(), ctype, data)
            taken += 1
        return f"{taken} snapshot(s)"

    def _service(self, key):
        svc = self.services.get(key)
        if svc is None:
            raise NotSetUp(f"{key} isn't set up")
        return svc

    def _act_sonos(self, run, step):
        self._service("sonos").act(step.get("target"), step.get("action", "play"), step.get("args") or {})

    def _act_sonos_sound(self, run, step):
        raise NotSetUp("Sonos sounds come in phase 2 (the LAN sound port, by its own card)")

    def _act_firetv(self, run, step):
        self._service("firetv").act(step.get("target"), step.get("action", "home"), step.get("args") or {})

    def _act_mode(self, run, step):
        if self.modes is None:
            raise NotSetUp("the Ring Alarm isn't set up")
        self.modes.change(step.get("mode"), from_rule=True)

    def _act_alert(self, run, step):
        title = "Test: " + run.rule["name"] if run.test else run.rule["name"]
        url = self.stop_url(run.stop_token) if (step.get("stop_button") and self.stop_url) else None
        self.notifier.send(title, str(step.get("text") or run.rule["name"])[:200], stop_url=url)

    # --- ending ---

    def end(self, run, reason):
        with self._lock:
            if run.ended is not None:
                return False
            run.ended, run.end_reason = self.clock(), reason
            if self._active.get(run.rule["id"]) is run:
                del self._active[run.rule["id"]]
        if run.timer is not None:
            try:
                run.timer.cancel()
            except Exception:
                pass
        for undo in reversed(run.cleanups):
            try:
                undo()
            except Exception as e:
                self.log(f"Rule {run.rule['name']!r}: clean-up failed: {e}")
        self.bus.publish({"source": "rules", "kind": "rule-ended", "rule": run.rule["name"], "reason": reason})
        return True

    def stop_run(self, run_id, reason="STOP"):
        run = next((r for r in self.runs if r.id == run_id), None)
        if run is None:
            raise KeyError(f"no run {run_id!r}")
        return self.end(run, reason)

    def stop_by_token(self, token):
        """The phone's STOP button: the token alone, compared in constant time."""
        for run in list(self._active.values()):
            if pysecrets.compare_digest(run.stop_token, str(token)):
                return self.end(run, "STOP from the phone")
        return False

    def stop_rule(self, rule_id, reason="STOP"):
        run = self._active.get(rule_id)
        return self.end(run, reason) if run else False

    def stop_all(self, reason="STOP"):
        for run in list(self._active.values()):
            self.end(run, reason)

    def _end_on_mode(self, mode):
        for run in list(self._active.values()):
            if mode in ((run.rule.get("until") or {}).get("mode_is") or []):
                self.end(run, f"house mode became {mode}")

    def test(self, rule_id):
        return self.fire(self.rule(rule_id), {"source": "page", "kind": "test"}, test=True)

    # --- the clock thread ---

    def _loop(self):
        while not self._stop.wait(self.tick_every):
            try:
                self.tick()
            except Exception as e:
                self.log(f"Rules: clock check failed: {e}")

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rules")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.stop_all("app stopping")

    def state(self):
        return {"rules": self.rules(), "runs": [r.to_dict() for r in list(self.runs)[:30]],
                "triggers": list(TRIGGERS), "actions": list(ACTIONS), "template": red_alert_template()}
