"""Which of two ideas is better? (idea_compare events). Against a real Idea Machine database; writing needs
IM_DEV_ADMIN_URL too, to delete the events."""

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
        assert not result.get("isError"), result
        assert admin.execute("SELECT kind, source FROM pub.feedback_events WHERE event_id > %s",
                             (before,)).fetchall() == [("idea_compare", "mcp")]
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND kind = 'idea_compare'", (before,))


def test_only_an_unknown_kind_is_worth_retrying(monkeypatch):
    """A kind Idea Machine hasn't deployed is NotYet (503, stays queued); any other check it fails is a bad event
    (400, dropped). Provoked by letting Lattice send what its own checks would normally stop."""
    with data.connect() as conn:
        a, b = [str(i) for (i,) in conn.execute("SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 2")]
        monkeypatch.setitem(data.EVENTS, "not_deployed_yet", "idea_id")
        with pytest.raises(data.NotYet):
            data.record(conn, {"kind": "not_deployed_yet", "idea_id": a})
        monkeypatch.setattr(data, "WINNERS", ("first", "second", "tie", "both"))
        with pytest.raises(ValueError, match="feedback_events_compare"):
            data.record(conn, {"kind": "idea_compare", "idea_id": a, "other_idea_id": b, "winner": "both"})
