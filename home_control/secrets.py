"""Find credentials without ever storing them in the repo.

Each secret is looked up, in order:
  1. its environment variable
  2. the secrets store:
     - default (every OS): `data/secrets.json` in the app's own data folder.
       The data folder is git-ignored, and the file is locked so only the
       account running the app can open it (icacls on Windows, 0600 elsewhere).
       On Windows every value in it is also encrypted with DPAPI for that
       account (stored as "dpapi:<base64>"); plain values left from older
       versions are encrypted at the next start. Other systems keep plain
       values in the locked file.
     - Windows, after `--secrets-store credential-manager`: that account's
       Windows Credential Manager, under the app's own entry names.
     The --set-... commands save into whichever store is active.
  3. for the Govee Desktop GUID only, in Credential Manager mode: the entry
     named in settings ("guid_credential"), or the only other entry with
     "govee" in its name
"""

import base64
import binascii
import json
import os
import subprocess
import sys
import threading

SECRETS = {
    "govee-desktop-guid": {"env": "GOVEE_DESKTOP_GUID", "target": "home-control/govee-desktop-guid",
                           "label": "Govee Desktop API GUID (Settings -> API in Govee Desktop)"},
    "govee-api-key": {"env": "GOVEE_API_KEY", "target": "home-control/govee-api-key",
                      "label": "Govee API key (emailed by Govee)"},
    # Salted hash of the page PIN (never the PIN itself); see auth.py.
    "page-pin-hash": {"env": "HOME_CONTROL_PIN_HASH", "target": "home-control/page-pin-hash",
                      "label": "page PIN hash"},
    # Ring's sign-in token. Ring rotates it; the app saves each new one at once.
    "ring-refresh-token": {"env": "RING_REFRESH_TOKEN", "target": "home-control/ring-refresh-token",
                           "label": "Ring sign-in token"},
    # Random key that signs the "remember this device" cookies; created automatically.
    "session-key": {"env": "HOME_CONTROL_SESSION_KEY", "target": "home-control/session-key",
                    "label": "cookie signing key"},
}
SET_FLAGS = {"govee-desktop-guid": "--set-guid", "govee-api-key": "--set-govee-key", "page-pin-hash": "--set-pin",
             "ring-refresh-token": "--ring-login"}

MARKER = "dpapi:"  # an encrypted value is MARKER + base64 of the DPAPI output
STORE_CREDMAN = "credential-manager"
STORE_FILE = "file"
# Windows only: start a console program without flashing a window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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


_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_data_dir = _DEFAULT_DATA_DIR


def set_data_dir(path):
    """The app calls this at start-up with its --data folder; the secrets file lives there."""
    global _data_dir
    _data_dir = path or _DEFAULT_DATA_DIR


def secrets_store(settings=None):
    """Which store is active: the private file in the data folder (default) or Credential Manager."""
    chosen = (settings or {}).get("secrets_store") or STORE_FILE
    if chosen == STORE_CREDMAN and sys.platform == "win32":
        return STORE_CREDMAN
    return STORE_FILE


def secrets_file():
    return os.path.join(_data_dir, "secrets.json")


def _current_user_sid():
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW).stdout
        sid = out.strip().split(",")[-1].strip().strip('"')
        if sid.startswith("S-1-"):
            return sid
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def icacls_commands(path, is_dir, sid):
    """The icacls calls that leave only this account with access (no inherited rights)."""
    grant = f"*{sid}:(OI)(CI)F" if is_dir else f"*{sid}:F"
    return [["icacls", path, "/inheritance:r", "/grant:r", grant]]


def _lock_down(path, is_dir):
    if sys.platform != "win32":
        os.chmod(path, 0o700 if is_dir else 0o600)
        return
    sid = _current_user_sid()
    if not sid:
        raise OSError("couldn't find this Windows account's SID to lock down the secrets file")
    for cmd in icacls_commands(path, is_dir, sid):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise OSError(f"icacls failed on {path}: {result.stdout.strip() or result.stderr.strip()}")


class SecretUnreadable(Exception):
    """A saved value that can't be decrypted here. The message never contains the stored text."""


def _cipher():
    """DPAPI on Windows; None elsewhere (values stay plain in the locked file, as before)."""
    if sys.platform == "win32":
        from . import dpapi
        return dpapi
    return None


def _conceal(value: str) -> str:
    cipher = _cipher()
    if cipher is None:
        return value
    return MARKER + base64.b64encode(cipher.protect(value.encode("utf-8"))).decode("ascii")


def _reveal(stored: str) -> str:
    if not stored.startswith(MARKER):
        return stored  # plain, from before encryption (or not Windows)
    cipher = _cipher()
    if cipher is None:
        raise SecretUnreadable("it is encrypted with Windows DPAPI and can't be read on this system")
    try:
        return cipher.unprotect(base64.b64decode(stored[len(MARKER):], validate=True)).decode("utf-8")
    except (OSError, ValueError, binascii.Error):
        raise SecretUnreadable("this Windows account can't decrypt it (saved by another account, "
                               "or the account's keys were reset)") from None


def _name_of(target):
    for name, spec in SECRETS.items():
        if spec["target"] == target:
            return name
    return target


_file_lock = threading.RLock()  # the Ring token can be saved from another thread


def _read_file_all():
    try:
        with open(secrets_file(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write_file_all(data):
    path = secrets_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    _lock_down(path, is_dir=False)
    return path


def _write_file_secret(target, value):
    stored = _conceal(value)  # encrypt before touching the file; if it fails, nothing is written
    with _file_lock:
        data = _read_file_all()
        data[target] = stored
        return _write_file_all(data)


def encrypt_saved_secrets():
    """Encrypt every plain value left in the secrets file. Returns the names encrypted, never values."""
    if _cipher() is None or not os.path.exists(secrets_file()):
        return []
    with _file_lock:
        data = _read_file_all()
        plain = [k for k, v in data.items() if isinstance(v, str) and v and not v.startswith(MARKER)]
        if not plain:
            return []
        for k in plain:
            data[k] = _conceal(data[k])
        _write_file_all(data)
    return [_name_of(k) for k in plain]


def secrets_report(settings: dict = None):
    """[(name, state)] for every secret: how it is kept, and whether it is encrypted. Never a value."""
    rows = []
    if secrets_store(settings) == STORE_CREDMAN:
        for name, spec in SECRETS.items():
            if os.environ.get(spec["env"], "").strip():
                rows.append((name, f"from the {spec['env']} environment variable (not on disk)"))
            else:
                saved = read_credential(spec["target"]) is not None
                rows.append((name, "in Windows Credential Manager (protected by Windows)" if saved else "not saved"))
        return rows
    data = _read_file_all()
    encrypting = _cipher() is not None
    for target in [s["target"] for s in SECRETS.values()] + sorted(set(data) - {s["target"] for s in SECRETS.values()}):
        name = _name_of(target)
        spec = SECRETS.get(name)
        stored = data.get(target)
        from_env = bool(spec and os.environ.get(spec["env"], "").strip())
        if not isinstance(stored, str) or not stored:
            state = "not saved"
            if from_env:
                state = f"from the {spec['env']} environment variable (not on disk)"
                from_env = False
        elif stored.startswith(MARKER):
            try:
                _reveal(stored)
                state = "encrypted"
            except SecretUnreadable as e:
                state = f"encrypted, but {e}"
        elif encrypting:
            state = "NOT encrypted (it will be encrypted at the next start)"
        else:
            state = "not encrypted (DPAPI is Windows only; the file is locked to this account)"
        if from_env:
            state += f"; the {spec['env']} environment variable is used instead"
        rows.append((name, state))
    return rows


def report_is_ok(rows):
    """True when nothing is left unencrypted on Windows and nothing fails to decrypt."""
    return not any(state.startswith("NOT encrypted") or state.startswith("encrypted, but") for _, state in rows)


def save_secret_value(name: str, value: str, settings: dict = None):
    """Store a secret in the active store; returns where it went (never the value)."""
    target = SECRETS[name]["target"]
    if secrets_store(settings) == STORE_CREDMAN:
        write_credential(target, value)
        return f"Windows Credential Manager as '{target}'"
    return _write_file_secret(target, value)


def copy_secrets_to_file(settings: dict = None):
    """Copy every secret found in Credential Manager into the data-folder file. Returns the names copied."""
    copied = []
    for name, spec in SECRETS.items():
        value = read_credential(spec["target"])
        if value:
            _write_file_secret(spec["target"], value)
            copied.append(name)
    return copied


def get_secret(name: str, settings: dict = None):
    """Return (value, where_it_came_from) or (None, reason)."""
    spec = SECRETS[name]
    env = os.environ.get(spec["env"], "").strip()
    if env:
        return env, f"{spec['env']} environment variable"
    missing = f"not saved yet (python -m home_control {SET_FLAGS[name]})" if name in SET_FLAGS else "not saved yet"

    if secrets_store(settings) == STORE_FILE:
        stored = _read_file_all().get(spec["target"])
        if stored:
            try:
                return _reveal(stored), secrets_file()
            except SecretUnreadable as e:
                again = f" (save it again: python -m home_control {SET_FLAGS[name]})" if name in SET_FLAGS else ""
                return None, f"saved in {secrets_file()}, but {e}{again}"
        return None, missing

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
    return None, missing


def get_guid(settings: dict):
    return get_secret("govee-desktop-guid", settings)
