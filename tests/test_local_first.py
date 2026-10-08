"""Slice 5: the copy the app keeps on the device, links I make, and logging out. Against a real Idea Machine database
(LATTICE_DATABASE_URL); the link test needs IM_DEV_ADMIN_URL too, to delete its events."""

import json
import os

import pytest

from test_review import admin, post  # noqa: F401  (admin is a fixture)
from test_server import login, request, url  # noqa: F401  (url is a fixture)

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def test_the_snapshot_holds_everything_the_app_shows(url):
    cookie = login(url)
    assert request(url + "/api/snapshot")[0] == 401
    snap = json.loads(request(url + "/api/snapshot", cookie)[2])
    assert {"graph", "ideas", "review", "status"} <= set(snap)
    nodes = snap["graph"]["nodes"]
    assert {n["id"] for n in nodes} == set(snap["ideas"])
    assert all(n["created_at"] for n in nodes)
    for n in nodes:
        assert n["pos"] is None or (len(n["pos"]) == 2 and all(-2 < v < 2 for v in n["pos"]))
    for d in snap["ideas"].values():
        assert all(ev["item_id"] and ev["quote"] for ev in d["evidence"])
    # The page itself carries no data, so the service worker can keep it.
    page = request(url + "/", cookie)[2]
    assert b"/*GRAPH*/" not in page and b"/api/snapshot" in page


def test_a_link_shows_at_once_and_an_unlink_removes_it(url, admin):
    cookie = login(url)
    a, b = [str(i) for (i,) in admin.execute("SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 2")]
    for bad in [{"kind": "link", "idea_id": a}, {"kind": "link", "idea_id": a, "other_idea_id": a},
                {"kind": "unlink", "idea_id": a, "other_idea_id": "nope"}]:
        assert post(url + "/api/feedback", cookie, bad)[0] == 400, bad
    made = []

    def mine():
        links = json.loads(request(url + "/api/snapshot", cookie)[2])["graph"]["links"]
        return [link for link in links if link["kind"] == "mine" and {link["source"], link["target"]} == {a, b}]

    try:
        for event, expect in [({"kind": "link", "idea_id": b, "other_idea_id": a}, 1),
                              ({"kind": "unlink", "idea_id": a, "other_idea_id": b}, 0)]:
            status, body = post(url + "/api/feedback", cookie, event)
            assert status == 201
            made.append(body["event_id"])
            assert len(mine()) == expect, event
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = ANY(%s)", (made,))


def test_the_service_worker_is_served_from_the_root(url):
    status, headers, body = request(url + "/sw.js")
    assert status == 200 and "javascript" in headers["Content-Type"] and b"/api/" in body


def test_logging_out_clears_the_device(url):
    cookie = login(url)
    status, headers, body = request(url + "/logout", cookie)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert headers["Clear-Site-Data"] == '"cache", "storage"'
    assert b'deleteDatabase("lattice")' in body
