"""Finding this machine's Tailscale address at start-up.

`--host tailscale` binds the page to whatever Tailscale address the machine
has right now, so nothing breaks if the address changes. It is looked up
each time the app starts. If Tailscale isn't up yet (e.g. right after boot),
start-up fails and --keep-running tries again a minute later.
"""

import ipaddress
import socket
import subprocess

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_DNS = "100.100.100.100"  # Tailscale's own resolver; only reachable over Tailscale
CLI_PATHS = ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_tailscale(addr) -> bool:
    try:
        return ipaddress.ip_address(str(addr).strip()) in TAILSCALE_NET
    except ValueError:
        return False


def _route_probe():
    """The local address the OS would use to reach Tailscale's resolver. Sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((TAILSCALE_DNS, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tailscale_address(run=subprocess.run, probe=_route_probe):
    for exe in CLI_PATHS:
        try:
            out = run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10,
                      creationflags=NO_WINDOW).stdout or ""
        except (OSError, subprocess.SubprocessError):
            continue
        for word in out.split():
            if is_tailscale(word):
                return word.strip()
    addr = probe()
    return addr if is_tailscale(addr) else None


def resolve_host(host, finder=tailscale_address):
    """Turn --host into an address to bind. 'tailscale' means this machine's current Tailscale address."""
    if str(host).strip().lower() != "tailscale":
        return host
    addr = finder()
    if not addr:
        raise RuntimeError("No Tailscale address found. Is Tailscale running and signed in?")
    return addr
