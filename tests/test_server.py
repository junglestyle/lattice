"""Against a real Idea Machine database: set LATTICE_DATABASE_URL (role lattice_app)."""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lattice import auth, data, server

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")
TOKEN = "test-token-0123456789abcdef"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def url(monkeypatch):
    monkeypatch.setenv("LATTICE_TOKEN", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def request(url, cookie=None, form=None):
    """(status, headers, body), without following redirects."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode() if form else None,
                                 headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def login(url):
    status, headers, _ = request(url + "/login", form={"token": TOKEN})
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie  # plain http locally
    return cookie.split(";")[0]


def test_everything_but_login_and_icons_needs_a_session(url):
    assert request(url + "/")[0:2][0] == 303
    assert request(url + "/api/graph")[0] == 401
    assert request(url + "/login", form={"token": "wrong"})[0] == 401
    for path in ["/login", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"]:
        assert request(url + path)[0] == 200, path
    assert json.loads(request(url + "/manifest.webmanifest")[2])["display"] == "standalone"
    assert request(url + "/apple-touch-icon.png")[2][:8] == b"\x89PNG\r\n\x1a\n"


def test_page_graph_and_idea_after_login(url):
    cookie = login(url)
    status, _, page = request(url + "/", cookie)
    assert status == 200 and b"<title>Lattice</title>" in page and b'rel="manifest"' in page
    g = json.loads(request(url + "/api/graph", cookie)[2])
    ids = {n["id"] for n in g["nodes"]}
    assert g["nodes"] and all(link["source"] in ids and link["target"] in ids for link in g["links"])
    one = json.loads(request(url + f"/api/idea/{g['nodes'][0]['id']}", cookie)[2])
    assert one["title"] == g["nodes"][0]["title"]
    assert request(url + "/api/idea/not-a-uuid", cookie)[0] == 400
    assert request(url + "/api/idea/00000000-0000-0000-0000-000000000000", cookie)[0] == 404


def test_a_forged_or_old_cookie_is_refused(url, monkeypatch):
    cookie = login(url)
    assert request(url + "/api/graph", "lattice_session=" + "0" * 64)[0] == 401
    monkeypatch.setenv("LATTICE_TOKEN", TOKEN + "-rotated")  # changing the token logs every device out
    assert request(url + "/api/graph", cookie)[0] == 401


def test_the_role_cannot_read_idea_machines_tables():
    import psycopg

    with data.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT 1 FROM im.items")


def test_a_short_token_is_refused(monkeypatch):
    monkeypatch.setenv("LATTICE_TOKEN", "short")
    with pytest.raises(SystemExit):
        auth.token()
