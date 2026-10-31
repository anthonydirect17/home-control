"""Fire TV sticks: on and off, home, back, play and pause, volume, and opening apps, over network ADB.

How it connects (adb-shell, https://github.com/JeffLIrion/adb_shell):
- Outbound only: this machine opens TCP 5555 on each stick. Nothing listens here, so no
  inbound firewall rule is needed.
- Each stick needs Developer Options -> ADB debugging turned on (once, by hand), and the
  first connection approved on the TV screen ("Always allow from this computer").
- The app's ADB key is made here, kept only in the secrets file (encrypted with DPAPI on
  Windows, like every other secret) and loaded into memory; it is never written as a file.
- The sticks control the TVs themselves through HDMI-CEC: sleeping the stick usually turns
  the TV off, waking it turns the TV on, and the volume keys reach the TV or soundbar.

Sticks are paired once with `python -m home_control --firetv-pair`, which searches the
network (DIAL over SSDP, the same search phones use to cast) and lists what it finds. Each
is remembered in its own private file, data/firetv.json (serial number, name, model and last
address), and found again by searching when its address changes. A file of its own means a
pairing done while the app runs is never overwritten by the app's next settings save.

Newer sticks running Amazon's Vega OS have no network ADB, so they can't be paired.
"""

import base64
import ipaddress
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .services import PollingService, ServiceError

ADB_PORT = 5555
KEY_SECRET = "firetv-adb-key"
PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
KEYS = {"home": 3, "back": 4, "play_pause": 85, "play": 126, "pause": 127,
        "volume_up": 24, "volume_down": 25, "mute": 164, "on": 224, "off": 223}
# Streaming apps by the name the page shows; only the ones installed on a stick are offered.
KNOWN_APPS = {
    "Netflix": "com.netflix.ninja",
    "YouTube": "com.amazon.firetv.youtube",
    "YouTube TV": "com.amazon.firetv.youtube.tv",
    "Prime Video": "com.amazon.avod",
    "Disney+": "com.disney.disneyplus",
    "Hulu": "com.hulu.plus",
    "Max": "com.wbd.stream",
    "Peacock": "com.peacocktv.peacockandroid",
    "Paramount+": "com.cbs.ott",
    "Apple TV": "com.apple.atve.amazon.appletv",
    "Plex": "com.plexapp.android",
    "Spotify": "com.spotify.tv.android",
    "Pluto TV": "tv.pluto.android",
    "Tubi": "com.tubitv",
}
HOME_SCREENS = {"com.amazon.tv.launcher", "com.amazon.firebat"}
APP_NAMES = {pkg: name for name, pkg in KNOWN_APPS.items()}
STATUS_CMD = ("dumpsys power | grep -E -m2 'mWakefulness=|Display Power: state='; "
              "dumpsys window | grep -E -m1 'mCurrentFocus='; "
              "dumpsys media_session | grep -E -m1 'state=PlaybackState'")
APPS_EVERY = 1800


def read_paired(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("serial")] if isinstance(data, list) else []


def write_paired(path, sticks):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sticks, f, indent=2)
    os.replace(tmp, path)


# ---------- the key ----------

def make_key():
    """A new 2048-bit RSA key as PEM text (PKCS#8), made in memory."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def android_public_key(private_pem):
    """The public half in Android's ADB format (as adb-shell's keygen writes it), with a plain label."""
    from cryptography.hazmat.primitives import serialization
    numbers = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None) \
        .private_numbers().public_numbers
    words = 2048 // 32
    n0inv = (1 << 32) - pow(numbers.n % (1 << 32), -1, 1 << 32)
    rr = pow(1 << 2048, 2, numbers.n)
    blob = struct.pack("<LL256s256sL", words, n0inv, numbers.n.to_bytes(256, "little"),
                       rr.to_bytes(256, "little"), numbers.e)
    return base64.b64encode(blob).decode("ascii") + " home-control"


def signer_for(private_pem):
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner
    return PythonRSASigner(android_public_key(private_pem), private_pem)


def load_or_make_key(get_secret, save_secret, settings):
    pem, _ = get_secret(KEY_SECRET, settings)
    if not pem:
        pem = make_key()
        save_secret(KEY_SECRET, pem, settings)
    return pem


# ---------- finding sticks (DIAL over SSDP) ----------

SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
          "ST: urn:dial-multiscreen-org:service:dial:1\r\n\r\n").encode()


def ssdp_locations(timeout=3.0):
    """(LOCATION header, the address the reply came from) for everything that answers a DIAL search."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found = set()
    try:
        for _ in range(2):
            sock.sendto(SEARCH, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (source, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            for line in data.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("location:"):
                    found.add((line.split(":", 1)[1].strip(), source))
    finally:
        sock.close()
    return sorted(found)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a device description is never followed elsewhere


MAX_DESCRIPTION = 64 * 1024


def _fetch(url):
    opener = urllib.request.build_opener(_NoRedirects)
    with opener.open(url, timeout=3) as resp:
        return resp.read(MAX_DESCRIPTION)


def safe_location(location, source):
    """The stick's address, if the description is plain http on the very address the reply came from,
    and that is a private home-network address. Otherwise None (the answer is ignored)."""
    try:
        url = urllib.parse.urlsplit(location)
        host = url.hostname or ""
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if url.scheme != "http" or host != source or addr.version != 4:
        return None
    if not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return None
    return host


def describe(location, source, fetch=None):
    """{name, model, udn, ip} from a DIAL device description, or None if it isn't a safe Amazon device."""
    host = safe_location(location, source)
    if host is None:
        return None
    try:
        root = ET.fromstring((fetch or _fetch)(location))
    except Exception:
        return None
    text = {el.tag.split("}")[-1]: (el.text or "").strip() for el in root.iter()}
    if "amazon" not in text.get("manufacturer", "").lower():
        return None
    return {"name": text.get("friendlyName") or "Fire TV", "model": text.get("modelName") or "",
            "udn": text.get("UDN") or None, "ip": host}


def find_sticks(locate=ssdp_locations, fetch=None):
    sticks = [describe(loc, source, fetch) for loc, source in locate()]
    return [s for s in sticks if s]


# ---------- one stick ----------

def parse_status(text):
    """What the stick is doing, from STATUS_CMD's output."""
    power = "unknown"
    m = re.search(r"mWakefulness=(\w+)", text)
    if m:
        power = {"Awake": "on", "Asleep": "off", "Dreaming": "screensaver", "Dozing": "off"}.get(m.group(1), "unknown")
    app = None
    m = re.search(r"mCurrentFocus=Window\{[^ ]+ \S+ ([A-Za-z0-9_.]+)/", text)
    if m:
        app = m.group(1)
    media = None
    m = re.search(r"state=PlaybackState \{state=(\d+)", text)
    if m:
        media = {"3": "playing", "2": "paused", "1": "stopped"}.get(m.group(1))
    return {"power": power, "app_id": app, "media": media}


def app_label(package):
    if not package:
        return None
    if package in HOME_SCREENS:
        return "Home screen"
    return APP_NAMES.get(package, package)


class Stick:
    def __init__(self, saved, make_device):
        self.saved = dict(saved)
        self.make_device = make_device
        self.dev = None
        self.info = {}
        self.apps = []
        self.apps_at = 0
        self.lock = threading.Lock()

    @property
    def serial(self):
        return self.saved.get("serial")

    def connect(self, signer, auth_timeout_s=10):
        if self.dev is not None:
            return
        dev = self.make_device(self.saved["ip"])
        try:
            dev.connect(rsa_keys=[signer], auth_timeout_s=auth_timeout_s)
        except Exception:
            try:
                dev.close()
            except Exception:
                pass
            raise
        self.dev = dev

    def shell(self, cmd, timeout_s=10):
        try:
            return self.dev.shell(cmd, timeout_s=timeout_s) or ""
        except Exception:
            self.drop()
            raise

    def drop(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None


def adb_device(ip):
    from adb_shell.adb_device import AdbDeviceTcp
    return AdbDeviceTcp(ip, ADB_PORT, default_transport_timeout_s=9.0)


class FireTvService(PollingService):
    key = "firetv"
    label = "Fire TV"
    interval = 10

    def __init__(self, paired_path, get_key, make_device=adb_device, find=find_sticks, make_signer=signer_for,
                 clock=time.time, **kw):
        super().__init__(clock=clock, **kw)
        self.paired_path = paired_path
        self.get_key = get_key          # returns the PEM text, or None if no stick was ever paired
        self.make_device = make_device
        self.find = find
        self.make_signer = make_signer
        self._signer = None
        self.sticks = {}                # serial -> Stick
        self._lock = threading.Lock()

    def _paired(self):
        return read_paired(self.paired_path)

    def _signer_or_none(self):
        if self._signer is None:
            pem = self.get_key()
            if pem:
                self._signer = self.make_signer(pem)
        return self._signer

    def _relocate(self, stick):
        """Search the network again for a stick whose address changed (matched by its DIAL id)."""
        udn = stick.saved.get("udn")
        if not udn:
            return False
        for found in self.find():
            if found.get("udn") == udn and found["ip"] != stick.saved.get("ip"):
                stick.saved["ip"] = found["ip"]
                self._save_addresses()
                return True
        return False

    def _save_addresses(self):
        paired = self._paired()
        for p in paired:
            st = self.sticks.get(p["serial"])
            if st is not None:
                p["ip"] = st.saved["ip"]
        write_paired(self.paired_path, paired)

    def poll(self):
        paired = self._paired()
        if not paired:
            self.sticks = {}
            self.status["note"] = "no Fire TV paired yet (python -m home_control --firetv-pair)"
            return
        for p in paired:
            if p["serial"] not in self.sticks:
                self.sticks[p["serial"]] = Stick(p, self.make_device)
        for serial in [s for s in self.sticks if s not in {p["serial"] for p in paired}]:
            self.sticks.pop(serial).drop()
        signer = self._signer_or_none()
        if signer is None:
            raise ServiceError("the Fire TV key is missing; pair the sticks again (python -m home_control --firetv-pair)")
        answering = 0
        for stick in self.sticks.values():
            with stick.lock:
                if self._read(stick, signer):
                    answering += 1
        self.status["note"] = None
        if not answering:
            raise ServiceError("no Fire TV is answering" if len(self.sticks) > 1 else "the Fire TV isn't answering")

    def _read(self, stick, signer):
        for attempt in (1, 2):
            try:
                stick.connect(signer)
                info = parse_status(stick.shell(STATUS_CMD))
                if self.clock() - stick.apps_at > APPS_EVERY:
                    installed = set(re.findall(r"package:(\S+)", stick.shell("pm list packages")))
                    stick.apps = [name for name, pkg in KNOWN_APPS.items() if pkg in installed]
                    stick.apps_at = self.clock()
                stick.info = dict(info, name=stick.saved.get("name") or "Fire TV", serial=stick.serial,
                                  app=app_label(info["app_id"]), answering=True)
                return True
            except Exception:
                stick.drop()
                if attempt == 1 and self._relocate(stick):
                    continue
                stick.info = dict(stick.info or {"name": stick.saved.get("name") or "Fire TV",
                                                 "serial": stick.serial}, answering=False, power="unknown")
                return False
        return False

    def state(self):
        return {"sticks": [dict(st.info or {"name": st.saved.get("name"), "serial": st.serial}, apps=list(st.apps))
                           for st in self.sticks.values()]}

    def health_text(self):
        if not self.sticks:
            return "none paired"
        on = sum(1 for st in self.sticks.values() if st.info.get("power") == "on")
        return f"{on} of {len(self.sticks)} on"

    def _stick(self, target):
        if not self.sticks:
            raise ServiceError("no Fire TV paired yet")
        if target in (None, "") and len(self.sticks) == 1:
            return next(iter(self.sticks.values()))
        for st in self.sticks.values():
            if target == st.serial or str(target).lower() == str(st.saved.get("name", "")).lower():
                return st
        raise KeyError(f"no Fire TV {target!r}" if target else "say which Fire TV")

    def act(self, target, action, args=None):
        args = args or {}
        stick = self._stick(target)
        if action == "launch":
            name = str(args.get("app", ""))
            package = KNOWN_APPS.get(name, name)
            if not PACKAGE.fullmatch(package):  # fullmatch: no trailing newline or anything else
                raise ValueError("that isn't an app name")
            cmd = f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
        elif action in KEYS:
            cmd = f"input keyevent {KEYS[action]}"
        else:
            raise ValueError(f"unknown Fire TV action {action!r}")
        signer = self._signer_or_none()
        if signer is None:
            raise ServiceError("the Fire TV key is missing")
        with stick.lock:
            try:
                stick.connect(signer)
                stick.shell(cmd)
            except Exception as e:
                raise ServiceError(f"the Fire TV didn't answer ({type(e).__name__})") from e
            self._read(stick, signer)
        return {"stick": stick.serial}


# ---------- pairing (Anthony, at the console) ----------

PAIR_PROPS = ("getprop ro.serialno; settings get global device_name; getprop ro.product.model; "
              "getprop ro.build.version.name")


def pair(paired_path, get_secret, save_secret, settings, ask=input, say=print, find=find_sticks,
         make_device=adb_device, make_signer=signer_for):
    """Find the sticks, let Anthony pick one, connect (he approves on the TV), and remember it."""
    say("Looking for Fire TVs on the home network...")
    sticks = find()
    for i, s in enumerate(sticks, 1):
        say(f"  {i}. {s['name']} ({s['model'] or 'Fire TV'})")
    if sticks:
        choice = ask("Which one? Type its number, or press Enter to type its address instead: ").strip()
    else:
        say("None answered the search.")
        choice = ""
    if choice.isdigit() and 1 <= int(choice) <= len(sticks):
        target = sticks[int(choice) - 1]
    else:
        ip = ask("Its address (Settings > My Fire TV > About > Network on the TV): ").strip()
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            say("That isn't an address. Nothing saved.")
            return 1
        target = {"name": "Fire TV", "model": "", "udn": None, "ip": ip}
    try:
        pem = load_or_make_key(get_secret, save_secret, settings)
    except Exception as e:
        say(f"Couldn't make or save the Fire TV key ({e}). Nothing paired.")
        return 1
    say("Connecting. On the TV, choose 'Always allow from this computer', then OK (you have 60 seconds).")
    dev = make_device(target["ip"])
    try:
        dev.connect(rsa_keys=[make_signer(pem)], auth_timeout_s=60)
        lines = (dev.shell(PAIR_PROPS, timeout_s=10) or "").splitlines() + ["", "", "", ""]
    except Exception as e:
        say(f"Couldn't connect ({type(e).__name__}). Is ADB debugging on, and was the prompt on the TV accepted?")
        say("Sticks running Amazon's Vega OS can't be paired: they have no network ADB.")
        return 1
    finally:
        try:
            dev.close()
        except Exception:
            pass
    serial, name, model, version = (x.strip() for x in lines[:4])
    if not serial:
        say("The stick didn't give its serial number. Nothing saved.")
        return 1
    entry = {"serial": serial, "name": name if name and name != "null" else target["name"],
             "model": model or target["model"], "fire_os": version, "ip": target["ip"], "udn": target["udn"]}
    paired = [p for p in read_paired(paired_path) if p.get("serial") != serial] + [entry]
    write_paired(paired_path, paired)
    say(f"Paired: {entry['name']}. Restart the app if it's running.")
    return 0
