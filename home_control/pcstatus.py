"""PC status: online or offline, read only.

This machine is "online" whenever the app runs and Tailscale is up here. Every other PC
is read from Tailscale's own view of the network (`tailscale status --json`, run on this
machine): nothing is ever sent to that PC, so a trading PC is never touched. There is no
sleep, shutdown or wake action, and there never will be.

Which PCs and what they are called come from the private settings (data/settings.json):

    "pcs": [
      {"label": "This server", "this_machine": true},
      {"label": "Office PC", "tailscale_name": "<that PC's name in Tailscale>"}
    ]
"""

import json
import subprocess
from datetime import datetime, timezone

from .netaddr import CLI_PATHS, NO_WINDOW
from .services import PollingService, ServiceError


def read_tailscale_status(run=subprocess.run):
    """Tailscale's status as a dict, from the local CLI. Raises ServiceError if it can't be read."""
    last = None
    for exe in CLI_PATHS:
        try:
            out = run([exe, "status", "--json"], capture_output=True, text=True, timeout=10,
                      creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            last = e
            continue
        if out.returncode == 0 and out.stdout:
            try:
                return json.loads(out.stdout)
            except ValueError:
                raise ServiceError("Tailscale's status couldn't be read") from None
        last = out.stderr or f"exit code {out.returncode}"
    raise ServiceError("Tailscale's status couldn't be read" + (" (is Tailscale running?)" if last else ""))


def _short(name):
    return str(name or "").split(".")[0].strip().lower()


def _when(text):
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if t.year < 2000 else t.astimezone(timezone.utc).isoformat(timespec="minutes")


class PcStatus(PollingService):
    key = "pcs"
    label = "PCs"
    interval = 30

    def __init__(self, store, read=read_tailscale_status, **kw):
        super().__init__(**kw)
        self.store = store
        self.read = read
        self._pcs = []

    def configured(self):
        return [p for p in (self.store.get("settings").get("pcs") or []) if isinstance(p, dict) and p.get("label")]

    def poll(self):
        pcs = self.configured()
        ts = self.read()
        self_node = ts.get("Self") or {}
        here_up = ts.get("BackendState", "Running") == "Running" and self_node.get("Online", True)
        peers = list((ts.get("Peer") or {}).values())
        result = []
        for pc in pcs:
            if pc.get("this_machine"):
                result.append({"label": pc["label"], "online": True, "tailscale": bool(here_up),
                               "last_seen": None, "this_machine": True})
                continue
            want = _short(pc.get("tailscale_name"))
            match = next((p for p in peers if want and want in (_short(p.get("HostName")), _short(p.get("DNSName")))),
                         None)
            if match is None:
                result.append({"label": pc["label"], "online": None, "last_seen": None,
                               "note": "not found in Tailscale"})
            else:
                online = bool(match.get("Online"))
                result.append({"label": pc["label"], "online": online,
                               "last_seen": None if online else _when(match.get("LastSeen"))})
        self._pcs = result

    def state(self):
        return {"pcs": list(self._pcs)}

    def health_text(self):
        if not self._pcs:
            return "none set up"
        online = sum(1 for p in self._pcs if p.get("online"))
        return f"{online} of {len(self._pcs)} online"

    def act(self, target, action, args=None):
        raise ServiceError("PC status is read only")
