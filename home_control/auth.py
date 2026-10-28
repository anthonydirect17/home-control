"""The page PIN.

- The PIN is exactly 6 digits. Only a salted scrypt hash of it is stored, as
  a secret (data/secrets.json or Credential Manager), never the PIN.
- After a correct PIN the browser gets a signed "remember this device"
  cookie, good for 30 days. Changing the PIN signs every device out.
- Arming, disarming and sirens ask for the PIN again every time
  (`check_action_pin`), on top of the page's own confirmation step.
- 5 wrong PINs in a row lock PIN entry for 15 minutes, for everyone.
"""

import base64
import hashlib
import hmac
import json
import secrets as pysecrets
import threading
import time

PIN_LENGTH = 6
REMEMBER_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
COOKIE_NAME = "hc_device"

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class PinError(Exception):
    """Wrong or malformed PIN, or locked out. The message is meant for the page."""

    def __init__(self, message, locked_for=0):
        super().__init__(message)
        self.locked_for = locked_for


def valid_pin_format(pin) -> bool:
    return isinstance(pin, str) and len(pin) == PIN_LENGTH and pin.isdigit()


def hash_pin(pin: str) -> str:
    if not valid_pin_format(pin):
        raise ValueError(f"the PIN must be exactly {PIN_LENGTH} digits")
    salt = pysecrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(salt=salt.hex(), digest=digest.hex(), **_SCRYPT)


def pin_matches(pin: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(str(pin).encode(), salt=bytes.fromhex(salt), dklen=len(bytes.fromhex(digest)),
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, bytes.fromhex(digest))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PinGate:
    """Checks PINs, issues and checks device cookies, and enforces the lockout.

    `pin_hash` is None when no PIN has been set. `required` is False only in
    demo mode with no PIN set, so pretend lights can be tried without one.
    """

    def __init__(self, pin_hash, session_key: bytes, required=True, clock=time.time):
        self.pin_hash = pin_hash
        self.session_key = session_key
        self.required = required or bool(pin_hash)
        self.clock = clock
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    @property
    def pin_set(self):
        return bool(self.pin_hash)

    def _pin_tag(self):
        # Ties cookies to the current PIN, so setting a new PIN signs every device out.
        return hmac.new(self.session_key, (self.pin_hash or "").encode(), hashlib.sha256).hexdigest()[:16]

    def locked_for(self):
        return max(0, int(self._locked_until - self.clock()))

    def check_pin(self, pin):
        """Raise PinError unless pin is right. Counts failures toward the lockout."""
        with self._lock:
            wait = self.locked_for()
            if wait:
                raise PinError(f"Too many wrong PINs. Try again in {wait // 60 + 1} min.", wait)
            if not self.pin_set:
                raise PinError("No PIN has been set. Run: python -m home_control --set-pin")
            if valid_pin_format(pin) and pin_matches(pin, self.pin_hash):
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._failures = 0
                self._locked_until = self.clock() + LOCKOUT_SECONDS
                raise PinError("Too many wrong PINs. PIN entry is locked for 15 min.", LOCKOUT_SECONDS)
            left = MAX_FAILURES - self._failures
            raise PinError(f"Wrong PIN. {left} {'try' if left == 1 else 'tries'} left before a 15 min lock.")

    def issue_cookie(self):
        payload = {"d": pysecrets.token_hex(8), "exp": int(self.clock()) + REMEMBER_SECONDS, "t": self._pin_tag()}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self.session_key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def cookie_ok(self, cookie) -> bool:
        if not self.required:
            return True
        if not cookie or "." not in cookie or not self.pin_set:
            return False
        body, sig = cookie.rsplit(".", 1)
        expected = _b64(hmac.new(self.session_key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            payload = json.loads(_unb64(body))
        except ValueError:
            return False
        return payload.get("exp", 0) > self.clock() and payload.get("t") == self._pin_tag()

    def check_action_pin(self, pin):
        """For arm/disarm/sirens: the PIN is needed every time, even on a remembered device."""
        self.check_pin(pin)


def load_or_create_session_key(get_secret, save_secret, settings):
    """The cookie-signing key lives in the secrets store; create it on first run.

    If it can't be saved (e.g. the store isn't writable right now), use a
    temporary one: everything works, devices just need the PIN again after
    the app restarts.
    """
    value, _ = get_secret("session-key", settings)
    if value:
        try:
            return bytes.fromhex(value), True
        except ValueError:
            pass
    key = pysecrets.token_bytes(32)
    try:
        save_secret("session-key", key.hex(), settings)
        return key, True
    except Exception:
        return key, False
