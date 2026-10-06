"""Token login: one shared token (LATTICE_TOKEN), exchanged once per device for a signed cookie.

The cookie carries no secret: it's an HMAC of a fixed message under the token, so changing the token logs every
device out. It's HttpOnly and SameSite=Strict, and Secure when the request came in over HTTPS (tailscale serve).
"""

import hashlib
import hmac
import os

COOKIE = "lattice_session"
MAX_AGE = 365 * 24 * 3600
PUBLIC = {"/login", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"}


def token() -> str:
    t = os.environ.get("LATTICE_TOKEN", "")
    if len(t) < 16:
        raise SystemExit("LATTICE_TOKEN is not set, or shorter than 16 characters")
    return t


def session_value() -> str:
    return hmac.new(token().encode(), b"lattice-session-v1", hashlib.sha256).hexdigest()


def token_ok(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), token().encode())


def cookie_ok(header: str | None) -> bool:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and hmac.compare_digest(value.encode(), session_value().encode()):
            return True
    return False


def set_cookie(secure: bool) -> str:
    return (f"{COOKIE}={session_value()}; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def clear_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
