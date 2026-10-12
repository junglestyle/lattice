"""The public MCP listener: the MCP endpoint alone, behind a path secret and Cloudflare's client address."""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lattice import auth, server

SECRET = "s" * 40
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("LATTICE_TOKEN", "test-token-0123456789abcdef")
    monkeypatch.setenv("LATTICE_MCP_SECRET", SECRET)
    monkeypatch.delenv("LATTICE_MCP_ALLOW", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PublicMcpHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def send(base, path, ip="160.79.105.17", body=PING, method="POST", headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if method == "POST" else None,
                                 method=method, headers={"Content-Type": "application/json",
                                                         **({"CF-Connecting-IP": ip} if ip else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_secret_path_from_anthropics_range_gets_in(public):
    status, body = send(public, f"/mcp/{SECRET}")
    assert status == 200 and json.loads(body) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert send(public, f"/mcp/{SECRET}", method="GET")[0] == 405


def test_everything_else_is_a_bare_404(public):
    for path, ip, extra in [(f"/mcp/{SECRET}", "8.8.8.8", {}),        # right secret, wrong address
                            (f"/mcp/{SECRET}", None, {}),             # not through the tunnel
                            (f"/mcp/{SECRET}", "not an ip", {}),
                            (f"/mcp/{SECRET[:-1]}", "160.79.105.17", {}),
                            ("/mcp", "160.79.105.17", {"Authorization": "Bearer test-token-0123456789abcdef"}),
                            ("/", "160.79.105.17", {}), ("/api/snapshot", "160.79.105.17", {}),
                            ("/login", "160.79.105.17", {})]:
        for method in ("GET", "POST"):
            assert send(public, path, ip, method=method, headers=extra) == (404, b""), (path, ip, method)
    assert send(public, f"/mcp/{SECRET}", method="DELETE") == (404, b"")


def test_a_foreign_origin_is_refused_even_with_the_secret(public):
    assert send(public, f"/mcp/{SECRET}", headers={"Origin": "https://evil.example"})[0] == 403


def test_the_allow_list_and_secret_are_checked(monkeypatch):
    monkeypatch.setenv("LATTICE_MCP_ALLOW", "10.0.0.0/8, 2607:6bc0::/48")
    assert auth.mcp_source_ok("10.1.2.3") and auth.mcp_source_ok("2607:6bc0::1") and not auth.mcp_source_ok("160.79.105.17")
    monkeypatch.setenv("LATTICE_MCP_ALLOW", "nonsense")
    with pytest.raises(SystemExit):
        auth.mcp_allow()
    for bad in ["short", "x" * 31, "y" * 40 + "/../"]:
        monkeypatch.setenv("LATTICE_MCP_SECRET", bad)
        with pytest.raises(SystemExit):
            auth.mcp_secret()
    monkeypatch.delenv("LATTICE_MCP_SECRET")
    assert auth.mcp_secret() is None and not auth.mcp_path_ok("/mcp/")


@pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")
def test_tools_work_through_it(public):
    status, body = send(public, f"/mcp/{SECRET}", body={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                        "params": {"name": "list_themes", "arguments": {}}})
    assert status == 200 and json.loads(body)["result"]["structuredContent"]["themes"]
