"""The MCP endpoint (slice 7). Past the token, against a real Idea Machine database (LATTICE_DATABASE_URL); the
star test needs IM_DEV_ADMIN_URL too, to delete its event."""

import json
import os
import urllib.error
import urllib.request

import pytest

from test_review import admin  # noqa: F401  (a fixture)
from test_server import TOKEN, url  # noqa: F401  (url is a fixture)

needs_db = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def rpc(url, message, token=TOKEN, headers=None):
    req = urllib.request.Request(url + "/mcp", data=json.dumps(message).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                          **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def tool(url, name, **args):
    status, body = rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert status == 200, body
    return body["result"]


def test_it_needs_the_token_and_refuses_browsers(url):
    hello = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert rpc(url, hello, token=None)[0] == 401
    assert rpc(url, hello, token="wrong-token-0123456789")[0] == 401
    assert rpc(url, hello, headers={"Origin": "https://evil.example"})[0] == 403
    assert rpc(url, hello)[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


@needs_db
def test_handshake_and_tools(url):
    status, body = rpc(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "0"}}})
    assert status == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"]["tools"] and body["result"]["instructions"]
    assert rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    names = {t["name"] for t in rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
    assert {"search_ideas", "get_idea", "list_themes", "open_theme", "whats_new", "star_idea", "link_ideas"} <= names
    assert rpc(url, {"jsonrpc": "2.0", "id": 3, "method": "nope"})[1]["error"]["code"] == -32601
    assert rpc(url, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "drop"}})[1]["error"]


@needs_db
def test_reading_the_lattice_never_names_anyone(url, admin):
    names = {n for (n,) in admin.execute("SELECT DISTINCT said_by FROM pub.evidence")} - {"me", "unknown"}
    themes = tool(url, "list_themes")["structuredContent"]["themes"]
    assert themes
    theme = tool(url, "open_theme", theme=themes[0]["name"].upper())["structuredContent"]
    assert theme["name"] == themes[0]["name"] and theme["ideas"]
    found = tool(url, "search_ideas", query=theme["ideas"][0]["title"].split()[0])["structuredContent"]["ideas"]
    assert found
    # The idea with the most other speakers, so the pseudonyms get a workout.
    (busiest,) = admin.execute("""SELECT idea_id FROM pub.evidence WHERE said_by NOT IN ('me', 'unknown')
                                  GROUP BY idea_id ORDER BY count(DISTINCT said_by) DESC LIMIT 1""").fetchone()
    result = tool(url, "get_idea", idea_id=str(busiest))
    text = result["content"][0]["text"]
    assert not any(name in text for name in names)
    speakers = {ev["said_by"] for ev in result["structuredContent"]["evidence"]}
    assert speakers and speakers <= {"me", "unknown"} | {f"S{i}" for i in range(1, 20)}
    assert all("item_id" not in ev for ev in result["structuredContent"]["evidence"])
    assert "proposed_themes" in tool(url, "whats_new", since="2026-01-01")["structuredContent"]


@needs_db
def test_mistakes_come_back_as_tool_errors(url):
    assert tool(url, "get_idea", idea_id="not-an-id")["isError"]
    assert tool(url, "get_idea", idea_id="00000000-0000-0000-0000-000000000000")["isError"]
    assert tool(url, "open_theme", theme="no theme is called this")["isError"]
    assert tool(url, "search_ideas")["isError"]
    assert tool(url, "whats_new", since="last tuesday")["isError"]


@needs_db
def test_a_star_from_conversation_is_a_feedback_event(url, admin):
    (idea,) = admin.execute("SELECT idea_id FROM pub.ideas WHERE NOT starred LIMIT 1").fetchone()
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
        assert not tool(url, "star_idea", idea_id=str(idea)).get("isError")
        assert admin.execute("SELECT kind, source FROM pub.feedback_events WHERE event_id > %s AND idea_id = %s",
                             (before, idea)).fetchall() == [("star", "mcp")]
        assert tool(url, "get_idea", idea_id=str(idea))["structuredContent"]["starred"] is True
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND idea_id = %s AND source = 'mcp'",
                      (before, idea))


@needs_db
def test_best_ideas_ranks_by_idea_machines_score(url, admin, monkeypatch):
    from uuid import UUID

    from lattice import data

    a, b = [i for (i,) in admin.execute("SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 2")]
    real = data.scores
    monkeypatch.setattr(data, "scores", lambda conn: {})
    assert tool(url, "best_ideas")["isError"]
    fake = {a: {"score": 0.4, "generality": 2, "novelty": 2, "compression": 3, "outside_value": 2, "recurrence": 0.0,
                "why": "Narrow."},
            b: {"score": 0.9, "generality": 5, "novelty": 4, "compression": 4, "outside_value": 5, "recurrence": 0.5,
                "why": "Travels well."}}
    monkeypatch.setattr(data, "scores", lambda conn: fake)
    best = tool(url, "best_ideas", limit=5)["structuredContent"]["ideas"]
    assert [UUID(i["id"]) for i in best] == [b, a] and best[0]["why"] == "Travels well." and best[0]["score"] == 0.9
    assert tool(url, "get_idea", idea_id=str(b))["structuredContent"]["ranking"]["novelty"] == 4
    monkeypatch.setattr(data, "scores", real)
    assert isinstance(real(data.connect()), dict)   # the real view reads, scored or not


@needs_db
def test_proposals_and_keeping_are_the_owners_call(url, admin):
    found = tool(url, "proposals")
    if found.get("isError"):   # an Idea Machine before M2
        pytest.skip(found["content"][0]["text"])
    proposals = found["structuredContent"]["proposals"]
    weights = [p["weight"] for p in proposals]
    assert weights == sorted(weights, reverse=True)
    (idea,) = admin.execute("SELECT idea_id FROM pub.ideas LIMIT 1").fetchone()
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
        assert tool(url, "keep_idea", idea_id=str(idea), decision="maybe").get("isError")
        for decision in ("pinned", "unkeep", "dismiss"):
            assert not tool(url, "keep_idea", idea_id=str(idea), decision=decision).get("isError")
        assert admin.execute("SELECT kind, payload, source FROM pub.feedback_events WHERE event_id > %s "
                             "ORDER BY event_id", (before,)).fetchall() == [
            ("idea_keep", {"as": "pinned"}, "mcp"), ("idea_unkeep", {}, "mcp"), ("idea_dismiss", {}, "mcp")]
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND source = 'mcp'", (before,))
