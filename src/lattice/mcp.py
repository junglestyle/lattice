"""Talk to it: an MCP server over pub (Idea Machine's ROADMAP, Phase 2 slice 7).

Streamable HTTP at /mcp, answering each JSON-RPC request with plain JSON (no SSE stream, no sessions). It's behind
the Lattice token as a bearer header and reachable only on the tailnet, like the rest of Lattice.

What leaves through it follows Idea Machine's privacy policy B: quotes go out, but speakers are pseudonymized per
answer ("me", "unknown", then S1, S2... in order of first appearance). Flags made here (star, link, note) are
feedback events like the app's, with source 'mcp'; so are review verdicts (spot checks one at a time, the rest
kept in bulk and flagged so).
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from lattice import data, meaning

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]   # newest first; the first is offered when the client's isn't
SERVER = {"name": "lattice", "title": "Lattice (Idea Machine)", "version": "0.1.0"}
INSTRUCTIONS = """Lattice is the owner's Idea Machine: when they mention Idea Machine, their lattice, their ideas or
what they've been thinking or saying lately, use these tools. It holds the ideas Idea Machine captured from their
recorded conversations (and seeded from an old ChatGPT archive), grouped into themes, connected to each other,
each backed by evidence quotes. Speak to the owner as "you". In quotes, "me" is the owner; S1, S2... are other people, renumbered in every
answer. Search or open a theme before asking the owner for ids. Starring, linking and notes are the owner's
feedback: do them when they ask, not on your own initiative.

To review new captures, call review_digest: read the owner the spot checks one at a time and record their own
verdict on each with decide_item (never your guess), then offer to keep the rest with keep_rest. The owner's
default is to keep: the lattice gets its value from connections as it grows. Captures kept that way come back
through look_again once their idea turns out to matter; offer them after the spot checks, and record the owner's
verdict with decide_item. Discards are the owner's high-signal verdict: when they discard, ask why in a few words
and pass it as the note.

When the owner wants to add an idea from your conversation: check similar_ideas with your draft (and search_ideas
for key words), and if an existing idea says the same, offer a note or a link instead. Otherwise draft a short title and a one-to-three-sentence statement in the
owner's own terms, read them back, and call add_idea only once they approve the wording.

Themes are the owner's to shape. You may suggest renaming, merging or splitting themes, or filing an idea
elsewhere, when you notice a theme is muddled, two overlap, or an idea sits in the wrong one; but change them
(rename_theme, merge_themes, split_theme, file_idea) only once the owner agrees to the specific change.

To draft something from the lattice (an outline, essay, guide, talk), call draft_material for a theme, a digest
point or a set of ideas, then ask the owner what form, audience and length they want before writing. Write in
their voice from their ideas; quote them sparingly; say where ideas pull against each other rather than smoothing
it over; don't add claims they haven't made without marking them as yours."""


def _pseudonymize(rows: list[dict], key: str = "said_by", alias: dict | None = None) -> list[dict]:
    alias = {} if alias is None else alias   # shared across calls to keep one numbering in one answer
    for row in rows:
        who = row.get(key)
        if who not in ("me", "unknown", None):
            who = alias.setdefault(who, f"S{len(alias) + 1}")
        row[key] = who
    return rows


def _uuid(value, name: str = "idea_id") -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        if name == "item_id":
            raise ToolError("item_id must be a capture's id (a UUID) from review_digest") from None
        raise ToolError(f"{name} must be an idea id (a UUID) from search_ideas, open_theme or whats_new") from None


class ToolError(Exception):
    """A mistake the model can fix: reported as a tool result with isError, not a protocol error."""


class UnknownTool(Exception):
    pass


def _brief(row, ranked: dict | None = None) -> dict:
    i, title, statement, themes, n, starred, made = row
    out = {"id": str(i), "title": title, "statement": statement, "themes": list(themes), "evidence": n,
           "starred": starred, "created_at": made.isoformat()}
    if ranked and i in ranked:
        out["score"] = round(ranked[i]["score"], 2)
    return out


def best_ideas(conn, limit: int = 10, theme: str | None = None) -> dict:
    ranked = data.scores(conn)
    if not ranked:
        raise ToolError("Idea Machine hasn't scored any ideas yet")
    limit = max(1, min(int(limit), 50))
    rows = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE idea_id = ANY(%s)"
                        + (" AND lower(%s) = ANY(SELECT lower(t) FROM unnest(themes) t)" if theme else ""),
                        [list(ranked), *([theme.strip()] if theme else [])]).fetchall()
    rows.sort(key=lambda r: -ranked[r[0]]["score"])
    ideas = []
    for r in rows[:limit]:
        x = ranked[r[0]]
        ideas.append(_brief(r, ranked) | {"why": x["why"], "generality": x["generality"], "novelty": x["novelty"],
                                          "compression": x["compression"], "outside_value": x["outside_value"]})
    return {"theme": theme, "ideas": ideas,
            "how_scored": "0.7 x a Claude rubric (generality, novelty, compression, interest to a thoughtful "
                          "stranger, each 1-5) + 0.2 x recurrence across captures + 0.1 if starred"}


BRIEF = "idea_id, title, statement, themes, n_evidence, starred, created_at"


def search_ideas(conn, query: str, limit: int = 10) -> dict:
    words = [w for w in str(query).lower().split() if w][:8]
    if not words:
        raise ToolError("query is empty")
    limit = max(1, min(int(limit), 50))
    text = "lower(concat_ws(' ', title, statement, notes, my_note, array_to_string(themes, ' ')))"
    rows = conn.execute(
        f"""SELECT {BRIEF} FROM pub.ideas WHERE {" AND ".join(f"{text} LIKE %s" for _ in words)}
            ORDER BY (SELECT count(*) FROM unnest(%s::text[]) w WHERE lower(title) LIKE '%%' || w || '%%') DESC,
                     starred DESC, n_evidence DESC, title
            LIMIT %s""", [*(f"%{w}%" for w in words), words, limit]).fetchall()
    return {"query": query, "ideas": [_brief(r) for r in rows]}


def get_idea(conn, idea_id: str) -> dict:
    found = data.idea(conn, _uuid(idea_id))
    if found is None:
        raise ToolError("no such idea (it may have been forgotten)")
    for ev in found["evidence"]:
        for k in ("item_id", "verdict", "relation", "confidence"):
            ev.pop(k, None)
    _pseudonymize(found["evidence"])
    found["connections"] = [
        {"id": str(other), "title": title, "kind": "your link" if kind == "mine" else kind}
        for other, title, kind in conn.execute(
            """SELECT CASE WHEN c.a = %(i)s THEN c.b ELSE c.a END AS other, d.title, c.kind FROM pub.connections c
               JOIN pub.ideas d ON d.idea_id = CASE WHEN c.a = %(i)s THEN c.b ELSE c.a END
               WHERE %(i)s IN (c.a, c.b) ORDER BY c.kind, d.title""", {"i": found["id"]})]
    if not found["corrected"]:
        found.pop("claude_title"), found.pop("claude_statement")
    ranking = data.scores(conn).get(UUID(found["id"]))
    if ranking:
        found["ranking"] = ranking
    return found


def list_themes(conn) -> dict:
    return {"themes": [{"id": str(i), "name": n, "description": d, "ideas": c,
                        "status": "pinned" if p else "proposed" if o == "clustered" else "unpinned"}
                       for i, n, d, o, p, c in conn.execute(
                           """SELECT theme_id, name, description, origin, pinned, n_ideas FROM pub.themes
                              ORDER BY pinned DESC, n_ideas DESC, name""")]}


def _theme(conn, theme: str) -> tuple:
    """(id, name, description) of the theme a reference names: its id, its exact name, or else part of its name."""
    row = conn.execute("""SELECT theme_id, name, description FROM pub.themes
                          WHERE theme_id::text = %(t)s OR lower(name) = lower(%(t)s)
                          ORDER BY theme_id::text = %(t)s DESC LIMIT 1""", {"t": str(theme).strip()}).fetchone()
    if row is None:
        rows = conn.execute("SELECT theme_id, name, description FROM pub.themes WHERE name ILIKE %s ORDER BY name",
                            (f"%{str(theme).strip()}%",)).fetchall()
        if len(rows) > 1:
            raise ToolError(f"{theme!r} matches several themes: {', '.join(r[1] for r in rows)}; use the full name")
        row = rows[0] if rows else None
    if row is None:
        raise ToolError(f"no theme matches {theme!r}; list_themes shows them all")
    return str(row[0]), row[1], row[2]


def open_theme(conn, theme: str) -> dict:
    tid, name, description = _theme(conn, theme)
    ideas = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE %s = ANY(themes) ORDER BY starred DESC, n_evidence DESC, title",
                         (name,)).fetchall()
    return {"id": str(tid), "name": name, "description": description, "ideas": [_brief(r) for r in ideas]}


MAX_DRAFT_IDEAS = 40


def _reading_order(ids: list[str], links: list[dict], weight: dict) -> tuple[list[list[str]], list[str]]:
    """Group connected ideas and walk each group so neighbours stay together: largest group first, each walked
    breadth-first from its best-connected idea (ties: weight, then id, so the order is stable). Ideas with no link in the set come back separately."""
    adj = {i: set() for i in ids}
    for link in links:
        adj[link["a"]].add(link["b"]), adj[link["b"]].add(link["a"])
    seen, groups = set(), []
    for start in sorted(ids, key=lambda i: (-len(adj[i]), -weight[i], i)):
        if start in seen or not adj[start]:
            continue
        walk, queue = [], [start]
        seen.add(start)
        while queue:
            i = queue.pop(0)
            walk.append(i)
            for j in sorted(adj[i] - seen, key=lambda j: (-len(adj[j]), -weight[j], j)):
                seen.add(j), queue.append(j)
        groups.append(walk)
    loose = sorted((i for i in ids if not adj[i]), key=lambda i: -weight[i])
    return sorted(groups, key=len, reverse=True), loose


def draft_material(conn, theme: str | None = None, digest_week: str | None = None, point: int | None = None,
                   idea_ids: list | None = None) -> dict:
    if sum(x is not None for x in (theme, point, idea_ids)) != 1:
        raise ToolError("give exactly one source: theme, a digest point (point, with digest_week for an older week), "
                        "or idea_ids")
    if theme is not None:
        t = open_theme(conn, theme)
        source = {"kind": "theme", "name": t["name"], "description": t["description"]}
        ids = [i["id"] for i in t["ideas"]]
    elif point is not None:
        d = weekly_digest(conn, digest_week)
        p = next((p for p in d["points"] if p["point"] == point), None)
        if p is None:
            raise ToolError(f"that digest has points 1 to {len(d['points'])}")
        source = {"kind": "digest point", "week_start": d["week_start"], "title": p["title"], "body": p["body"]}
        ids = [i["id"] for i in p["ideas"]]
    else:
        ids = list(dict.fromkeys(_uuid(i) for i in idea_ids))
        source = {"kind": "chosen ideas"}
    if not ids:
        raise ToolError("no ideas left in that source (they may have been forgotten or discarded)")
    ids = ids[:MAX_DRAFT_IDEAS]
    ranked = data.scores(conn)
    alias: dict = {}
    ideas = {}
    for i in ids:
        found = data.idea(conn, i)
        if found is None:
            continue
        quotes = [{"said_by": ev["said_by"], "said_at": ev["said_at"][:10], "quote": ev["quote"]}
                  for ev in found["evidence"] if ev["verdict"] != "discard"][:3]
        _pseudonymize(quotes, alias=alias)
        score = ranked.get(UUID(i), {}).get("score")
        ideas[i] = {"id": i, "title": found["title"], "statement": found["statement"],
                    "origin": {"mine": "added by you", "archive": "from your archive"}.get(found["origin"], "said"),
                    "your_note": found["my_note"], "context": found["notes"], "starred": found["starred"],
                    "times_said": len(found["evidence"]), "score": round(score, 2) if score is not None else None,
                    "quotes": quotes}
    if not ideas:
        raise ToolError("none of those ideas exist (they may have been forgotten)")
    links = [{"a": str(a), "b": str(b), "kind": "your link" if k == "mine" else k}
             for a, b, k in conn.execute(
                 """SELECT a, b, kind FROM pub.connections WHERE a = ANY(%(i)s::uuid[]) AND b = ANY(%(i)s::uuid[])
                    ORDER BY kind""", {"i": list(ideas)})]
    weight = {i: 2 * x["starred"] + x["times_said"] + (x["score"] or 0) for i, x in ideas.items()}
    groups, loose = _reading_order(list(ideas), links, weight)
    title = lambda i: ideas[i]["title"]   # noqa: E731
    return {"source": source, "ideas": list(ideas.values()),
            "connections": [{**link, "a_title": title(link["a"]), "b_title": title(link["b"])} for link in links],
            "suggested_order": {"connected_groups": [[title(i) for i in g] for g in groups],
                                "loose_ends": [title(i) for i in loose]},
            "how": "Ask the owner what to make (outline, essay, one-page guide, talk, thread), for whom, and how long, "
                   "before drafting. Build from these ideas in their words: statements and notes are the owner's "
                   "thinking; quotes are what was actually said (S1, S2 are other people, renumbered per answer). "
                   "The suggested order keeps connected ideas together; loose ends may not belong. Flag tensions "
                   "between ideas instead of resolving them silently, and mark anything you add."}


def whats_new(conn, since: str | None = None) -> dict:
    if since:
        try:
            start = datetime.fromisoformat(str(since))
        except ValueError:
            raise ToolError("since must be an ISO date or time, e.g. 2026-10-01") from None
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=7)
    ideas = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE created_at >= %s ORDER BY created_at DESC LIMIT 100",
                         (start,)).fetchall()
    themes = [n for (n,) in conn.execute(
        "SELECT name FROM pub.themes WHERE origin = 'clustered' AND NOT pinned ORDER BY name")]
    waiting = conn.execute("SELECT count(*) FROM pub.review_items").fetchone()[0]
    unread = len(data.look_again(conn))
    total = conn.execute("SELECT count(*) FROM pub.ideas WHERE created_at >= %s", (start,)).fetchone()[0]
    return {"since": start.isoformat(), "new_ideas_total": total, "new_ideas": [_brief(r) for r in ideas],
            "proposed_themes": themes,
            "captures_waiting_for_review": waiting, "kept_unread_worth_a_look": unread}


def review_digest(conn) -> dict:
    queue = data.review(conn, limit=500)
    picked = set(queue["audit"])
    alias: dict = {}
    spot = [i for i in queue["items"] if i["id"] in picked]
    spot.sort(key=lambda i: i["confidence"])
    checks = []
    for n, i in enumerate(spot, 1):
        _pseudonymize([i], alias=alias)
        _pseudonymize(i["context"], key="speaker", alias=alias)
        checks.append({"item_id": i["id"], "spot_check": f"{n} of {len(spot)}", "kind": i["kind"],
                       "said_by": i["said_by"], "said_at": i["said_at"], "quote": i["quote"], "gist": i["gist"],
                       "confidence": round(i["confidence"], 2), "themes": i["themes"],
                       "idea": i["idea"]["title"] if i["idea"] else None,
                       "conversation": [f"{c['speaker']}: {c['text']}" for c in i["context"]]})
    rest = [i for i in queue["items"] if i["id"] not in picked]
    kinds: dict = {}
    for i in rest:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    return {"waiting": queue["total"], "spot_checks": checks,
            "rest": {"count": len(rest), "by_kind": kinds,
                     "items": [{"item_id": i["id"], "kind": i["kind"], "gist": i["gist"],
                                "idea": i["idea"]["title"] if i["idea"] else None} for i in rest]},
            "how": "Spot checks are spread across Idea Machine's confidence, lowest first. Read each to the owner "
                   "and record what they say with decide_item(spot_check=true). Then offer to keep the rest: if they want to judge "
                   "some of them themselves, leave those out of keep_rest's item_ids."}


def look_again(conn, limit: int = 5) -> dict:
    found = data.look_again(conn)
    shown = _pseudonymize([{k: ev[k] for k in ("item_id", "idea_id", "idea", "kind", "said_by", "said_at", "quote",
                                               "gist", "why")} for ev in found[:max(1, min(int(limit), 20))]])
    return {"kept_unread_worth_a_look": len(found), "captures": shown,
            "how": "Each was kept with the rest without being read, and its idea has since turned out to matter "
                   "(why). Read the quote and why to the owner and record their verdict with decide_item "
                   "(spot_check=false). Keeping it again is fine; a discard is the useful signal, with a reason."}


def weekly_digest(conn, week: str | None = None) -> dict:
    if week:
        try:
            week = datetime.fromisoformat(str(week)).date().isoformat()
        except ValueError:
            raise ToolError("week must be an ISO date in the week you want, e.g. 2026-09-30") from None
    found = data.digests(conn, week)
    if not found:
        raise ToolError("no digest for that week" if week else "Idea Machine hasn't written a weekly digest yet")
    d = found[0]
    ids = list({i for p in d["points"] for i in p["idea_ids"]})
    titles = {str(i): t for i, t in conn.execute("SELECT idea_id, title FROM pub.ideas WHERE idea_id = ANY(%s::uuid[])",
                                                 (ids,))}
    weeks = [x["week_start"] for x in data.digests(conn)]
    return {"digest_id": d["id"], "week_start": d["week_start"], "summary": d["summary"],
            "points": [{"point": p["point"], "kind": p["kind"], "title": p["title"], "body": p["body"],
                        "ideas": [{"id": i, "title": titles[i]} for i in p["idea_ids"] if i in titles],
                        "owner_said": p["mine"]} for p in d["points"]],
            "other_weeks": [w for w in weeks if w != d["week_start"]]}


def rate_digest_point(conn, digest_id: int, point: int, rating: str, note: str = "") -> dict:
    if rating not in ("star", "dismiss"):
        raise ToolError("rating must be star or dismiss")
    try:
        data.record(conn, {"kind": f"digest_{rating}", "digest_id": digest_id, "point": point, "note": note},
                    source="mcp")
    except data.NotYet:
        raise ToolError("Idea Machine doesn't accept digest ratings yet") from None
    return {"digest_id": digest_id, "point": point, "rating": rating}


# Shaping themes (slice 4). Idea Machine applies these as they arrive, so the answer reads back the result; an edit
# it records but doesn't make (a name another theme has) comes back as an error to tell the owner.

def _theme_result(conn, theme_id: str) -> dict | None:
    row = conn.execute("SELECT theme_id, name, pinned, n_ideas FROM pub.themes WHERE theme_id = %s", (theme_id,)).fetchone()
    return {"id": str(row[0]), "name": row[1], "pinned": row[2], "ideas": row[3]} if row else None


def _theme_event(conn, event: dict) -> None:
    try:
        data.record(conn, event, source="mcp")
    except data.NotYet:
        raise ToolError("Idea Machine doesn't take this theme edit yet; nothing changed") from None


def rename_theme(conn, theme: str, name: str) -> dict:
    tid, old, _ = _theme(conn, theme)
    _theme_event(conn, {"kind": "rename_theme", "theme_id": tid, "name": name})
    now = _theme_result(conn, tid)
    if now and now["name"] == old and old.strip().lower() != str(name).strip().lower():
        raise ToolError(f"not renamed: another theme is probably called {str(name).strip()!r}")
    return {"renamed": old, "theme": now}


def merge_themes(conn, theme: str, into: str) -> dict:
    tid, name, _ = _theme(conn, theme)
    into_id, into_name, _ = _theme(conn, into)
    if tid == into_id:
        raise ToolError("that's the same theme")
    _theme_event(conn, {"kind": "merge_theme", "theme_id": tid, "into": into_id})
    return {"merged": name, "into": _theme_result(conn, into_id)}


def split_theme(conn, theme: str, name: str, idea_ids: list) -> dict:
    tid, source, _ = _theme(conn, theme)
    new = str(uuid.uuid4())
    _theme_event(conn, {"kind": "split_theme", "theme_id": tid, "name": name,
                        "idea_ids": [_uuid(i) for i in idea_ids or []], "new_theme_id": new})
    made = _theme_result(conn, new)
    if made is None:
        raise ToolError(f"not split: another theme is probably called {str(name).strip()!r}")
    return {"split_from": _theme_result(conn, tid) or source, "new_theme": made}


def file_idea(conn, idea_id: str, theme: str, remove: bool = False) -> dict:
    tid, name, _ = _theme(conn, theme)
    i = _uuid(idea_id)
    _theme_event(conn, {"kind": "unfile_idea" if remove else "file_idea", "idea_id": i, "theme_id": tid})
    row = conn.execute("SELECT title, themes FROM pub.ideas WHERE idea_id = %s", (i,)).fetchone()
    if row is None:
        raise ToolError("no such idea (it may have been forgotten)")
    return {"idea": row[0], "themes": list(row[1]), ("removed_from" if remove else "filed_into"): name}


def similar_ideas(conn, idea_id: str | None = None, text: str | None = None, limit: int = 8) -> dict:
    if (idea_id is None) == (text is None):
        raise ToolError("give idea_id (ideas like this one) or text (ideas close to these words), not both")
    limit = max(1, min(int(limit), 30))
    if idea_id is not None:
        i = _uuid(idea_id)
        hits = [(o, s) for o, s in meaning.neighbours(conn).get(i, [])][:limit]
        if not hits:   # not published yet, or a new idea not embedded: embed its own text
            row = conn.execute("SELECT title, statement FROM pub.ideas WHERE idea_id = %s", (i,)).fetchone()
            if row is None:
                raise ToolError("no such idea (it may have been forgotten)")
            try:
                hits = [(o, s) for o, s in meaning.near(conn, f"{row[0]}\n{row[1]}", limit + 1) if o != i][:limit]
            except meaning.Unavailable as e:
                raise ToolError(f"similar ideas aren't available: {e}") from None
    else:
        try:
            hits = meaning.near(conn, text, limit)
        except meaning.Unavailable as e:
            raise ToolError(f"search by meaning isn't available: {e}; try search_ideas") from None
    sim = dict(hits)
    rows = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE idea_id = ANY(%s::uuid[])", (list(sim),)).fetchall()
    ideas = sorted((_brief(r) | {"similarity": round(sim[str(r[0])], 2)} for r in rows), key=lambda x: -x["similarity"])
    return {"ideas": ideas, "note": "Cosine similarity of Idea Machine's embeddings, best first. Between two ideas, "
                                    "about 0.85 and up is close to the same idea and 0.7 related. A few words score "
                                    "lower against a whole idea (0.6 to 0.75 for a good match), so judge by rank."}


def add_idea(conn, title: str, statement: str, note: str = "", link_to: list | None = None) -> dict:
    links = [_uuid(i, "link_to") for i in (link_to or [])]
    new = str(uuid.uuid4())
    try:
        data.record(conn, {"kind": "idea_add", "idea_id": new, "title": title, "statement": statement, "note": note,
                           "link_to": links}, source="mcp")
    except data.NotYet:
        raise ToolError("Idea Machine doesn't accept new ideas yet; nothing was saved") from None
    return {"idea_id": new, "title": str(title).strip()[:120], "linked_to": links,
            "note": "Added. Idea Machine embeds, places and files it into themes on its next hourly run."}


VERDICTS = {"keep": "item_keep", "star": "item_star", "discard": "item_discard"}


def decide_item(conn, item_id: str, verdict: str, note: str = "", spot_check: bool = False) -> dict:
    if verdict not in VERDICTS:
        raise ToolError("verdict must be keep, star or discard")
    data.record(conn, {"kind": VERDICTS[verdict], "item_id": _uuid(item_id, "item_id"), "note": note,
                       "audit": spot_check is True}, source="mcp")
    return {"item_id": item_id, "verdict": verdict, "spot_check": spot_check is True}


def keep_rest(conn, item_ids: list) -> dict:
    if not isinstance(item_ids, list) or not item_ids:
        raise ToolError("item_ids: the rest.items ids from review_digest the owner agreed to keep")
    ids = {_uuid(i, "item_id") for i in item_ids}
    queue = data.review(conn, limit=500)
    waiting = {i["id"] for i in queue["items"]} - set(queue["audit"])   # a spot check is never kept in bulk
    for i in sorted(ids & waiting):
        data.record(conn, {"kind": "item_keep", "item_id": i, "bulk": True}, source="mcp")
    return {"kept": len(ids & waiting), "skipped_not_waiting_or_spot_checks": sorted(ids - waiting),
            "still_waiting": queue["total"] - len(ids & waiting)}


def star_idea(conn, idea_id: str, starred: bool = True) -> dict:
    data.record(conn, {"kind": "star" if starred else "unstar", "idea_id": _uuid(idea_id)}, source="mcp")
    return {"idea_id": idea_id, "starred": bool(starred)}


def link_ideas(conn, idea_id: str, other_idea_id: str, unlink: bool = False) -> dict:
    a, b = _uuid(idea_id), _uuid(other_idea_id, "other_idea_id")
    if a == b:
        raise ToolError("an idea can't link to itself")
    data.record(conn, {"kind": "unlink" if unlink else "link", "idea_id": a, "other_idea_id": b}, source="mcp")
    return {"idea_id": a, "other_idea_id": b, "linked": not unlink}


def compare_pair(conn) -> dict:
    pair = data.next_pair(conn)
    if pair is None:
        raise ToolError("no pair left to compare")
    rows = {str(i): (t, st) for i, t, st in conn.execute(
        "SELECT idea_id, title, statement FROM pub.ideas WHERE idea_id = ANY(%s::uuid[])",
        ([pair["first"], pair["second"]],))}
    return {"first": {"id": pair["first"], "title": rows[pair["first"]][0], "statement": rows[pair["first"]][1]},
            "second": {"id": pair["second"], "title": rows[pair["second"]][0], "statement": rows[pair["second"]][1]},
            "why_this_pair": pair["reason"],
            "ask": "Read both to the owner in this order and ask which is the better idea, or if they're about the "
                   "same; then call record_comparison with their answer. Don't answer for them."}


def record_comparison(conn, first_idea_id: str, second_idea_id: str, winner: str, note: str = "") -> dict:
    a, b = _uuid(first_idea_id, "first_idea_id"), _uuid(second_idea_id, "second_idea_id")
    try:
        data.record(conn, {"kind": "idea_compare", "idea_id": a, "other_idea_id": b, "winner": winner, "note": note},
                    source="mcp")
    except data.NotYet:
        raise ToolError("Idea Machine doesn't accept comparisons yet") from None
    return {"first_idea_id": a, "second_idea_id": b, "winner": winner}


def note_idea(conn, idea_id: str, note: str) -> dict:
    data.record(conn, {"kind": "idea_note", "idea_id": _uuid(idea_id), "note": note}, source="mcp")
    return {"idea_id": idea_id, "note": str(note).strip()[:1000] or None}


ID = {"type": "string", "description": "An idea's id, from search_ideas, open_theme, whats_new or get_idea"}
THEME = {"type": "string", "description": "A theme's name (or a distinctive part of it) or id, from list_themes"}
TOOLS = {
    "search_ideas": (search_ideas, True, "Find ideas in the owner's Idea Machine (their lattice) whose title, "
                     "statement, notes or themes contain every word of the query.",
                     {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                                             "default": 10}}, ["query"]),
    "get_idea": (get_idea, True, "One idea in full: statement, your note and corrections, themes, connections, "
                 "and the evidence quotes with who said them and when.", {"idea_id": ID}, ["idea_id"]),
    "best_ideas": (best_ideas, True, "The owner's best ideas by Idea Machine's score, best first, each with why "
                   "it scored as it did; optionally within one theme.",
                   {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "theme": {"type": "string", "description": "A theme's exact name"}}, []),
    "list_themes": (list_themes, True, "Every theme with its size and whether it's pinned or only proposed.", {}, []),
    "open_theme": (open_theme, True, "The ideas in one theme, starred and best-evidenced first.",
                   {"theme": {"type": "string", "description": "A theme's name (or part of it) or id"}}, ["theme"]),
    "whats_new": (whats_new, True, "What's new in the owner's Idea Machine: ideas added since a date (default: the last 7 days), proposed themes waiting "
                  "for a decision, and how many captures wait for review.",
                  {"since": {"type": "string", "description": "ISO date or time"}}, []),
    "review_digest": (review_digest, True, "The Idea Machine captures waiting for the owner's review: a few spot checks, picked across "
                      "Idea Machine's confidence for the owner to judge one by one, and the rest summarized for "
                      "keeping in one go. Use when the owner wants to review or catch up.", {}, []),
    "look_again": (look_again, True, "Captures the owner kept in bulk without reading whose idea has since turned "
                   "out to matter (linked, evolving, echoing the archive, bridging themes, compared, said again), "
                   "for a real verdict. Use after the spot checks or when the owner asks what's worth a second look.",
                   {"limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}, []),
    "weekly_digest": (weekly_digest, True, "Idea Machine's weekly digest of the owner's lattice: a summary and "
                      "points (what kept coming up, what converges, sleepers, projects forming, what a stranger would "
                      "value), each with the ideas it rests on. The latest week, or the one holding a date. Its text is "
                      "written for the owner from idea text and may name people (it isn't pseudonymized like quotes).",
                      {"week": {"type": "string", "description": "An ISO date in the week wanted"}}, []),
    "rate_digest_point": (rate_digest_point, False, "Record the owner's response to a digest point: star (worth "
                          "surfacing) or dismiss (not useful). Only when the owner says; the latest response wins.",
                          {"digest_id": {"type": "integer"}, "point": {"type": "integer", "minimum": 1},
                           "rating": {"type": "string", "enum": ["star", "dismiss"]},
                           "note": {"type": "string", "maxLength": 500, "description": "Why, if the owner said"}},
                          ["digest_id", "point", "rating"]),
    "draft_material": (draft_material, True, "Everything needed to draft a piece of writing from the owner's lattice: "
                       "the ideas in a theme, a weekly digest point or a chosen set, with their notes, quotes, "
                       "connections and a suggested reading order. Use when the owner wants to write, outline or "
                       "turn a cluster into something.",
                       {"theme": {"type": "string", "description": "A theme's name (or part of it) or id"},
                        "point": {"type": "integer", "minimum": 1,
                                  "description": "A point number in the weekly digest"},
                        "digest_week": {"type": "string", "description": "An ISO date in the digest's week; default "
                                                                         "the latest"},
                        "idea_ids": {"type": "array", "items": ID, "maxItems": MAX_DRAFT_IDEAS}}, []),
    "rename_theme": (rename_theme, False, "Rename one of the owner's themes. Only once they've agreed to the name.",
                     {"theme": THEME, "name": {"type": "string", "maxLength": 80}}, ["theme", "name"]),
    "merge_themes": (merge_themes, False, "Merge a theme into another: its ideas join `into`, and it goes away. "
                     "`into` keeps its name. Only once the owner has agreed; it can't be undone with one call.",
                     {"theme": THEME, "into": THEME}, ["theme", "into"]),
    "split_theme": (split_theme, False, "Split some ideas out of a theme into a new theme of the owner's (pinned; "
                    "Idea Machine writes its description on its next run). open_theme first for the idea ids. Only "
                    "once the owner has agreed to the name and the ideas.",
                    {"theme": THEME, "name": {"type": "string", "maxLength": 80},
                     "idea_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": 500}},
                    ["theme", "name", "idea_ids"]),
    "file_idea": (file_idea, False, "Put one idea into a theme, or take it out (remove=true). Idea Machine won't "
                  "undo it on its own. Only when the owner asks or agrees.",
                  {"idea_id": ID, "theme": THEME, "remove": {"type": "boolean", "default": False}},
                  ["idea_id", "theme"]),
    "similar_ideas": (similar_ideas, True, "Ideas close in meaning: like one idea (idea_id), or like some words "
                      "(text), e.g. to check whether the owner has had a thought before, or to find what an idea "
                      "connects to beyond its links.",
                      {"idea_id": ID, "text": {"type": "string", "maxLength": 1000},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8}}, []),
    "add_idea": (add_idea, False, "Add the owner's own idea to the lattice, worded as they approved: a short title, "
                 "a one-to-three-sentence statement, optional context, and optional ideas it connects to. Only after "
                 "they approve the wording; similar_ideas first for one it may repeat.",
                 {"title": {"type": "string", "maxLength": 120}, "statement": {"type": "string", "maxLength": 1000},
                  "note": {"type": "string", "maxLength": 1000,
                           "description": "Context: where it came from, why it matters"},
                  "link_to": {"type": "array", "items": ID, "maxItems": 10}}, ["title", "statement"]),
    "decide_item": (decide_item, False, "Record the owner's own verdict on one capture (a spot check, or any item "
                    "they chose to judge). Never decide for them.",
                    {"item_id": {"type": "string", "description": "A capture's item_id from review_digest"},
                     "verdict": {"type": "string", "enum": ["keep", "star", "discard"]},
                     "note": {"type": "string", "maxLength": 500, "description": "Why, if the owner said"},
                     "spot_check": {"type": "boolean", "default": False,
                                    "description": "true for an item from review_digest's spot_checks; false for one "
                                                   "the owner chose to judge from the rest"}},
                    ["item_id", "verdict"]),
    "keep_rest": (keep_rest, False, "Keep, in one go, the non-spot-check captures from review_digest that the "
                  "owner agreed to keep without reading. Only after they say so.",
                  {"item_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 500,
                                "description": "rest.items ids from review_digest, minus any the owner wants to judge"}},
                  ["item_ids"]),
    "star_idea": (star_idea, False, "Star an idea (or unstar it with starred=false). Only when the owner asks.",
                  {"idea_id": ID, "starred": {"type": "boolean", "default": True}}, ["idea_id"]),
    "link_ideas": (link_ideas, False, "Link two ideas the owner sees as connected (or remove the link with "
                   "unlink=true). Only when the owner asks.",
                   {"idea_id": ID, "other_idea_id": ID, "unlink": {"type": "boolean", "default": False}},
                   ["idea_id", "other_idea_id"]),
    "compare_pair": (compare_pair, True, "Two ideas to ask the owner \"which is the better idea?\" about, chosen "
                     "to teach Idea Machine's ranking the most. Only when the owner wants to compare ideas.", {}, []),
    "record_comparison": (record_comparison, False, "Record the owner's answer to compare_pair: which idea they "
                          "said is better (first or second, in the order you read them) or tie.",
                          {"first_idea_id": ID, "second_idea_id": ID,
                           "winner": {"type": "string", "enum": ["first", "second", "tie"]},
                           "note": {"type": "string", "maxLength": 500, "description": "Why, if the owner said"}},
                          ["first_idea_id", "second_idea_id", "winner"]),
    "note_idea": (note_idea, False, "Set the owner's context note on an idea (replaces the old one; empty clears "
                  "it). Only when the owner asks.",
                  {"idea_id": ID, "note": {"type": "string", "maxLength": 1000}}, ["idea_id", "note"]),
}


# --- The demo view (/demo/mcp) -----------------------------------------------------------------------------------
# For showing the lattice to someone through a Claude app, as the app's demo mode does: read-only, the private
# review queue left out, ideas and themes tagged sensitive (pub.sensitivity) in the hidden categories withheld from
# every answer, digest summaries and points citing a withheld idea dropped, and swearing blacked out. The categories
# are the server's (LATTICE_DEMO_HIDE, comma-separated; the app's choices live on the phone); my own mark on an idea
# or theme always decides.

DEMO_TOOLS = ("search_ideas", "get_idea", "best_ideas", "list_themes", "open_theme", "whats_new", "weekly_digest",
              "similar_ideas", "draft_material")
DEMO_INSTRUCTIONS = """Lattice (demo) is a read-only view of the owner's idea lattice, curated for showing it to other
people: ideas Idea Machine captured from the owner's recorded conversations, grouped into themes and connected. Some
ideas and themes are withheld and swearing is blacked out; don't speculate about or mention what might be missing.
Speak to the owner as "you". In quotes, "me" is the owner; S1, S2... are other people. Search or open a theme before
asking for ids. To draft something, call draft_material, then ask what to make before writing."""
DEMO_DEFAULT = "sex,gender_politics,political_extremism,violence,racism"
SWEARING = re.compile(r"\b(" + "|".join([
    r"fuck\w*", r"motherfuck\w*", r"shit\w*", r"bullshit\w*", r"bitch\w*", r"cunts?", r"cocks?", r"cocksuck\w*",
    r"dick(head)?s?", r"puss(y|ies)", r"ass(hole)?s?", r"arse(hole)?s?", r"bastards?", r"piss\w*", r"sluts?", r"whores?",
    r"damn\w*", r"goddamn\w*", r"twats?", r"wank\w*", r"douche\w*", r"fag(got)?s?", r"n[i1]gg\w*", r"retards?", r"spics?",
    r"chinks?", r"kikes?"]) + r")\b", re.IGNORECASE)   # the same list as the app's


def demo_categories() -> set:
    return {c.strip() for c in os.environ.get("LATTICE_DEMO_HIDE", DEMO_DEFAULT).split(",") if c.strip()}


class _Withheld:
    """What the demo view keeps back, worked out once per call."""

    def __init__(self, conn):
        cats, sens = demo_categories(), data.sensitivity(conn)
        hide = lambda s: s["sensitive"] if s["by"] == "owner" else bool(set(s["categories"]) & cats)   # noqa: E731
        self.ideas = {i for i, s in sens["ideas"].items() if hide(s)}
        self.themes = {i for i, s in sens["themes"].items() if hide(s)}
        self.theme_names = {n for i, n in conn.execute("SELECT theme_id::text, name FROM pub.themes") if i in self.themes}
        self.titles = {t for i, t in conn.execute("SELECT idea_id::text, title FROM pub.ideas") if i in self.ideas}
        self.ids = self.ideas | self.themes

    def check(self, conn, name: str, args: dict) -> None:
        """Asking for a withheld idea or theme gets the answer a missing one would."""
        if args.get("idea_id") is not None and str(args["idea_id"]) in self.ideas:
            raise ToolError("no such idea (it may have been forgotten)")
        if name in ("open_theme", "draft_material") and args.get("theme") is not None:
            if _theme(conn, args["theme"])[0] in self.themes:
                raise ToolError(f"no theme matches {args['theme']!r}; list_themes shows them all")
        if any(str(i) in self.ideas for i in args.get("idea_ids") or []):
            raise ToolError("no such idea (it may have been forgotten)")

    def clean(self, x, key: str = ""):
        if isinstance(x, dict):
            return {k: self.clean(v, k) for k, v in x.items()
                    if k not in ("captures_waiting_for_review", "kept_unread_worth_a_look")}
        if isinstance(x, list):
            out = []
            for el in x:
                if isinstance(el, dict) and any(str(el.get(k)) in self.ids for k in ("id", "idea_id", "other_id", "a", "b")):
                    continue
                if isinstance(el, str) and (el in self.theme_names or el in self.titles):
                    continue
                out.append(self.clean(el, key))
            return out
        if isinstance(x, str):
            return SWEARING.sub(lambda m: "\u2588" * len(m.group()), x)
        return x


def _demo_digest(result: dict, held: _Withheld) -> dict:
    return {**result, "summary": "", "points": [p for p in result["points"]
                                                if not any(i["id"] in held.ideas for i in p["ideas"])]}


def tool_list(writes: bool = True, demo: bool = False) -> list[dict]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False},
             "annotations": {"readOnlyHint": ro, "destructiveHint": False, "openWorldHint": False}}
            for name, (_, ro, desc, props, req) in TOOLS.items()
            if (name in DEMO_TOOLS if demo else ro or writes)]


def call(conn, name: str, args: dict, writes: bool = True, demo: bool = False) -> dict:
    if name not in TOOLS or (demo and name not in DEMO_TOOLS) or not (TOOLS[name][1] or (writes and not demo)):
        raise UnknownTool(name)   # a write tool on a read-only grant, or anything outside the demo view, doesn't exist
    fn, _, _, props, req = TOOLS[name]
    try:
        missing = [k for k in req if k not in args]
        if missing:
            raise ToolError(f"missing {', '.join(missing)}")
        held = _Withheld(conn) if demo else None
        if held:
            held.check(conn, name, args)
            if name == "draft_material" and args.get("point") is not None:   # a withheld point isn't there
                d = _demo_digest(weekly_digest(conn, args.get("digest_week")), held)
                if not any(p["point"] == args["point"] for p in d["points"]):
                    raise ToolError("that digest has no such point")
        result = fn(conn, **{k: v for k, v in args.items() if k in props})
        if held:
            if name == "weekly_digest":
                result = _demo_digest(result, held)
            result = held.clean(result)
    except (ToolError, data.NotYet, ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}


def handle(message, connect=data.connect, writes: bool = True, demo: bool = False):
    """One JSON-RPC message in, its response out (None for a notification). `writes`: whether the caller may star,
    link and note (a read-only OAuth grant may not). `demo`: the read-only view with sensitive content held back."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    mid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if "id" not in message:   # a notification (notifications/initialized and friends): nothing to say
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {**SERVER, "title": "Lattice (demo)"} if demo else SERVER,
                             "instructions": DEMO_INSTRUCTIONS if demo else INSTRUCTIONS})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": tool_list(writes, demo)})
    if method == "tools/call":
        try:
            with connect() as conn:
                return _result(mid, call(conn, params.get("name"), params.get("arguments") or {}, writes, demo))
        except UnknownTool:
            return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
    return _error(mid, -32601, f"method not found: {method}")


def _result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
