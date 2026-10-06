"""Smoke test against a real Idea Machine database: set LATTICE_DATABASE_URL (role lattice_app)."""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lattice import data, server

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


@pytest.fixture
def url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def test_page_graph_and_idea(url):
    status, page = get(url + "/")
    assert status == 200 and b"<title>Lattice</title>" in page
    g = json.loads(get(url + "/api/graph")[1])
    assert g["nodes"] and {"id", "title", "themes", "evidence", "origin"} <= set(g["nodes"][0])
    ids = {n["id"] for n in g["nodes"]}
    assert all(link["source"] in ids and link["target"] in ids for link in g["links"])
    one = json.loads(get(url + f"/api/idea/{g['nodes'][0]['id']}")[1])
    assert one["title"] == g["nodes"][0]["title"]


def test_bad_ids_and_paths(url):
    for path, code in [("/api/idea/not-a-uuid", 400), ("/api/idea/00000000-0000-0000-0000-000000000000", 404),
                       ("/etc/passwd", 404)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(url + path)
        assert e.value.code == code


def test_the_role_cannot_read_idea_machines_tables():
    import psycopg

    with data.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT 1 FROM im.items")
