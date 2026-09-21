"""Admin login and API key, both enabled by ADMIN_PASSWORD. Without it the app is open, as on a trusted LAN.

- The admin logs in with the password and gets a signed session cookie. The signing secret lives in the
  database, so sessions survive a restart; the password's fingerprint is part of the signature, so changing
  the password logs every session out.
- Scripts send the API key as `Authorization: Bearer <key>`. Only its SHA-256 is stored: the key is shown
  once, when generated, and a new one replaces the old at once.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable

SESSION_COOKIE = "redirect_session"
SESSION_SECONDS = 30 * 24 * 3600
API_KEY_PREFIX = "rdk_"
# Characters of the key shown later to tell keys apart (prefix included); far too few to guess the rest
API_KEY_HINT_LENGTH = 8
# Failed logins allowed per client address within the window; then that address waits for the window to pass
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60

# Names in the settings table
SESSION_SECRET = "session_secret"
API_KEY_HASH = "api_key_hash"
API_KEY_HINT = "api_key_hint"
API_KEY_CREATED_AT = "api_key_created_at"


def new_secret() -> str:
    return secrets.token_hex(32)


def new_api_key_body() -> str:
    return secrets.token_urlsafe(32)


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def check_password(given: str, expected: str) -> bool:
    """Constant-time comparison (on digests, so the length of the password does not leak either)."""
    return hmac.compare_digest(_digest(given), _digest(expected))


def _signature(secret: str, password: str, expires: int) -> str:
    message = f"{expires}|".encode() + _digest(password)
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


def make_session(secret: str, password: str, now: float) -> str:
    expires = int(now) + SESSION_SECONDS
    return f"{expires}.{_signature(secret, password, expires)}"


def session_is_valid(token: str, secret: str, password: str, now: float) -> bool:
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or len(expires) > 12:
        return False
    return hmac.compare_digest(signature, _signature(secret, password, int(expires))) and int(expires) > now


def hash_api_key(key: str) -> str:
    """A key carries 256 random bits: a plain SHA-256 is enough, no slow hash needed."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def api_key_matches(given: str, stored_hash: str | None) -> bool:
    return stored_hash is not None and hmac.compare_digest(hash_api_key(given), stored_hash)


def bearer_token(authorization: str) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None."""
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


class LoginThrottle:
    """Counts failed logins per client address; blocks an address after LOGIN_ATTEMPTS within the window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, client: str) -> list[float]:
        horizon = self.clock() - LOGIN_WINDOW_SECONDS
        recent = [moment for moment in self._failures.get(client, []) if moment > horizon]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return recent

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= LOGIN_ATTEMPTS

    def failed(self, client: str) -> None:
        self._failures.setdefault(client, []).append(self.clock())

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)
