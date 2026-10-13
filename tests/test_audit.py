"""Lower-touch review: a few spot checks across Idea Machine's confidence, the rest kept in bulk (app and MCP).
The database tests need LATTICE_DATABASE_URL, and IM_DEV_ADMIN_URL to delete the events they append."""

import json
import os

import pytest

from lattice import data
from test_mcp import tool
from test_review import admin, post  # noqa: F401  (admin is a fixture)
from test_server import login, request, url  # noqa: F401  (url is a fixture)

needs_db = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def items(confidences):
    return [{"id": f"{n:08d}-0000-0000-0000-000000000000", "confidence": c} for n, c in enumerate(confidences)]


def test_spot_checks_span_the_confidence_scale():
    queue = items([i / 100 for i in range(100)])
    picked = data.audit(queue)
    conf = {i["id"]: i["confidence"] for i in queue}
    assert len(picked) == 5 and len(set(picked)) == 5
    assert [int(conf[p] * 5) for p in picked] == [0, 1, 2, 3, 4]   # one per band, lowest first
    assert data.audit(list(reversed(queue))) == picked              # the same on every device
    # An item stays picked when others leave the queue (here: the first item of each other band).
    assert set(picked) & set(data.audit([i for i in queue if i["id"] not in {queue[25]["id"], queue[65]["id"]}]))
    assert data.audit(items([0.9, 0.2])) == [items([0.9, 0.2])[1]["id"], items([0.9, 0.2])[0]["id"]]   # few: all


@needs_db
def test_the_snapshot_names_the_spot_checks(url):
    review = json.loads(request(url + "/api/snapshot", login(url))[2])["review"]
    ids = {i["id"] for i in review["items"]}
    assert set(review["audit"]) <= ids and len(review["audit"]) == min(5, len(ids))


@needs_db
def test_bulk_keeps_and_spot_checks_are_flagged(url, admin):
    review = json.loads(request(url + "/api/review", login(url))[2])
    rest = [i["id"] for i in review["items"] if i["id"] not in review["audit"]]
    if not rest:
        pytest.skip("no more than the spot checks waiting in this database")
    made = []
    try:
        for event, payload in [({"kind": "item_keep", "item_id": rest[0], "bulk": True}, {"bulk": True}),
                               ({"kind": "item_discard", "item_id": review["audit"][0], "audit": True}, {"audit": True}),
                               ({"kind": "item_star", "item_id": rest[0], "audit": "yes"}, {})]:
            status, body = post(url + "/api/feedback", login(url), event)
            assert status == 201
            made.append(body["event_id"])
            assert admin.execute("SELECT payload FROM pub.feedback_events WHERE event_id = %s",
                                 (body["event_id"],)).fetchone()[0] == payload
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = ANY(%s)", (made,))


@needs_db
def test_review_by_conversation(url, admin):
    names = {n for (n,) in admin.execute("SELECT DISTINCT speaker_label FROM im.source_segments")} - {"me", "unknown"}
    result = tool(url, "review_digest")
    digest = result["structuredContent"]
    if not digest["spot_checks"]:
        pytest.skip("nothing waiting for review in this database")
    text = result["content"][0]["text"]
    assert not any(f'"{n}' in text or f"{n}:" in text for n in names if len(n) > 2)   # speakers are pseudonymized
    checks = [c["item_id"] for c in digest["spot_checks"]]
    confidences = [c["confidence"] for c in digest["spot_checks"]]
    assert confidences == sorted(confidences) and digest["rest"]["count"] == len(digest["rest"]["items"])
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
        assert tool(url, "decide_item", item_id=checks[0], verdict="maybe")["isError"]
        assert not tool(url, "decide_item", item_id=checks[0], verdict="keep", spot_check=True).get("isError")
        rest = [i["item_id"] for i in digest["rest"]["items"]][:2]
        kept = tool(url, "keep_rest", item_ids=rest + checks[1:2])["structuredContent"]
        assert kept["kept"] == len(rest) and kept["skipped_not_waiting_or_spot_checks"] == checks[1:2]
        rows = admin.execute("SELECT kind, payload, source FROM pub.feedback_events WHERE event_id > %s ORDER BY 1",
                             (before,)).fetchall()
        assert ("item_keep", {"audit": True}, "mcp") in rows
        assert rows.count(("item_keep", {"bulk": True}, "mcp")) == len(rest)
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND source = 'mcp'", (before,))


@needs_db
def test_evidence_says_how_its_verdict_was_made():
    with data.connect() as conn:
        evidence = [ev for evs in data.evidence_of(conn).values() for ev in evs]
    assert evidence and all(ev["how"] in (None, "one", "audit", "bulk") for ev in evidence)
    assert all(ev["how"] is None for ev in evidence if ev["verdict"] is None)
