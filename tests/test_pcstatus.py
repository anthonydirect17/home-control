"""PC status: read only, from Tailscale's own view; never an action, never a packet to the PC."""

import json
import subprocess

import pytest

from home_control import pcstatus
from home_control.pcstatus import PcStatus, read_tailscale_status
from home_control.services import ServiceError
from home_control.store import Store

TS = {
    "BackendState": "Running",
    "Self": {"HostName": "box-a", "Online": True},
    "Peer": {
        "nodekey:1": {"HostName": "desk-pc", "DNSName": "desk-pc.example.ts.net.", "Online": True,
                      "LastSeen": "0001-01-01T00:00:00Z"},
        "nodekey:2": {"HostName": "Laptop", "DNSName": "laptop.example.ts.net.", "Online": False,
                      "LastSeen": "2026-10-31T01:02:03Z"},
    },
}


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "settings.json"))
    s.update_settings({"pcs": [{"label": "This server", "this_machine": True},
                               {"label": "Desk PC", "tailscale_name": "DESK-PC"},
                               {"label": "Laptop", "tailscale_name": "laptop.example.ts.net"},
                               {"label": "Old PC", "tailscale_name": "gone"}]})
    return s


def test_reads_each_pc_from_tailscale(store):
    svc = PcStatus(store, read=lambda: TS, log=lambda m: None)
    assert svc.poll_once()
    pcs = svc.state()["pcs"]
    assert pcs[0] == {"label": "This server", "online": True, "tailscale": True, "last_seen": None,
                      "this_machine": True}
    assert pcs[1] == {"label": "Desk PC", "online": True, "last_seen": None}
    assert pcs[2] == {"label": "Laptop", "online": False, "last_seen": "2026-10-31T01:02+00:00"}
    assert pcs[3]["online"] is None and pcs[3]["note"] == "not found in Tailscale"
    assert svc.health_text() == "2 of 4 online"


def test_it_has_no_actions(store):
    svc = PcStatus(store, read=lambda: TS, log=lambda m: None)
    for action in ("sleep", "shutdown", "wake", "restart"):
        with pytest.raises(ServiceError, match="read only"):
            svc.act("Desk PC", action)


def test_tailscale_unreadable_marks_it_down(store):
    def broken():
        raise ServiceError("Tailscale's status couldn't be read")
    svc = PcStatus(store, read=broken, log=lambda m: None)
    assert not svc.poll_once()
    assert svc.status["up"] is False and "Tailscale" in svc.status["error"]


def test_reader_uses_the_local_cli_only_with_no_window():
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(TS))
    assert read_tailscale_status(run=run) == TS
    cmd, kw = calls[0]
    assert cmd[1:] == ["status", "--json"] and kw["creationflags"] == pcstatus.NO_WINDOW


def test_reader_tries_the_install_path_then_gives_a_plain_error():
    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    with pytest.raises(ServiceError, match="is Tailscale running"):
        read_tailscale_status(run=run)


def test_nothing_configured(tmp_path):
    svc = PcStatus(Store(str(tmp_path / "s.json")), read=lambda: TS, log=lambda m: None)
    svc.poll_once()
    assert svc.state() == {"pcs": []} and svc.health_text() == "none set up"
