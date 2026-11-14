"""Session login for the staff desk.

When STAFF_USERNAME and STAFF_PASSWORD are both set, /staff requires a signed
cookie. When either is empty (local simulator), the desk stays open and a
warning is logged at startup. Render should set both.
"""

from __future__ import annotations

import hashlib
import hmac
import time

COOKIE = "staff_session"
_MAX_AGE_SECONDS = 12 * 60 * 60


def auth_enabled(settings) -> bool:
    return bool(settings.staff_username and settings.staff_password)


def session_secret(settings) -> str:
    if settings.staff_session_secret:
        return settings.staff_session_secret
    return settings.staff_password


def credentials_match(settings, username: str, password: str) -> bool:
    return _digest_eq(username, settings.staff_username) and _digest_eq(
        password, settings.staff_password
    )


def issue_session(settings, username: str, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + _MAX_AGE_SECONDS)
    payload = f"{username}|{expires}"
    signature = hmac.new(session_secret(settings).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}"


def read_session(settings, token: str | None, now: float | None = None) -> str | None:
    if not token or token.count("|") != 2:
        return None
    username, expires, signature = token.split("|")
    payload = f"{username}|{expires}"
    expected = hmac.new(session_secret(settings).encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        expiry = int(expires)
    except ValueError:
        return None
    if expiry < int(now if now is not None else time.time()):
        return None
    if not _digest_eq(username, settings.staff_username):
        return None
    return username


def _digest_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode()).digest(),
        hashlib.sha256(right.encode()).digest(),
    )
