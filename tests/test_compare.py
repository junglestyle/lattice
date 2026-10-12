"""Which of two ideas is better? (idea_compare events). Against a real Idea Machine database; writing needs
IM_DEV_ADMIN_URL too, to delete the events. Until Idea Machine adds the kind, a comparison is refused with 503,
which keeps it in the app's queue."""

import json
import os
import random

import pytest

from lattice import data
from test_mcp import tool
from test_review import admin, post  # noqa: F401  (admin is a fixture)
from test_server import login, request, url  # noqa: F401  (url is a fixture)

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def test_a_pair_is_two_current_ideas_in_either_order():
    with data.connect() as conn:
        ideas = {str(i) for (i,) in conn.execute("SELECT idea_id FROM pub.ideas")}
        firsts = set()
        for seed in range(8):
            pair = data.next_pair(conn, random.Random(seed))
            assert pair and pair["first"] != pair["second"] and {pair["first"], pair["second"]} <= ideas
            assert pair["reason"]
            firsts.add(pair["first"])
        assert len(firsts) > 1


def test_comparisons_are_checked_then_recorded(url, admin):
    cookie = login(url)
    a, b = [str(i) for (i,) in admin.execute("SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 2")]
    for bad in [{"kind": "idea_compare", "idea_id": a, "other_idea_id": b},
                {"kind": "idea_compare", "idea_id": a, "other_idea_id": b, "winner": "both"},
                {"kind": "idea_compare", "idea_id": a, "other_idea_id": a, "winner": "tie"}]:
        assert post(url + "/api/feedback", cookie, bad)[0] == 400, bad
    status, body = post(url + "/api/feedback", cookie, {"kind": "idea_compare", "idea_id": a, "other_idea_id": b,
                                                       "winner": "second", "note": " clearer "})
    if status == 503:
        pytest.skip("Idea Machine doesn't accept idea_compare yet (refused with 503, so the app keeps it queued)")
    try:
        assert status == 201
        assert admin.execute("SELECT idea_id::text, other_idea_id::text, payload FROM pub.feedback_events "
                             "WHERE event_id = %s", (body["event_id"],)).fetchone() == (a, b, {"winner": "second",
                                                                                             "note": "clearer"})
        snap = json.loads(request(url + "/api/snapshot", cookie)[2])
        assert sorted([a, b]) in snap["compared"]
        with data.connect() as conn:
            for seed in range(20):
                pair = data.next_pair(conn, random.Random(seed))
                assert pair is None or {pair["first"], pair["second"]} != {a, b}
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id = %s", (body.get("event_id"),))


def test_comparing_in_conversation(url, admin):
    asked = tool(url, "compare_pair")["structuredContent"]
    assert asked["first"]["title"] and asked["second"]["title"] and "record_comparison" in asked["ask"]
    assert tool(url, "record_comparison", first_idea_id=asked["first"]["id"], second_idea_id=asked["second"]["id"],
                winner="maybe")["isError"]
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
        result = tool(url, "record_comparison", first_idea_id=asked["first"]["id"],
                      second_idea_id=asked["second"]["id"], winner="first")
        if result.get("isError"):
            assert "doesn't accept" in result["content"][0]["text"]
        else:
            assert admin.execute("SELECT kind, source FROM pub.feedback_events WHERE event_id > %s",
                                 (before,)).fetchall() == [("idea_compare", "mcp")]
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND kind = 'idea_compare'", (before,))
