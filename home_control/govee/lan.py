"""Govee's official LAN API: direct control over your home network.

No Govee Desktop, no admin rights, no internet. Each light needs LAN Control
switched on in the Govee Home app. Protocol (from Govee's LAN API docs):
  - discovery: send {"cmd": "scan"} to multicast 239.255.255.250:4001,
    lights answer on UDP 4002 with their ip, device id and model
  - commands go to the light's ip on UDP 4003: turn, brightness, colorwc,
    devStatus (the light answers devStatus on 4002 with its current state)

The LAN API colors a light as a whole; per-segment effects still need Govee
Desktop. If another program (often Govee Desktop) is already listening on
port 4002, commands still work but discovery and state reading don't.
"""

import json
import socket
import threading
import time

from .base import Backend, BackendError, Found, norm_id

MULTICAST = ("239.255.255.250", 4001)
LISTEN_PORT = 4002
CONTROL_PORT = 4003


def local_ipv4_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        addrs.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


class LanBackend(Backend):
    route = "lan"
    label = "Govee LAN"
    capabilities = frozenset({"power", "brightness", "color", "state"})

    def __init__(self, store=None, *, listen_port=LISTEN_PORT, control_port=CONTROL_PORT,
                 scan_target=MULTICAST, bind_host="", scan_seconds=2.5, state_timeout=1.0,
                 multicast=True):
        self.store = store
        self.listen_port = listen_port
        self.control_port = control_port
        self.scan_target = scan_target
        self.bind_host = bind_host
        self.scan_seconds = scan_seconds
        self.state_timeout = state_timeout
        self.multicast = multicast
        self.listening = False
        self._note = None
        self._recv = None
        self._send_sock = None
        self._found = {}
        self._status = {}       # ip -> latest devStatus data
        self._waiters = {}      # ip -> Event
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # setup

    def connect(self):
        if self._send_sock is None:
            self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self._recv is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind_host, self.listen_port))
        except OSError as e:
            sock.close()
            self._note = (f"Can't listen on UDP {self.listen_port} ({e}); another program, often "
                          "Govee Desktop, is using it. Lights can still be controlled, but finding "
                          "new lights and reading their state won't work.")
            return
        sock.settimeout(0.5)
        self._recv = sock
        self.listening = True
        threading.Thread(target=self._receive_loop, daemon=True, name="govee-lan").start()

    def close(self):
        self._stop.set()

    def status_note(self):
        return self._note

    def _receive_loop(self):
        while not self._stop.is_set():
            try:
                data, src = self._recv.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode())["msg"]
                cmd, payload = msg.get("cmd"), msg.get("data") or {}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if cmd == "scan":
                dev = norm_id(payload.get("device"))
                if dev:
                    with self._lock:
                        self._found[dev] = {"ip": payload.get("ip") or src[0], "sku": payload.get("sku", "")}
            elif cmd == "devStatus":
                with self._lock:
                    self._status[src[0]] = payload
                    waiter = self._waiters.get(src[0])
                if waiter:
                    waiter.set()

    def discover(self):
        cache = dict((self.store.get("lan_cache") if self.store else None) or {})
        if self.listening:
            with self._lock:
                self._found.clear()
            self._send_scan()
            time.sleep(self.scan_seconds)
            with self._lock:
                found = dict(self._found)
            for dev_id, info in found.items():
                cache[dev_id] = info
            if self.store and found:
                self.store.put("lan_cache", cache)
        return [Found(route=self.route, address=info["ip"], id=dev_id, model=info.get("sku", ""))
                for dev_id, info in cache.items()]

    def _send_scan(self):
        msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
        interfaces = local_ipv4_addresses() if self.multicast else []
        for addr in interfaces or [None]:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.multicast:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                if addr:
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
                    except OSError:
                        pass
            try:
                s.sendto(msg, self.scan_target)
            except OSError:
                pass
            s.close()

    # commands

    def _send(self, ip, cmd, data):
        if not ip:
            raise BackendError("no network address known for this light")
        if self._send_sock is None:
            self.connect()
        payload = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        try:
            self._send_sock.sendto(payload, (ip, self.control_port))
        except OSError as e:
            raise BackendError(f"couldn't reach {ip}: {e}") from e

    def power(self, address, on):
        self._send(address, "turn", {"value": 1 if on else 0})

    def brightness(self, address, value):
        self._send(address, "brightness", {"value": max(1, min(100, int(value)))})

    def color(self, address, rgb):
        r, g, b = (max(0, min(255, int(c))) for c in rgb)
        self._send(address, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})

    def state(self, address):
        if not self.listening or not address:
            return None
        event = threading.Event()
        with self._lock:
            self._waiters[address] = event
            self._status.pop(address, None)
        try:
            self._send(address, "devStatus", {})
            if not event.wait(self.state_timeout):
                return None
            with self._lock:
                data = self._status.get(address) or {}
        finally:
            with self._lock:
                self._waiters.pop(address, None)
        out = {}
        if "onOff" in data:
            out["on"] = bool(data["onOff"])
        if "brightness" in data:
            out["brightness"] = int(data["brightness"])
        c = data.get("color")
        if isinstance(c, dict) and not data.get("colorTemInKelvin"):
            out["color"] = "#{:02X}{:02X}{:02X}".format(int(c.get("r", 0)), int(c.get("g", 0)), int(c.get("b", 0)))
        return out
