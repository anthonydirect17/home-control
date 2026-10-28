"""Ring: cameras, doorbell, Ring Alarm and its sensors.

The Ring side runs in a small Node.js helper (`ring-bridge/`, using
ring-client-api), because that is the library that covers Ring Alarm. This
module starts the helper, keeps it running, and talks to it:
  - start-up settings go to the helper's stdin (the refresh token never
    touches the command line, the environment or a file);
  - the helper reports events and rotated tokens on stdout, one JSON per line;
  - commands go to its HTTP port on 127.0.0.1, with a per-start secret.

Ring rotates the refresh token regularly; every new one is saved at once,
or the next start would fail.

Safety: nothing here or in the helper ever contacts Ring's monitoring
center. Arming, disarming and sirens need the page PIN every time (server.py).
"""

import json
import os
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(ROOT, "ring-bridge")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_PORT = 8782  # inside the app's helper range, 127.0.0.1 only
TOKEN_SECRET = "ring-refresh-token"


class RingError(Exception):
    """A Ring command failed. The message is meant for the page."""


def node_command(node_path="node", script="bridge.mjs"):
    return [node_path or "node", os.path.join(BRIDGE_DIR, script)]


def setup_problem(node_path="node"):
    """Why the helper can't run here, or None if it can."""
    if not shutil.which(node_path or "node"):
        return "Node.js was not found"
    if not os.path.isdir(os.path.join(BRIDGE_DIR, "node_modules", "ring-client-api")):
        return "the Ring helper isn't installed (run: npm ci --ignore-scripts, in ring-bridge)"
    return None


class RingBridge:
    label = "Ring"

    def __init__(self, command, get_token, save_token, bus, port=DEFAULT_PORT, cwd=BRIDGE_DIR,
                 retry_delay=30, max_delay=300, signed_out_delay=60, log=print):
        self.command = command
        self.get_token = get_token
        self.save_token = save_token
        self.bus = bus
        self.port = port
        self.cwd = cwd
        self.retry_delay = retry_delay
        self.max_delay = max_delay
        self.signed_out_delay = signed_out_delay
        self.log = log
        self.status = {"label": self.label, "connected": False, "signed_in": False, "error": None,
                       "cameras": 0, "sensors": 0}
        self._secret = None
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    # running the helper

    def start(self):
        self._thread = threading.Thread(target=self._supervise, daemon=True, name="ring-bridge")
        self._thread.start()

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _supervise(self):
        delay = self.retry_delay
        while not self._stop.is_set():
            started = time.monotonic()
            outcome = self._run_once()
            if self._stop.is_set():
                break
            if outcome == "signed-out":
                self._stop.wait(self.signed_out_delay)  # picks up a --ring-login done meanwhile
                continue
            if time.monotonic() - started > 300:
                delay = self.retry_delay  # it ran fine for a while; start the back-off over
            self.log(f"Ring helper stopped; starting again in {delay} s.")
            self._stop.wait(delay)
            delay = min(self.max_delay, delay * 2)

    def _run_once(self):
        token, _ = self.get_token()
        if not token:
            self.status.update(connected=False, signed_in=False,
                               error="Not signed in to Ring (python -m home_control --ring-login)")
            return "signed-out"
        self.status.update(signed_in=True)
        self._secret = pysecrets.token_hex(16)
        try:
            proc = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                                    creationflags=NO_WINDOW)
        except OSError as e:
            self.status.update(connected=False, error=f"couldn't start the Ring helper: {e}")
            return "failed"
        self._proc = proc
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True, name="ring-stderr").start()
        try:
            proc.stdin.write(json.dumps({"refreshToken": token, "port": self.port, "apiSecret": self._secret}) + "\n")
            proc.stdin.flush()  # stdin stays open: the helper exits when it closes
        except OSError:
            pass
        for line in proc.stdout:
            self._handle_line(line)
        proc.wait()
        self.status.update(connected=False)
        return "exited"

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                self.log(f"Ring helper: {line}")

    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            self.log(f"Ring helper: {line}")
            return
        kind = msg.get("type")
        if kind == "token" and msg.get("refreshToken"):
            try:
                self.save_token(msg["refreshToken"])
            except Exception as e:  # must be loud: a lost token means signing in again
                self.status["error"] = f"couldn't save Ring's new sign-in token: {e}"
                self.log(self.status["error"])
        elif kind == "ready":
            self.status.update(connected=True, error=None, cameras=msg.get("cameras", 0),
                               sensors=msg.get("sensors", 0))
        elif kind == "status":
            self.status["connected"] = bool(msg.get("connected"))
        elif kind == "event":
            event = {k: v for k, v in msg.items() if k != "type"}
            event["source"] = "ring"
            self.bus.publish(event)
        elif kind == "error":
            self.status.update(connected=False, error=f"Ring: {msg.get('message', 'unknown error')}")

    # talking to the helper

    def request(self, method, path, body=None, timeout=20):
        """Returns (content_type, bytes). Raises RingError with a page-ready message."""
        if not self.status["connected"] or not self._secret:
            raise RingError(self.status.get("error") or "Ring isn't connected")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"X-Api-Secret": self._secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error")
            except ValueError:
                message = None
            raise RingError(message or f"Ring helper error {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RingError(f"couldn't reach the Ring helper: {getattr(e, 'reason', e)}") from e

    def get_json(self, path):
        _, raw = self.request("GET", path)
        return json.loads(raw)

    def post(self, path, body):
        _, raw = self.request("POST", path, body)
        return json.loads(raw)


def ring_login(node_path="node", save_token=None, run=subprocess.run):
    """Run the helper's sign-in in this terminal and save the token it returns."""
    problem = setup_problem(node_path)
    if problem:
        print(f"Can't sign in to Ring: {problem}.")
        return 2
    result = run(node_command(node_path, "login.mjs"), cwd=BRIDGE_DIR, stdout=subprocess.PIPE, text=True)
    for line in (result.stdout or "").splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "token" and msg.get("refreshToken"):
            print(f"Saved Ring's sign-in in {save_token(msg['refreshToken'])}.")
            print("Restart Home Control (or wait a minute) for Ring to connect.")
            return 0
    return result.returncode or 1


class DemoRing:
    """Pretend Ring for --demo: two cameras, a few sensors and an alarm, no account needed."""

    label = "Ring (demo)"

    def __init__(self, bus):
        self.bus = bus
        self.status = {"label": self.label, "connected": True, "signed_in": True, "error": None,
                       "cameras": 3, "sensors": 3}
        self.calls = []
        self.state = {
            "locations": [{"id": "demo-home", "name": "Demo Home", "hasAlarm": True, "mode": "none",
                           "modeName": "disarmed", "alarmState": None, "sirenOn": False}],
            "cameras": [
                {"id": "1", "name": "Demo Doorbell", "locationId": "demo-home", "model": "demo", "isDoorbell": True,
                 "hasLight": False, "hasSiren": False, "lightOn": False, "battery": 76, "batteryLow": False,
                 "offline": False},
                {"id": "2", "name": "Demo Backyard Floodlight", "locationId": "demo-home", "model": "demo",
                 "isDoorbell": False, "hasLight": True, "hasSiren": True, "lightOn": False, "battery": None,
                 "batteryLow": False, "offline": False},
                {"id": "3", "name": "Demo Garage Cam", "locationId": "demo-home", "model": "demo",
                 "isDoorbell": False, "hasLight": False, "hasSiren": False, "lightOn": False, "battery": 9,
                 "batteryLow": True, "offline": True},
            ],
            "sensors": [
                {"id": "s1", "name": "Demo Front Door", "locationId": "demo-home", "type": "sensor.contact",
                 "faulted": False, "tamper": False, "battery": 92, "batteryLow": False},
                {"id": "s2", "name": "Demo Hallway Motion", "locationId": "demo-home", "type": "sensor.motion",
                 "faulted": True, "tamper": False, "battery": 88, "batteryLow": False},
                {"id": "s3", "name": "Demo Back Door", "locationId": "demo-home", "type": "sensor.contact",
                 "faulted": False, "tamper": False, "battery": 15, "batteryLow": True},
            ],
        }

    def start(self):
        for kind, extra in (("motion", {"camera": {"id": "2", "name": "Demo Backyard Floodlight"}}),
                            ("sensor-open", {"sensor": {"id": "s2", "name": "Demo Hallway Motion",
                                                        "type": "sensor.motion"}}),
                            ("doorbell", {"camera": {"id": "1", "name": "Demo Doorbell"}})):
            self.bus.publish({"source": "ring", "kind": kind, **extra})

    def stop(self):
        pass

    def _camera(self, cid):
        cam = next((c for c in self.state["cameras"] if c["id"] == cid), None)
        if cam is None:
            raise RingError("no such camera")
        return cam

    def get_json(self, path):
        parts = [p for p in path.split("?")[0].split("/") if p]
        if path == "/devices":
            return json.loads(json.dumps(self.state))
        if parts[:1] == ["cameras"] and len(parts) >= 3:
            self._camera(parts[1])
            if parts[2] == "events":
                return [{"dingId": "9001", "at": "2026-10-28T01:00:00Z", "kind": "motion", "hasRecording": True},
                        {"dingId": "9000", "at": "2026-10-27T22:15:00Z", "kind": "ding", "hasRecording": True}]
            if parts[2] == "recording":
                return {"url": "https://example.invalid/demo-clip"}
        raise RingError("not found")

    def request(self, method, path, body=None, timeout=20):
        parts = [p for p in path.split("/") if p]
        if method == "GET" and len(parts) == 3 and parts[0] == "cameras" and parts[2] == "snapshot":
            cam = self._camera(parts[1])
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">'
                   '<rect width="640" height="360" fill="#28323a"/>'
                   f'<text x="320" y="185" fill="#cfd8dc" font-family="sans-serif" font-size="28" '
                   f'text-anchor="middle">{cam["name"]} (demo)</text></svg>')
            return "image/svg+xml", svg.encode()
        raise RingError("not found")

    def post(self, path, body):
        parts = [p for p in path.split("/") if p]
        self.calls.append((path, body))
        if parts[0] == "cameras":
            cam = self._camera(parts[1])
            if parts[2] == "light":
                if not cam["hasLight"]:
                    raise RingError("this camera has no light")
                cam["lightOn"] = bool(body.get("on"))
            elif parts[2] == "siren" and not cam["hasSiren"]:
                raise RingError("this camera has no siren")
            return {"ok": True}
        loc = self.state["locations"][0]
        if parts[2] == "mode":
            names = {"none": "disarmed", "some": "home", "all": "away"}
            if body.get("mode") not in names:
                raise RingError("mode must be none, some or all")
            loc.update(mode=body["mode"], modeName=names[body["mode"]])
            self.bus.publish({"source": "ring", "kind": "alarm-mode", "mode": body["mode"],
                              "modeName": names[body["mode"]], "location": {"id": loc["id"], "name": loc["name"]}})
        elif parts[2] == "siren":
            loc["sirenOn"] = bool(body.get("on"))
        return {"ok": True}
