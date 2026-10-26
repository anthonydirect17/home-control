"""Find credentials without ever storing them in this folder.

Each secret is looked up, in order:
  1. its environment variable
  2. on Windows: Credential Manager, under the app's own entry name
     on Linux/macOS: ~/.config/home-control/secrets.json (readable by you only)
     (python -m home_control --set-guid / --set-govee-key saves it there)
  3. for the Govee Desktop GUID only: the Credential Manager entry named in
     settings ("guid_credential"), or the only other entry with "govee" in its name
"""

import json
import os
import sys

SECRETS = {
    "govee-desktop-guid": {"env": "GOVEE_DESKTOP_GUID", "target": "home-control/govee-desktop-guid",
                           "label": "Govee Desktop API GUID (Settings -> API in Govee Desktop)"},
    "govee-api-key": {"env": "GOVEE_API_KEY", "target": "home-control/govee-api-key",
                      "label": "Govee API key (emailed by Govee)"},
}
APP_TARGET = SECRETS["govee-desktop-guid"]["target"]

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCRED = ctypes.POINTER(_CREDENTIAL)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_PCRED)]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [_PCRED, wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       ctypes.POINTER(ctypes.POINTER(_PCRED))]
    _advapi.CredEnumerateW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2


def _decode(blob: bytes) -> str:
    # cmdkey, keyring and most tools store UTF-16-LE; some store UTF-8.
    if len(blob) >= 2 and len(blob) % 2 == 0 and blob[1] == 0:
        return blob.decode("utf-16-le").strip("\x00").strip()
    return blob.decode("utf-8", errors="replace").strip("\x00").strip()


def read_credential(target: str):
    if sys.platform != "win32":
        return None
    pcred = _PCRED()
    if not _advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        return None
    try:
        cred = pcred.contents
        return _decode(ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize))
    finally:
        _advapi.CredFree(pcred)


def write_credential(target: str, secret: str, user: str = "home-control"):
    if sys.platform != "win32":
        raise RuntimeError("Credential Manager is only available on Windows")
    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWrite failed")


def govee_credential_names():
    """Names (never values) of generic credentials that mention Govee."""
    if sys.platform != "win32":
        return []
    count = wintypes.DWORD()
    creds = ctypes.POINTER(_PCRED)()
    if not _advapi.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
        return []
    try:
        names = []
        for i in range(count.value):
            c = creds[i].contents
            if c.Type == _CRED_TYPE_GENERIC and c.TargetName and "govee" in c.TargetName.lower():
                names.append(c.TargetName)
        return names
    finally:
        _advapi.CredFree(creds)


def _secrets_file():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "home-control", "secrets.json")


def _read_file_secret(target):
    try:
        with open(_secrets_file(), encoding="utf-8") as f:
            return (json.load(f) or {}).get(target)
    except (OSError, ValueError):
        return None


def save_secret_value(name: str, value: str):
    """Store a secret the right way for this OS; returns where it went."""
    target = SECRETS[name]["target"]
    if sys.platform == "win32":
        write_credential(target, value)
        return f"Windows Credential Manager as '{target}'"
    path = _secrets_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        pass
    data[target] = value
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    return path


def get_secret(name: str, settings: dict = None):
    """Return (value, where_it_came_from) or (None, reason)."""
    spec = SECRETS[name]
    env = os.environ.get(spec["env"], "").strip()
    if env:
        return env, f"{spec['env']} environment variable"
    if sys.platform != "win32":
        value = _read_file_secret(spec["target"])
        if value:
            return value, _secrets_file()
        flag = "--set-guid" if name == "govee-desktop-guid" else "--set-govee-key"
        return None, f"not saved yet (python -m home_control {flag})"

    value = read_credential(spec["target"])
    if value:
        return value, f"Credential Manager ({spec['target']})"

    if name == "govee-desktop-guid":
        named = (settings or {}).get("guid_credential")
        if named:
            value = read_credential(named)
            if value:
                return value, f"Credential Manager ({named})"
            return None, f"Credential Manager entry '{named}' was not found"
        ours = {s["target"] for s in SECRETS.values()}
        others = [n for n in govee_credential_names() if n not in ours]
        if len(others) == 1:
            value = read_credential(others[0])
            if value:
                return value, f"Credential Manager ({others[0]})"
        if len(others) > 1:
            return None, ("several Govee entries in Credential Manager: " + ", ".join(others)
                          + ". Set guid_credential in data/settings.json to the right one.")
        return None, "not saved yet (python -m home_control --set-guid)"
    return None, "not saved yet (python -m home_control --set-govee-key)"


def get_guid(settings: dict):
    return get_secret("govee-desktop-guid", settings)
