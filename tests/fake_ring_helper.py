"""Stands in for ring-bridge/bridge.mjs in tests: same stdin, stdout and HTTP protocol, no Ring account.

FAKE_RING_MODE: "normal" (default), "crash" (exit right after ready), "fail" (report an error and exit).
FAKE_RING_LOG: file to append each received command to, as JSON lines.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

cfg = json.loads(sys.stdin.readline())
mode = os.environ.get("FAKE_RING_MODE", "normal")
log_path = os.environ.get("FAKE_RING_LOG")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def record(entry):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


record({"started": True, "token": cfg["refreshToken"], "argv": sys.argv})

if mode == "fail":
    emit({"type": "error", "message": "Refresh token is not valid"})
    sys.exit(1)

DEVICES = {
    "locations": [{"id": "L1", "name": "Home", "hasAlarm": True, "mode": "none", "modeName": "disarmed",
                   "alarmState": None, "sirenOn": False}],
    "cameras": [{"id": "101", "name": "Front Door", "hasLight": False, "hasSiren": False}],
    "sensors": [],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self):
        if self.headers.get("X-Api-Secret") != cfg["apiSecret"]:
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._ok():
            return
        if self.path == "/devices":
            return self._send(200, DEVICES)
        if self.path == "/cameras/101/snapshot":
            return self._send(200, raw=b"\xff\xd8JPEG", ctype="image/jpeg")
        self._send(404, {"error": "no such camera"})

    def do_POST(self):
        if not self._ok():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        record({"path": self.path, "body": body})
        self._send(200, {"ok": True})


server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
emit({"type": "token", "refreshToken": cfg["refreshToken"] + "-rotated"})
print("ring-client-api style log line that is not JSON")
sys.stdout.flush()
emit({"type": "ready", "connected": True, "locations": 1, "cameras": 1, "sensors": 0})
emit({"type": "event", "kind": "motion", "at": "2026-10-28T02:00:00Z", "camera": {"id": "101", "name": "Front Door"}})
if mode == "crash":
    sys.exit(3)
sys.stdin.read()  # stays up until the app closes stdin, like the real helper
server.shutdown()
