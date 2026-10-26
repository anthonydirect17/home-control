"""Drive Govee lights through Govee Desktop's OpenAPI (GoveeAPI.dll).

Govee Desktop must be running (as administrator) with Settings -> API ->
Enable API turned on, and Scenic/Razer DreamView turned off. The DLL is
loaded with pythonnet. All calls go through one lock because the DLL is
not documented as thread-safe.
"""

import glob
import json
import os
import subprocess
import sys
import threading

from .base import Backend, BackendError, Found

ERRORS = {
    1: "Govee Desktop is not running",
    100: "Govee Desktop program error",
    101: "initialization failed",
    102: "device is offline",
    1001: "API GUID was rejected (check Settings -> API in Govee Desktop)",
    1002: "a parameter was empty",
    1003: "no device with that name",
    1010: "color value error",
    1011: "invalid brightness",
    4000: "no response from the light (it may still have changed)",
    4001: "failed to send",
}

# 4000 means "sent, but no reply". The light usually changes anyway, so it
# isn't treated as a failure.
SOFT_CODES = {4000}


def describe(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return "OK" if code == 0 else f"{code}: {ERRORS.get(code, 'unknown error')}"


def find_dll(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    folders = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Path -like '*Govee*' } | "
             "Select-Object -ExpandProperty Path -Unique"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no window flash
        ).stdout
        folders += [os.path.dirname(p.strip()) for p in out.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            folders += glob.glob(os.path.join(base, "*Govee*"))
            folders += glob.glob(os.path.join(base, "*", "*Govee*"))
            folders += glob.glob(os.path.join(base, "Programs", "*Govee*"))
    for folder in folders:
        for pattern in ("GoveeAPI.dll", os.path.join("GoveeAPI", "GoveeAPI.dll"),
                        os.path.join("**", "GoveeAPI.dll")):
            hits = glob.glob(os.path.join(folder, pattern), recursive=True)
            if hits:
                return hits[0]
    return None


def _load_runtime(dll_path):
    try:
        import pythonnet
    except ImportError as e:
        raise BackendError("pythonnet is not installed (pip install -r requirements.txt)") from e
    with open(dll_path, "rb") as f:
        blob = f.read()
    try:
        if b".NETCoreApp" in blob:
            here = os.path.dirname(dll_path)
            configs = (glob.glob(os.path.join(here, "*.runtimeconfig.json"))
                       + glob.glob(os.path.join(os.path.dirname(here), "*.runtimeconfig.json")))
            if configs:
                pythonnet.load("coreclr", runtime_config=configs[0])
            else:
                pythonnet.load("coreclr")
        else:
            pythonnet.load("netfx")
    except RuntimeError:
        # Already loaded earlier in this process (e.g. after a reconnect).
        pass


class GoveeDesktopBackend(Backend):
    route = "desktop"
    label = "Govee Desktop"
    capabilities = frozenset({"power", "brightness", "color", "segments"})

    def __init__(self, guid: str, dll_path: str = None):
        self._guid = guid
        self._dll_hint = dll_path
        self._api = None
        self._lock = threading.Lock()
        self.dll_path = None
        self.methods = []

    def connect(self):
        if sys.platform != "win32":
            raise BackendError("Govee Desktop control only works on Windows")
        dll = find_dll(self._dll_hint)
        if not dll:
            raise BackendError("GoveeAPI.dll not found. Set govee_dll in data/settings.json "
                               "to its full path (it is in the Govee Desktop install folder).")
        self.dll_path = dll
        _load_runtime(dll)
        import clr  # provided by pythonnet

        sys.path.append(os.path.dirname(dll))
        try:
            clr.AddReference(os.path.splitext(dll)[0])
        except Exception:
            clr.AddReference("GoveeAPI")
        from GoveeAPI import ConnectGovee  # type: ignore

        api = ConnectGovee()
        self.methods = sorted({m.Name for m in api.GetType().GetMethods()
                               if m.DeclaringType.Name == "ConnectGovee"})
        with self._lock:
            try:
                code = api.InitConnect(self._guid)
            except TypeError:
                from System import Guid  # type: ignore
                code = api.InitConnect(Guid(self._guid))
        if str(code) != "0":
            raise BackendError(f"Govee Desktop refused the connection ({describe(code)})")
        self._api = api

    def _call(self, method, *args):
        if self._api is None:
            raise BackendError("not connected to Govee Desktop")
        with self._lock:
            try:
                result = getattr(self._api, method)(*args)
            except Exception as e:  # .NET exceptions surface as generic Exceptions
                raise BackendError(f"{method} failed: {e}") from e
        return result

    def _check(self, code, what):
        try:
            value = int(str(code))
        except ValueError:
            raise BackendError(f"{what}: unexpected reply {code!r}")
        if value != 0 and value not in SOFT_CODES:
            raise BackendError(f"{what}: {describe(value)}")
        return value

    def discover(self):
        raw = self._call("GetDeviceBaseInfo")
        try:
            data = json.loads(str(raw))
        except ValueError:
            raise BackendError(f"device list was not JSON: {str(raw)[:200]}")
        if isinstance(data, dict):
            data = next((data[k] for k in ("data", "devices", "Devices", "Data")
                         if isinstance(data.get(k), list)), [data])
        found = []
        for d in data:
            if str(d.get("IsLANOn", "1")) != "1":
                continue  # Govee Desktop can't reach it; the cloud may
            try:
                segs = int(d.get("SegmentNums") or 1)
            except (TypeError, ValueError):
                segs = 1
            name = str(d.get("Name", ""))
            found.append(Found(route=self.route, address=name, name=name,
                               model=str(d.get("SkuType", "")), segments=max(1, segs)))
        return found

    def power(self, name, on):
        return self._check(self._call("DeviceSwitchControl", name, 1 if on else 0), f"{name} power")

    def brightness(self, name, value):
        value = max(1, min(100, int(value)))
        return self._check(self._call("DeviceBrightnessControl", name, value), f"{name} brightness")

    def color(self, name, rgb):
        r, g, b = (max(0, min(255, int(c))) for c in rgb)
        return self._check(self._call("DeviceColorControl", name, r, g, b), f"{name} color")

    def segments(self, name, hex_colors):
        return self._check(self._call("DeviceSegmentsColor", name, ",".join(hex_colors), 0),
                           f"{name} segments")
