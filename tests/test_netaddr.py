import subprocess

import pytest

from home_control.netaddr import is_tailscale, resolve_host, tailscale_address


class FakeRun:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = self.outputs.get(cmd[0])
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, 0, stdout=out or "")


def test_tailscale_range():
    assert is_tailscale("100.64.0.1") and is_tailscale("100.127.255.254")
    assert not is_tailscale("100.128.0.1") and not is_tailscale("192.168.1.5") and not is_tailscale("nope")


def test_uses_the_tailscale_cli_first_without_a_window():
    run = FakeRun({"tailscale": "100.101.102.103\n"})
    assert tailscale_address(run=run, probe=lambda: None) == "100.101.102.103"
    assert run.calls[0][0] == ["tailscale", "ip", "-4"]
    assert "creationflags" in run.calls[0][1]


def test_falls_back_to_full_cli_path_then_route_probe():
    run = FakeRun({"tailscale": FileNotFoundError(), r"C:\Program Files\Tailscale\tailscale.exe": "100.90.1.2"})
    assert tailscale_address(run=run, probe=lambda: None) == "100.90.1.2"
    run = FakeRun({"tailscale": FileNotFoundError(), r"C:\Program Files\Tailscale\tailscale.exe": OSError()})
    assert tailscale_address(run=run, probe=lambda: "100.70.0.9") == "100.70.0.9"
    assert tailscale_address(run=run, probe=lambda: "192.168.1.20") is None  # not a Tailscale address


def test_resolve_host():
    assert resolve_host("127.0.0.1") == "127.0.0.1"
    assert resolve_host("Tailscale", finder=lambda: "100.64.5.5") == "100.64.5.5"
    with pytest.raises(RuntimeError, match="Tailscale"):
        resolve_host("tailscale", finder=lambda: None)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", ""])
def test_never_listens_on_every_network(host):
    with pytest.raises(RuntimeError, match="every network"):
        resolve_host(host)


def test_mask_addresses():
    from home_control.netaddr import mask_addresses
    text = ("bind on ('100.101.102.103', 8780) failed; light 192.168.1.44 replied; "
            "v6 fd7a:115c:a1e0::1234 and fe80::1; device AB:CD:EF:12:34:56:78:90")
    masked = mask_addresses(text)
    for secret in ("100.101.102.103", "192.168.1.44", "fd7a:115c:a1e0::1234", "fe80::1", "AB:CD:EF:12:34:56:78:90"):
        assert secret not in masked
    assert masked.count("<address>") == 5
    kept = "--- start 2026-10-30 21:35:02 --- Python 3.12.10, helper on 127.0.0.1:8782 and ::1"
    assert mask_addresses(kept) == kept
