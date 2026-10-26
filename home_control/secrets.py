"""Find the Govee Desktop API GUID without ever storing it in this folder.

Lookup order:
  1. GOVEE_DESKTOP_GUID environment variable
  2. Windows Credential Manager, entry "home-control/govee-desktop-guid"
     (python -m home_control --set-guid saves it there)
  3. Windows Credential Manager, the entry named in settings
     ("guid_credential"), or the only generic entry with "govee" in its name
"""

import os
import sys

APP_TARGET = "home-control/govee-desktop-guid"

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


def get_guid(settings: dict):
    """Return (guid, where_it_came_from) or (None, reason)."""
    env = os.environ.get("GOVEE_DESKTOP_GUID", "").strip()
    if env:
        return env, "GOVEE_DESKTOP_GUID environment variable"
    if sys.platform != "win32":
        return None, "not on Windows and GOVEE_DESKTOP_GUID is not set"

    value = read_credential(APP_TARGET)
    if value:
        return value, f"Credential Manager ({APP_TARGET})"

    named = (settings or {}).get("guid_credential")
    if named:
        value = read_credential(named)
        if value:
            return value, f"Credential Manager ({named})"
        return None, f"Credential Manager entry '{named}' was not found"

    others = [n for n in govee_credential_names() if n != APP_TARGET]
    if len(others) == 1:
        value = read_credential(others[0])
        if value:
            return value, f"Credential Manager ({others[0]})"
    if len(others) > 1:
        return None, ("several Govee entries in Credential Manager: " + ", ".join(others)
                      + ". Set guid_credential in data/settings.json to the right one.")
    return None, "no GUID found. Run: python -m home_control --set-guid"
