"""The public MCP listener with OAuth in front: the whole sign-in as claude.ai does it, and what's refused."""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lattice import auth, oauth, server

TOKEN = "test-token-0123456789abcdef"
BASE = "https://lattice-mcp.example.com"
CLAUDE_IP = "160.79.105.17"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("LATTICE_TOKEN", TOKEN)
    monkeypatch.setenv("LATTICE_PUBLIC_URL", BASE)
    monkeypatch.delenv("LATTICE_MCP_ALLOW", raising=False)
    oauth._failures.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PublicMcpHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(base, path, method="GET", ip=CLAUDE_IP, json_body=None, form=None, headers=None):
    """(status, headers, body) without following redirects."""
    data, kind = None, None
    if json_body is not None:
        data, kind = json.dumps(json_body).encode(), "application/json"
    elif form is not None:
        data, kind = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={**({"Content-Type": kind} if kind else {}),
                                          **({"CF-Connecting-IP": ip} if ip else {}), **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def sign_in(base, writes=True, scope="lattice:read lattice:write", path="/mcp"):
    """Register, consent and trade the code, as claude.ai does. Returns the token response."""
    status, _, body = call(base, "/register", "POST", json_body={"redirect_uris": [CALLBACK], "client_name": "Claude",
                                                                 "token_endpoint_auth_method": "none"})
    assert status == 201, body
    client_id = json.loads(body)["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                                "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
                                "scope": scope, "resource": BASE + path})
    status, headers, page = call(base, f"/authorize?{q}", ip="203.0.113.9")   # my browser, anywhere
    assert status == 200 and headers["X-Frame-Options"] == "DENY" and b"claude.ai" in page
    assert (b"demo connector" in page) == (path == "/demo/mcp") and (b'name="writes"' in page) == (path != "/demo/mcp")
    request = re.search(rb'name="request" value="([^"]+)"', page).group(1).decode()
    form = {"request": request, "token": TOKEN, **({"writes": "on"} if writes else {})}
    status, headers, _ = call(base, "/authorize", "POST", ip="203.0.113.9", form=form)
    assert status == 303
    back = urllib.parse.urlsplit(headers["Location"])
    params = dict(urllib.parse.parse_qsl(back.query))
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK and params["state"] == "st8" and params["iss"] == BASE
    status, headers, body = call(base, "/token", "POST", form={"grant_type": "authorization_code", "code": params["code"],
                                                               "redirect_uri": CALLBACK, "client_id": client_id,
                                                               "code_verifier": verifier, "resource": BASE + path})
    assert status == 200, body
    tokens = json.loads(body) | {"client_id": client_id, "_code": params["code"], "_verifier": verifier}
    return tokens


def mcp(base, access, message, path="/mcp"):
    status, _, body = call(base, path, "POST", json_body=message, headers={"Authorization": f"Bearer {access}"})
    return status, json.loads(body) if body else None


def test_discovery_starts_from_a_401(public):
    status, headers, _ = call(public, "/mcp", "POST", json_body=PING)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith(f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource"')
    prm = json.loads(call(public, "/.well-known/oauth-protected-resource")[2])
    assert prm["resource"] == BASE + "/mcp" and prm["authorization_servers"] == [BASE]
    asm = json.loads(call(public, "/.well-known/oauth-authorization-server")[2])
    assert asm["issuer"] == BASE and asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"] == BASE + "/register" and "none" in asm["token_endpoint_auth_methods_supported"]


def test_the_whole_sign_in_then_tools(public):
    t = sign_in(public)
    assert t["token_type"] == "Bearer" and t["scope"] == "lattice:read lattice:write"
    assert mcp(public, t["access_token"], PING) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})
    tools = {x["name"] for x in mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
    assert "star_idea" in tools
    # A code works once.
    status, _, body = call(public, "/token", "POST", form={"grant_type": "authorization_code", "code": t["_code"],
                                                           "redirect_uri": CALLBACK, "client_id": t["client_id"],
                                                           "code_verifier": t["_verifier"]})
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_a_read_only_grant_has_no_write_tools(public):
    t = sign_in(public, writes=False)
    assert t["scope"] == "lattice:read"
    tools = {x["name"] for x in mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
    assert "search_ideas" in tools and not tools & {"star_idea", "link_ideas", "note_idea"}
    _, body = mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                              "params": {"name": "star_idea", "arguments": {"idea_id": "x"}}})
    assert body["error"]["code"] == -32602


def test_refresh_tokens_rotate_and_a_reused_one_revokes_the_family(public):
    t = sign_in(public)
    refresh = {"grant_type": "refresh_token", "client_id": t["client_id"]}
    status, _, body = call(public, "/token", "POST", form=refresh | {"refresh_token": t["refresh_token"]})
    assert status == 200
    second = json.loads(body)
    assert second["refresh_token"] != t["refresh_token"] and mcp(public, second["access_token"], PING)[0] == 200
    # The first refresh token again: refused, and the family with it.
    for old in (t["refresh_token"], second["refresh_token"]):
        status, _, body = call(public, "/token", "POST", form=refresh | {"refresh_token": old})
        assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_pkce_and_redirects_are_enforced(public):
    status, _, body = call(public, "/register", "POST", json_body={"redirect_uris": ["https://evil.example/cb"]})
    assert status == 400 and json.loads(body)["error"] == "invalid_redirect_uri"
    client_id = json.loads(call(public, "/register", "POST", json_body={"redirect_uris": [CALLBACK]})[2])["client_id"]
    base_q = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s"}
    # No PKCE: sent back to Claude with an error.
    status, headers, _ = call(public, "/authorize?" + urllib.parse.urlencode(base_q), ip=None)
    assert status == 303 and "error=invalid_request" in headers["Location"]
    # A redirect the client didn't register, or a forged client: shown, never redirected.
    for q in [base_q | {"redirect_uri": "https://evil.example/cb"}, base_q | {"client_id": client_id[:-2] + "xx"}]:
        assert call(public, "/authorize?" + urllib.parse.urlencode(q), ip=None)[0] in (400, 401)


def test_wrong_tokens_are_capped(public):
    client_id = json.loads(call(public, "/register", "POST", json_body={"redirect_uris": [CALLBACK]})[2])["client_id"]
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                                "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    page = call(public, f"/authorize?{q}", ip=None)[2]
    request = re.search(rb'name="request" value="([^"]+)"', page).group(1).decode()
    statuses = [call(public, "/authorize", "POST", ip=None, form={"request": request, "token": "wrong"})[0]
                for _ in range(oauth.LOGIN_FAILURES_PER_HOUR + 1)]
    assert statuses[:-1] == [401] * oauth.LOGIN_FAILURES_PER_HOUR and statuses[-1] == 429
    # Even the right token waits now.
    assert call(public, "/authorize", "POST", ip=None, form={"request": request, "token": TOKEN})[0] == 429


def test_only_claude_reaches_anything_but_the_sign_in_page(public):
    t = sign_in(public)
    auth_header = {"Authorization": f"Bearer {t['access_token']}"}
    for ip in ("8.8.8.8", None, "not an ip"):
        for path, method in [("/mcp", "POST"), ("/token", "POST"), ("/register", "POST"),
                             ("/.well-known/oauth-authorization-server", "GET")]:
            status, _, body = call(public, path, method, ip=ip, json_body={} if method == "POST" else None, headers=auth_header)
            assert (status, body) == (404, b""), (path, ip)
    for path in ("/", "/api/snapshot", "/login", "/logout", "/sw.js"):
        assert call(public, path)[0] == 404, path
    # The tailnet's bearer token means nothing here.
    assert mcp(public, TOKEN, PING)[0] == 401


def test_changing_the_token_revokes_every_grant(public, monkeypatch):
    t = sign_in(public)
    monkeypatch.setenv("LATTICE_TOKEN", TOKEN + "-rotated")
    assert mcp(public, t["access_token"], PING)[0] == 401


def test_settings_are_checked(monkeypatch):
    monkeypatch.setenv("LATTICE_MCP_ALLOW", "10.0.0.0/8, 2607:6bc0::/48")
    assert auth.mcp_source_ok("10.1.2.3") and auth.mcp_source_ok("2607:6bc0::1") and not auth.mcp_source_ok(CLAUDE_IP)
    monkeypatch.setenv("LATTICE_MCP_ALLOW", "nonsense")
    with pytest.raises(SystemExit):
        auth.mcp_allow()
    for bad in ["http://lattice.example.com", "https://lattice.example.com/mcp", "lattice.example.com"]:
        monkeypatch.setenv("LATTICE_PUBLIC_URL", bad)
        with pytest.raises(SystemExit):
            auth.public_url()
    monkeypatch.setenv("LATTICE_PUBLIC_URL", "https://lattice.example.com/")
    assert auth.public_url() == "https://lattice.example.com"
    monkeypatch.delenv("LATTICE_PUBLIC_URL")
    assert auth.public_url() is None


@pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")
def test_tools_work_through_it(public):
    t = sign_in(public)
    _, body = mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "list_themes", "arguments": {}}})
    assert body["result"]["structuredContent"]["themes"]


def test_a_demo_connector_is_its_own_read_only_resource(public):
    prm = json.loads(call(public, "/.well-known/oauth-protected-resource/demo/mcp")[2])
    assert prm["resource"] == BASE + "/demo/mcp" and prm["scopes_supported"] == ["lattice:read"]
    status, headers, _ = call(public, "/demo/mcp", "POST", json_body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 401 and "/.well-known/oauth-protected-resource/demo/mcp" in headers["WWW-Authenticate"]
    demo = sign_in(public, writes=True, path="/demo/mcp")   # asking for writes doesn't get them
    assert demo["scope"] == "lattice:read"
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert mcp(public, demo["access_token"], ping, "/demo/mcp")[0] == 200
    assert mcp(public, demo["access_token"], ping)[0] == 401   # a demo token never reaches the lattice itself
    full = sign_in(public)
    assert mcp(public, full["access_token"], ping, "/demo/mcp")[0] == 200   # a full one may show less
    refreshed = json.loads(call(public, "/token", "POST", form={
        "grant_type": "refresh_token", "refresh_token": demo["refresh_token"], "client_id": demo["client_id"]})[2])
    assert mcp(public, refreshed["access_token"], ping)[0] == 401   # still bound after a refresh
    status, _, body = call(public, "/token", "POST", form={
        "grant_type": "refresh_token", "refresh_token": refreshed["refresh_token"], "client_id": demo["client_id"],
        "resource": BASE + "/mcp"})
    assert status == 400 and json.loads(body)["error"] == "invalid_target"
