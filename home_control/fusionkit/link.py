"""fusion-link/1: how an app behind the Bridge knows a request came through the Bridge.

The Bridge is the only door: it holds the one sign-in (the page PIN) and passes each
request on to the app on 127.0.0.1. An app must not trust 127.0.0.1 alone, because any
program on the box can reach it. So the Bridge signs every request it passes on with a
secret it shares with that app only, and the app checks the signature.

Headers the Bridge adds (it removes any of these a browser sends):

    X-Bridge-User   who signed in at the door ("owner")
    X-Bridge-Base   the app's path on the Bridge, e.g. "/home"; the app builds links from it
    X-Bridge-Route  "tailscale" (everything allowed) or "public" (web-view-safe routes only)
    X-Bridge-Time   unix seconds
    X-Bridge-Sig    HMAC-SHA256 over the lines below, base64url without padding:
                    fusion-link/1, time, method, path with query, user, base, route

Sensitive actions (the alarm, sirens): the page asks the Bridge to confirm the PIN, and
the Bridge answers with a one-time token for that app and action, valid 60 seconds. The
app checks it with check_confirm(), which also refuses a token used before.

The shared secret is a link file the Bridge writes for each app, encrypted with DPAPI for
the account both run in (plain, locked to the owner, on systems without DPAPI). The app's
private settings name the file. Nothing here is ever logged or sent anywhere else.

Standard library only: apps copy the fusionkit folder into their own code.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

SCHEMA = "fusion-link/1"
MAX_SKEW = 60          # seconds a signed request stays good (clocks are the same box)
CONFIRM_SECONDS = 60
ENTROPY = b"fusion-link/v1"
H_USER, H_BASE, H_ROUTE, H_TIME, H_SIG = ("X-Bridge-User", "X-Bridge-Base", "X-Bridge-Route",
                                          "X-Bridge-Time", "X-Bridge-Sig")
HEADERS = (H_USER, H_BASE, H_ROUTE, H_TIME, H_SIG)
H_CONFIRM = "X-Fusion-Confirm"   # the page sends a one-time confirmation token in this header
ROUTES = ("tailscale", "public")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret, *parts):
    msg = "\n".join(str(p) for p in parts).encode("utf-8")
    return _b64(hmac.new(secret, msg, hashlib.sha256).digest())


# ---------- signing requests (the Bridge) and checking them (the app) ----------

def signed_path(scope):
    """The exact path and query that are signed: the raw bytes as sent, never a decoded copy.

    Both sides use this, so a path with %2F, %25, %23 or spaces means the same to both.
    scope: the ASGI scope (Starlette: request.scope). None if the path isn't plain ASCII.
    """
    raw = scope.get("raw_path") or scope.get("path", "").encode("utf-8")
    raw = raw.split(b"?", 1)[0]          # some servers include the query in raw_path
    query = scope.get("query_string") or b""
    try:
        text = raw.decode("ascii") + ("?" + query.decode("ascii") if query else "")
    except UnicodeDecodeError:
        return None
    return text


def bridge_headers(secret, method, path, base, route="tailscale", user="owner", now=None):
    """The headers the Bridge adds to a request it passes to an app. path includes the query."""
    when = str(int(now if now is not None else time.time()))
    return {H_USER: user, H_BASE: base, H_ROUTE: route, H_TIME: when,
            H_SIG: _mac(secret, SCHEMA, when, method.upper(), path, user, base, route)}


def verify(secret, method, path, headers, now=None, max_skew=MAX_SKEW):
    """{"user", "base", "route"} if the request was signed by the Bridge with this secret; else None.

    path: signed_path(request.scope). headers: any mapping with case-insensitive .get
    (Starlette's request.headers is one).
    """
    if path is None:
        return None
    get = headers.get
    user, base, route, when, sig = (get(h) for h in HEADERS)
    if not (user and base is not None and route in ROUTES and when and sig):
        return None
    try:
        t = int(when)
    except ValueError:
        return None
    if abs((now if now is not None else time.time()) - t) > max_skew:
        return None
    want = _mac(secret, SCHEMA, when, method.upper(), path, user, base, route)
    if not hmac.compare_digest(want.encode("ascii"), str(sig).encode("ascii", "replace")):
        return None
    return {"user": user, "base": base, "route": route}


# ---------- one-time PIN confirmations ----------

def make_confirm(secret, app, action, now=None, ttl=CONFIRM_SECONDS):
    """A token saying "the owner just entered the PIN for this action in this app"."""
    payload = {"app": app, "action": action, "exp": int((now or time.time()) + ttl), "nonce": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"v1.{body}.{_mac(secret, SCHEMA, 'confirm', body)}"


class ConfirmChecker:
    """Checks confirm tokens and remembers the ones used, so each works once."""

    def __init__(self, secret, app):
        self.secret = secret
        self.app = app
        self._used = {}  # nonce -> expiry
        self._lock = threading.Lock()

    def check(self, token, action, now=None):
        now = now if now is not None else time.time()
        try:
            version, body, sig = str(token).split(".")
            if version != "v1":
                return False
            want = _mac(self.secret, SCHEMA, "confirm", body)
            if not hmac.compare_digest(want.encode("ascii"), sig.encode("ascii", "replace")):
                return False
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return False
        if payload.get("app") != self.app or payload.get("action") != action or payload.get("exp", 0) < now:
            return False
        with self._lock:
            for n in [n for n, exp in self._used.items() if exp < now]:
                del self._used[n]
            if payload["nonce"] in self._used:
                return False
            self._used[payload["nonce"]] = payload["exp"]
        return True


# ---------- the link file (the shared secret) ----------

def new_secret():
    return secrets.token_bytes(32)


def _protect(data):
    if sys.platform == "win32":
        from . import dpapi
        return "dpapi:" + _b64(dpapi.protect(data, ENTROPY))
    return "plain:" + _b64(data)


def _unprotect(text):
    kind, _, value = str(text).partition(":")
    if kind == "dpapi":
        from . import dpapi
        return dpapi.unprotect(_unb64(value), ENTROPY)
    if kind == "plain":
        return _unb64(value)
    raise ValueError("not a link secret")


def write_link(path, secret, app, lock_down=None):
    """Write an app's link file. lock_down(path) restricts it to the owner (icacls / chmod)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"schema": SCHEMA, "app": app, "secret": _protect(secret)}, f)
    os.replace(tmp, path)
    if lock_down:
        lock_down(path)
    return path


def read_link(path, app=None):
    """The shared secret from a link file. Raises ValueError with a plain reason (never the secret)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ValueError("the Bridge link file is missing or unreadable") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or (app and data.get("app") != app):
        raise ValueError("the Bridge link file is for something else")
    try:
        return _unprotect(data["secret"])
    except (KeyError, OSError, ValueError):
        raise ValueError("the Bridge link file can't be read by this account") from None
