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


def test_kept_unread_comes_back_when_it_matters():
    g = {"themes": [{"name": "A"}, {"name": "B"}],
         "nodes": [{"id": "x", "themes": ["A"]}, {"id": "y", "themes": ["B"]}, {"id": "z", "themes": ["A"]}],
         "links": [{"source": "x", "target": "y", "kind": "evolves"}, {"source": "z", "target": "x", "kind": "related"}]}
    spans = data.bridges(g)
    assert spans == {"x": ["A", "B"], "y": ["A", "B"]}   # the evolves link crosses themes; z only touches A
    assert data.why_it_matters("x", g, [{}], spans, []) == ["it evolves another idea", "it bridges A ↔ B"]
    assert data.why_it_matters("z", g, [{}], spans, []) == []                   # related alone doesn't count
    assert data.why_it_matters("z", g, [{}, {}], spans, [["x", "z"]]) == ["it came up in a comparison",
                                                                          "it was said again"]


@needs_db
def test_look_again_by_conversation(url, admin, monkeypatch):
    found = tool(url, "look_again")["structuredContent"]
    assert {"kept_unread_worth_a_look", "captures", "how"} <= set(found)
    assert "kept_unread_worth_a_look" in tool(url, "whats_new")["structuredContent"]
    # In process, as if every keep so far had been a bulk keep: real ideas, links and themes.
    from lattice import mcp
    real = data.evidence_of
    monkeypatch.setattr(data, "evidence_of", lambda conn, idea_id=None: {
        i: [{**ev, "how": "bulk" if ev["verdict"] == "keep" else ev["how"]} for ev in evs]
        for i, evs in real(conn, idea_id).items()})
    with data.connect() as conn:
        result = mcp.look_again(conn, limit=20)
    if not result["captures"]:
        pytest.skip("no kept capture in this database whose idea matters")
    names = {n for (n,) in admin.execute("SELECT DISTINCT said_by FROM pub.evidence")} - {"me", "unknown"}
    assert all(c["why"] and c["said_by"] not in names for c in result["captures"])
    reasons = [len(c["why"]) for c in result["captures"]]
    assert reasons == sorted(reasons, reverse=True)


@needs_db
def test_the_weekly_digest(url, admin):
    snap = json.loads(request(url + "/api/snapshot", login(url))[2])
    if not snap.get("digests"):
        pytest.skip("no weekly digest in this database")
    d = snap["digests"][0]
    assert d["summary"] and all({"point", "kind", "title", "body", "idea_ids", "mine"} <= set(p) for p in d["points"])
    got = tool(url, "weekly_digest", week=d["week_start"])["structuredContent"]
    assert got["digest_id"] == d["id"] and len(got["points"]) == len(d["points"])
    assert tool(url, "weekly_digest", week="1999-01-01")["isError"]
    assert tool(url, "rate_digest_point", digest_id=d["id"], point=1, rating="meh")["isError"]
    for bad in [{"kind": "digest_star", "digest_id": "1", "point": 1}, {"kind": "digest_star", "digest_id": d["id"]},
                {"kind": "digest_dismiss", "digest_id": True, "point": 1}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
        assert not tool(url, "rate_digest_point", digest_id=d["id"], point=1, rating="dismiss", note="obvious").get("isError")
        status, _ = post(url + "/api/feedback", login(url), {"kind": "digest_star", "digest_id": d["id"], "point": 1})
        assert status == 201
        assert tool(url, "weekly_digest")["structuredContent"]["points"][0]["owner_said"] == "star"   # latest wins
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND kind LIKE 'digest_%%'", (before,))


@needs_db
def test_adding_an_idea(url, admin):
    import uuid as _uuid

    new = str(_uuid.uuid4())
    for bad in [{"kind": "idea_add", "idea_id": new, "title": "No statement"},
                {"kind": "idea_add", "idea_id": new, "title": " ", "statement": "x"},
                {"kind": "idea_add", "idea_id": new, "title": "t", "statement": "s", "link_to": ["nope"]},
                {"kind": "idea_add", "idea_id": new, "title": "t", "statement": "s", "link_to": "x"}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    (other,) = admin.execute("SELECT idea_id FROM pub.ideas LIMIT 1").fetchone()
    good = {"kind": "idea_add", "idea_id": new, "title": " Mine ", "statement": "An idea I typed.",
            "note": "From a walk.", "link_to": [str(other), new]}
    status, body = post(url + "/api/feedback", login(url), good)
    try:
        assert status in (201, 503)   # 503 until Idea Machine takes idea_add
        if status == 201:
            assert admin.execute("SELECT payload FROM pub.feedback_events WHERE event_id = %s",
                                 (body["event_id"],)).fetchone()[0] == {
                "title": "Mine", "statement": "An idea I typed.", "note": "From a walk.", "link_to": [str(other)]}
            shown = json.loads(request(url + f"/api/idea/{new}", login(url))[2])   # at once, not on the run
            assert (shown["title"], shown["origin"], shown["notes"]) == ("Mine", "mine", "From a walk.")
            assert post(url + "/api/feedback", login(url), good)[0] == 201   # a resend changes nothing
        added = tool(url, "add_idea", title="T", statement="S")
        assert bool(added.get("isError")) == (status == 503)
        made = [new] + ([added["structuredContent"]["idea_id"]] if status == 201 else [])
    finally:
        made = locals().get("made", [new])
        admin.execute("DELETE FROM pub.feedback_events WHERE idea_id = ANY(%s::uuid[])", (made,))
        admin.execute("DELETE FROM im.ideas WHERE idea_id = ANY(%s::uuid[]) AND origin = 'mine'", (made,))


def test_a_reading_order_keeps_neighbours_together():
    from lattice.mcp import _reading_order

    ids = list("abcdef")
    links = [{"a": "a", "b": "b"}, {"a": "b", "b": "c"}, {"a": "d", "b": "e"}]
    groups, loose = _reading_order(ids, links, {i: 0 for i in ids} | {"f": 5})
    assert groups == [["b", "a", "c"], ["d", "e"]] and loose == ["f"]


@needs_db
def test_draft_material(url, admin):
    themes = tool(url, "list_themes")["structuredContent"]["themes"]
    theme = max(themes, key=lambda t: t["ideas"])
    got = tool(url, "draft_material", theme=theme["name"])["structuredContent"]
    assert got["source"]["name"] == theme["name"] and got["ideas"] and "how" in got
    titles = {i["title"] for i in got["ideas"]}
    order = got["suggested_order"]
    assert set(sum(order["connected_groups"], []) + order["loose_ends"]) == titles   # every idea, once
    names = {n for (n,) in admin.execute("SELECT DISTINCT said_by FROM pub.evidence")} - {"me", "unknown"}
    speakers = {q["said_by"] for i in got["ideas"] for q in i["quotes"]}
    assert not speakers & names
    picked = [i["id"] for i in got["ideas"][:2]]
    assert len(tool(url, "draft_material", idea_ids=picked)["structuredContent"]["ideas"]) == 2
    assert tool(url, "draft_material")["isError"]
    assert tool(url, "draft_material", theme="x", idea_ids=picked)["isError"]
    if json.loads(request(url + "/api/snapshot", login(url))[2]).get("digests"):
        point = tool(url, "draft_material", point=1)["structuredContent"]
        assert point["source"]["kind"] == "digest point" and point["ideas"]
        assert tool(url, "draft_material", point=99)["isError"]


@needs_db
def test_shaping_themes_is_checked(url, admin):
    t1, t2 = [str(i) for (i,) in admin.execute("SELECT theme_id FROM pub.themes ORDER BY theme_id LIMIT 2")]
    (idea,) = admin.execute("SELECT idea_id::text FROM pub.ideas LIMIT 1").fetchone()
    for bad in [{"kind": "rename_theme", "theme_id": t1, "name": "  "},
                {"kind": "merge_theme", "theme_id": t1, "into": t1},
                {"kind": "merge_theme", "theme_id": t1, "into": "nope"},
                {"kind": "split_theme", "theme_id": t1, "name": "New", "idea_ids": []},
                {"kind": "split_theme", "theme_id": t1, "name": "", "idea_ids": [idea]},
                {"kind": "split_theme", "theme_id": t1, "name": "New", "idea_ids": [idea], "new_theme_id": "x"},
                {"kind": "file_idea", "idea_id": idea, "theme_id": "nope"}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    (name,) = admin.execute("SELECT name FROM pub.themes WHERE theme_id = %s", (t1,)).fetchone()
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    # Only a rename to its own name goes through: Idea Machine may apply theme events as they arrive, and a real
    # merge or split would reshape dev's themes.
    try:
        assert post(url + "/api/feedback", login(url), {"kind": "rename_theme", "theme_id": t1, "name": name})[0] == 201
    finally:
        admin.execute("DELETE FROM pub.feedback_events WHERE event_id > %s AND source = 'lattice' AND kind IN "
                      "('rename_theme', 'merge_theme', 'split_theme', 'file_idea')", (before,))


@needs_db
def test_shaping_themes_by_conversation():
    """Against Idea Machine's triggers, in one transaction rolled back: the edits are real while it lasts."""
    import psycopg

    from lattice import mcp

    conn = psycopg.connect(os.environ["LATTICE_DATABASE_URL"])   # not autocommit
    try:
        themes = conn.execute("SELECT name FROM pub.themes ORDER BY n_ideas DESC LIMIT 2").fetchall()
        (big,), (other,) = themes
        renamed = mcp.rename_theme(conn, big.upper(), "A rolled-back name")
        assert renamed["renamed"] == big and renamed["theme"]["name"] == "A rolled-back name"
        with pytest.raises(mcp.ToolError):   # a name another theme has isn't taken
            mcp.rename_theme(conn, "A rolled-back name", other)
        ideas = [i["id"] for i in mcp.open_theme(conn, "A rolled-back name")["ideas"][:2]]
        split = mcp.split_theme(conn, "A rolled-back name", "Split, rolled back", ideas)
        assert split["new_theme"]["name"] == "Split, rolled back" and split["new_theme"]["ideas"] == 2
        out = mcp.file_idea(conn, ideas[0], "Split, rolled back", remove=True)
        assert "Split, rolled back" not in out["themes"]
        assert "Split, rolled back" in mcp.file_idea(conn, ideas[0], "Split, rolled back")["themes"]
        merged = mcp.merge_themes(conn, "Split, rolled back", "A rolled-back name")
        assert merged["into"]["name"] == "A rolled-back name"
        assert not conn.execute("SELECT 1 FROM pub.themes WHERE name = 'Split, rolled back'").fetchone()
        with pytest.raises(mcp.ToolError):
            mcp.merge_themes(conn, other, other)
    finally:
        conn.rollback()
        conn.close()


@needs_db
def test_lattice_embeds_as_idea_machine_does(admin):
    """Search by meaning compares Lattice's query vectors with Idea Machine's idea vectors: they must be the same."""
    import numpy as np

    from lattice import meaning

    rows = admin.execute("""SELECT t.title, t.statement, e.embedding::text FROM im.idea_text t
                            JOIN im.idea_embeddings e USING (idea_id) WHERE e.model_version = %s LIMIT 5""",
                         (meaning.MODEL_VERSION,)).fetchall()
    if not rows:
        pytest.skip("Idea Machine's ideas aren't embedded with the model Lattice knows")
    for title, statement, stored in rows:
        v = np.array(meaning.embed(f"{title}\n{statement}"))
        assert float(v @ np.array(json.loads(stored))) > 0.9999


@needs_db
def test_ideas_like_this(url):
    near = json.loads(request(url + "/api/near?q=leading+a+partner+in+dance", login(url))[2])
    assert set(near) >= {"available", "ideas"}
    assert request(url + "/api/near?q=dance&k=many", login(url))[0] == 400
    snap = json.loads(request(url + "/api/snapshot", login(url))[2])
    assert isinstance(snap["neighbours"], dict)
    assert tool(url, "similar_ideas")["isError"] and tool(url, "similar_ideas", idea_id="x", text="y")["isError"]
    found = tool(url, "similar_ideas", text="leading a partner in dance")
    assert bool(found.get("isError")) != near["available"]
    if near["available"]:
        ideas = found["structuredContent"]["ideas"]
        assert ideas and ideas == sorted(ideas, key=lambda i: -i["similarity"])
        like = tool(url, "similar_ideas", idea_id=ideas[0]["id"])["structuredContent"]["ideas"]
        assert like and ideas[0]["id"] not in [i["id"] for i in like]


@needs_db
def test_markdown_export(url):
    import io
    import re
    import zipfile

    assert request(url + "/api/export")[0] == 401
    status, headers, body = request(url + "/api/export", login(url))
    assert status == 200
    z = zipfile.ZipFile(io.BytesIO(body))
    names = z.namelist()
    snap = json.loads(request(url + "/api/snapshot", login(url))[2])
    assert "Lattice/Lattice.md" in names
    assert sum(n.startswith("Lattice/ideas/") for n in names) == len(snap["ideas"])
    assert sum(n.startswith("Lattice/themes/") for n in names) == len(snap["graph"]["themes"])
    files = {n.removeprefix("Lattice/").rsplit("/", 1)[-1].removesuffix(".md") for n in names}
    for n in names:   # every [[link]] lands on a file in the vault
        for target in re.findall(r"\[\[([^\]|]+)", z.read(n).decode()):
            assert target in files, (n, target)
    one = z.read(next(n for n in names if n.startswith("Lattice/ideas/"))).decode()
    assert one.startswith("---\nid: ") and "\n# " in one


@needs_db
def test_demo_mode_marks_are_checked(url):
    (idea,) = json.loads(request(url + "/api/snapshot", login(url))[2])["graph"]["nodes"][:1]
    for bad in [{"kind": "idea_sensitive", "idea_id": idea["id"]},
                {"kind": "idea_sensitive", "idea_id": idea["id"], "sensitive": "yes"},
                {"kind": "theme_sensitive", "theme_id": "nope", "sensitive": True}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    snap = json.loads(request(url + "/api/snapshot", login(url))[2])
    assert set(snap["sensitivity"]) == {"ideas", "themes"}


@needs_db
def test_the_demo_view_holds_back_and_blacks_out(monkeypatch, admin):
    from lattice import mcp

    conn = data.connect()
    themes = {n: str(i) for i, n in conn.execute("SELECT theme_id, name FROM pub.themes")}
    gender = next(n for n in themes if n.startswith("Gender")) if any(n.startswith("Gender") for n in themes) else max(themes)
    hidden = [str(i) for (i,) in conn.execute("SELECT idea_id FROM pub.ideas WHERE %s = ANY(themes)", (gender,))]
    fake = {"themes": {themes[gender]: {"sensitive": True, "categories": ["gender_politics"], "by": "claude"}},
            "ideas": {i: {"sensitive": True, "categories": ["gender_politics"], "by": "claude"} for i in hidden}}
    monkeypatch.setattr(data, "sensitivity", lambda c: fake)
    rpc = lambda m, **p: mcp.handle({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, connect=data.connect, demo=True)
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert names == set(mcp.DEMO_TOOLS)
    assert "Lattice (demo)" in rpc("initialize", protocolVersion="2025-06-18")["result"]["serverInfo"]["title"]
    use = lambda name, **a: rpc("tools/call", name=name, arguments=a)
    assert "error" in use("review_digest") and "error" in use("star_idea", idea_id=hidden[0])
    listed = use("list_themes")["result"]["structuredContent"]["themes"]
    assert gender not in {t["name"] for t in listed} and listed
    assert use("get_idea", idea_id=hidden[0])["result"]["isError"]
    assert use("open_theme", theme=gender)["result"]["isError"]
    best = use("best_ideas", theme=gender)["result"]
    assert best.get("isError") or not best["structuredContent"]["ideas"]
    assert gender not in json.dumps(best)
    text = json.dumps([use("search_ideas", query=w)["result"] for w in ("the", "a", "is")])
    assert not any(i in text for i in hidden)
    other = next(t for t in listed if t["ideas"] > 1)["name"]
    drafted = use("draft_material", theme=other)["result"]["structuredContent"]
    assert not any(i["id"] in hidden for i in drafted["ideas"])
    digest = use("weekly_digest")["result"]
    if not digest.get("isError"):
        d = digest["structuredContent"]
        assert d["summary"] == "" and not any(i["id"] in hidden for p in d["points"] for i in p["ideas"])
    held = mcp._Withheld(conn)
    assert held.clean({"quote": "What the fuck, this is bullshit, class"})["quote"] == "What the ████, this is ████████, class"
    monkeypatch.setenv("LATTICE_DEMO_HIDE", "violence")   # the server's choice of categories
    assert gender in {t["name"] for t in use("list_themes")["result"]["structuredContent"]["themes"]}
    conn.close()


@needs_db
def test_words_for_transcription_are_checked(url):
    for bad in [{"kind": "vocab_add"}, {"kind": "vocab_add", "term": "  "}, {"kind": "vocab_add", "term": "x" * 61},
                {"kind": "vocab_add", "term": "goetia", "source": "guess"}, {"kind": "vocab_remove", "term": ""}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    assert isinstance(json.loads(request(url + "/api/snapshot", login(url))[2])["vocab"], list)
