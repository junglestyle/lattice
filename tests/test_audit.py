"""Digests, adding and drafting, themes, meaning, export and demo mode (app and MCP).
The database tests need LATTICE_DATABASE_URL, and IM_DEV_ADMIN_URL to delete the events they append."""

import json
import os

import pytest

from lattice import data
from test_mcp import tool
from test_review import admin, post  # noqa: F401  (admin is a fixture)
from test_server import login, request, url  # noqa: F401  (url is a fixture)

needs_db = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


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
    for bad in [{"kind": "digest_star", "digest_id": "1", "point": 1}, {"kind": "digest_star", "digest_id": d["id"]},
                {"kind": "digest_dismiss", "digest_id": True, "point": 1}]:
        assert post(url + "/api/feedback", login(url), bad)[0] == 400, bad
    before = admin.execute("SELECT coalesce(max(event_id), 0) FROM pub.feedback_events").fetchone()[0]
    try:
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
    made = [new]
    try:
        assert status == 201
        assert admin.execute("SELECT payload FROM pub.feedback_events WHERE event_id = %s",
                             (body["event_id"],)).fetchone()[0] == {
            "title": "Mine", "statement": "An idea I typed.", "note": "From a walk.", "link_to": [str(other)]}
        shown = json.loads(request(url + "/api/snapshot", login(url))[2])["ideas"][new]   # at once, not on the run
        assert (shown["title"], shown["origin"], shown["notes"]) == ("Mine", "mine", "From a walk.")
        assert post(url + "/api/feedback", login(url), good)[0] == 201   # a resend changes nothing
        added = tool(url, "add_idea", title="T", statement="S")
        assert not added.get("isError"), added
        made.append(added["structuredContent"]["idea_id"])
    finally:
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
