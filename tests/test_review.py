"""The review queue and the feedback endpoint, against a real Idea Machine database (LATTICE_DATABASE_URL).

Tests that write need IM_DEV_ADMIN_URL too, to delete the events they appended (lattice_app can only insert).
"""

import json
import os
import uuid

import psycopg
import pytest

from test_server import TOKEN, login, request, url  # noqa: F401  (url is a fixture)

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def post(url, cookie, body, content_type="application/json"):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": content_type, **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def admin():
    if "IM_DEV_ADMIN_URL" not in os.environ:
        pytest.skip("needs IM_DEV_ADMIN_URL to clean up")
    dsn = os.environ["IM_DEV_ADMIN_URL"].rsplit("/", 1)[0] + "/" + os.environ["LATTICE_DATABASE_URL"].rsplit("/", 1)[1]
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def test_review_queue_has_context(url):
    cookie = login(url)
    status, _, body = request(url + "/api/review", cookie)
    review = json.loads(body)
    assert status == 200 and review["total"] >= len(review["items"])
    for item in review["items"][:5]:
        assert item["quote"] and item["context"] and any(line["source"] for line in item["context"])


def test_feedback_is_checked_then_appended(url, admin):
    cookie = login(url)
    assert post(url + "/api/feedback", None, {"kind": "star", "idea_id": str(uuid.uuid4())})[0] == 401
    assert post(url + "/api/feedback", cookie, {"kind": "star"}, "application/x-www-form-urlencoded")[0] == 415
    for bad in [{"kind": "drop_table"}, {"kind": "star", "idea_id": "1; select"}, {"kind": "item_keep"},
                {"kind": "pin_theme", "idea_id": str(uuid.uuid4())}]:
        assert post(url + "/api/feedback", cookie, bad)[0] == 400, bad
    (idea,) = admin.execute("SELECT idea_id FROM pub.ideas LIMIT 1").fetchone()
    status, body = post(url + "/api/feedback", cookie, {"kind": "star", "idea_id": str(idea)})
    try:
        assert status == 201
        assert admin.execute("SELECT kind, idea_id, source FROM pub.feedback_events WHERE event_id = %s",
                             (body["event_id"],)).fetchone() == ("star", idea, "lattice")
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = %s", (body.get("event_id"),))


def test_a_discard_hides_the_capture_and_keeps_its_note(url, admin):
    cookie = login(url)
    review = json.loads(request(url + "/api/review", cookie)[2])
    if not review["items"]:
        pytest.skip("nothing waiting for review in this database")
    item = review["items"][0]["id"]
    status, body = post(url + "/api/feedback", cookie, {"kind": "item_discard", "item_id": item, "note": " garbled "})
    try:
        assert status == 201
        after = json.loads(request(url + "/api/review", cookie)[2])
        assert item not in [i["id"] for i in after["items"]] and after["total"] == review["total"] - 1
        assert admin.execute("SELECT payload FROM pub.feedback_events WHERE event_id = %s",
                             (body["event_id"],)).fetchone()[0] == {"note": "garbled"}
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = %s", (body.get("event_id"),))


def test_correcting_an_idea_shows_at_once_and_keeps_claudes_version(url, admin):
    cookie = login(url)
    idea, title, statement = admin.execute(
        "SELECT idea_id, title, statement FROM pub.ideas WHERE origin = 'captured' AND NOT corrected LIMIT 1").fetchone()
    assert post(url + "/api/feedback", cookie, {"kind": "idea_correct", "idea_id": str(idea)})[0] == 400   # nothing to set
    made = []
    try:
        for event in [{"kind": "idea_correct", "idea_id": str(idea), "statement": "  What I actually meant.  "},
                      {"kind": "idea_note", "idea_id": str(idea), "note": "Said at the bar, as a joke."}]:
            status, body = post(url + "/api/feedback", cookie, event)
            assert status == 201
            made.append(body["event_id"])
        shown = json.loads(request(url + f"/api/idea/{idea}", cookie)[2])
        assert shown["statement"] == "What I actually meant." and shown["title"] == title
        assert shown["claude_statement"] == statement and shown["corrected"] is True
        assert shown["my_note"] == "Said at the bar, as a joke."
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = ANY(%s)", (made,))


def test_a_keep_can_carry_a_note(url, admin):
    cookie = login(url)
    review = json.loads(request(url + "/api/review", cookie)[2])
    if not review["items"]:
        pytest.skip("nothing waiting for review in this database")
    item = review["items"][0]
    assert item["idea"] is None or {"id", "title", "statement"} <= set(item["idea"])
    status, body = post(url + "/api/feedback", cookie, {"kind": "item_keep", "item_id": item["id"], "note": "connects to frames"})
    try:
        assert status == 201
        assert admin.execute("SELECT payload FROM pub.feedback_events WHERE event_id = %s",
                             (body["event_id"],)).fetchone()[0] == {"note": "connects to frames"}
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = %s", (body.get("event_id"),))
