"""Token login: one shared token (LATTICE_TOKEN), exchanged once per device for a signed cookie.

The cookie carries no secret: it's an HMAC of a fixed message under the token, so changing the token logs every
device out. It's HttpOnly and SameSite=Strict, and Secure when the request came in over HTTPS (tailscale serve).
"""

import hashlib
import hmac
import ipaddress
import os

COOKIE = "lattice_session"
MAX_AGE = 365 * 24 * 3600
PUBLIC = {"/login", "/logout", "/sw.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
          "/apple-touch-icon.png"}


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


def bearer_ok(header: str | None) -> bool:
    """`Authorization: Bearer <LATTICE_TOKEN>`, for the MCP endpoint (a client there has no cookie)."""
    scheme, _, value = (header or "").partition(" ")
    return scheme.lower() == "bearer" and token_ok(value.strip())


# Anthropic's outbound range for MCP calls to external servers (platform.claude.com/docs/en/api/ip-addresses).
ANTHROPIC_OUTBOUND = "160.79.104.0/21"


def mcp_secret() -> str | None:
    """LATTICE_MCP_SECRET: the path secret of the public MCP listener. Unset, there is no public listener."""
    s = os.environ.get("LATTICE_MCP_SECRET", "")
    if not s:
        return None
    if len(s) < 32 or not s.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("LATTICE_MCP_SECRET must be at least 32 letters, digits, - or _ (e.g. openssl rand -hex 24)")
    return s


def mcp_allow() -> list:
    try:
        return [ipaddress.ip_network(n.strip()) for n in
                os.environ.get("LATTICE_MCP_ALLOW", ANTHROPIC_OUTBOUND).split(",") if n.strip()]
    except ValueError as e:
        raise SystemExit(f"LATTICE_MCP_ALLOW: {e}") from None


def mcp_path_ok(path: str) -> bool:
    secret = mcp_secret()
    return secret is not None and hmac.compare_digest(path.encode(), f"/mcp/{secret}".encode())


def mcp_source_ok(address: str | None) -> bool:
    """The client address Cloudflare reports. Missing means the request didn't come through the tunnel."""
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in mcp_allow())


def set_cookie(secure: bool) -> str:
    return (f"{COOKIE}={session_value()}; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def clear_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
