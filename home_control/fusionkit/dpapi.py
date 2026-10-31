"""Windows DPAPI: encrypt bytes so only the same Windows account can decrypt them.

Uses CryptProtectData and CryptUnprotectData from crypt32.dll through ctypes,
with no extra package. The scope is the current user (not the machine): only
that Windows account can decrypt the values. Any program running as that same
account can decrypt them too. The app-specific entropy value is in this public
code, so it is no barrier to such a program; it only keeps each app's values apart from
other apps' DPAPI data by accident. (Copied from home-control's dpapi.py.)

`libs` lets tests pass stand-ins for crypt32 and kernel32; the app never sets it.
"""

import ctypes

ENTROPY = b"fusionkit/v1"  # callers pass their own; this is only a default
CRYPTPROTECT_UI_FORBIDDEN = 0x1  # never show a prompt; fail instead


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    """A DATA_BLOB over a copy of `data`. Returns (blob, buffer); keep the buffer alive while the blob is used."""
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


_loaded = None


def _last_error():
    return ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0  # Windows only


def _windows_libs():
    global _loaded
    if _loaded is None:
        from ctypes import wintypes
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(DATA_BLOB)
        crypt32.CryptProtectData.argtypes = [blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                             wintypes.DWORD, blob_p]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                               wintypes.DWORD, blob_p]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _loaded = (crypt32, kernel32)
    return _loaded


def _take(out: DATA_BLOB, kernel32) -> bytes:
    """Copy the bytes DPAPI returned, then free its buffer."""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes, entropy: bytes = ENTROPY, libs=None) -> bytes:
    crypt32, kernel32 = libs or _windows_libs()
    data_in, keep_in = _blob(data)
    extra, keep_extra = _blob(entropy)
    out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.pointer(data_in), "home-control", ctypes.pointer(extra), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.pointer(out)):
        raise OSError(_last_error(), "Windows couldn't encrypt the secret (CryptProtectData)")
    return _take(out, kernel32)


def unprotect(data: bytes, entropy: bytes = ENTROPY, libs=None) -> bytes:
    crypt32, kernel32 = libs or _windows_libs()
    data_in, keep_in = _blob(data)
    extra, keep_extra = _blob(entropy)
    out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.pointer(data_in), None, ctypes.pointer(extra), None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.pointer(out)):
        raise OSError(_last_error(), "Windows couldn't decrypt the secret (CryptUnprotectData)")
    return _take(out, kernel32)
