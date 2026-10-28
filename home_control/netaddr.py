"""Finding this machine's Tailscale address at start-up.

`--host tailscale` binds the page to whatever Tailscale address the machine
has right now, so nothing breaks if the address changes. It is looked up
each time the app starts. If Tailscale isn't up yet (e.g. right after boot),
start-up fails and --keep-running tries again a minute later.

Also here: the app refuses to listen on every network at once (0.0.0.0, ::),
and mask_addresses() keeps IP addresses out of the log file.
"""

import ipaddress
import re
import socket
import subprocess

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_DNS = "100.100.100.100"  # Tailscale's own resolver; only reachable over Tailscale
CLI_PATHS = ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EVERY_NETWORK = {"", "0.0.0.0", "::", "[::]"}
LOOPBACK = {"127.0.0.1", "::1"}
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
MASK = "<address>"


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


def mask_addresses(text):
    """Replace IPv4 and IPv6 addresses (except loopback) with a placeholder.

    Times like 21:35:02 are left alone: an IPv6 match needs "::" or at least 3 colons.
    """
    def v4(m):
        return m.group(0) if m.group(0) in LOOPBACK else MASK

    def v6(m):
        word = m.group(0)
        if word in LOOPBACK or ("::" not in word and word.count(":") < 3):
            return word
        return MASK

    return _IPV6.sub(v6, _IPV4.sub(v4, text))


def resolve_host(host, finder=tailscale_address):
    """Turn --host into an address to bind. 'tailscale' means this machine's current Tailscale address."""
    if str(host).strip() in EVERY_NETWORK:
        raise RuntimeError(f"won't listen on every network (--host {host!r}). "
                           "Use --host tailscale, or 127.0.0.1 for this PC only.")
    if str(host).strip().lower() != "tailscale":
        return host
    addr = finder()
    if not addr:
        raise RuntimeError("No Tailscale address found. Is Tailscale running and signed in?")
    return addr
