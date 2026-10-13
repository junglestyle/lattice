"""Talk to it: an MCP server over pub (Idea Machine's ROADMAP, Phase 2 slice 7).

Streamable HTTP at /mcp, answering each JSON-RPC request with plain JSON (no SSE stream, no sessions). It's behind
the Lattice token as a bearer header and reachable only on the tailnet, like the rest of Lattice.

What leaves through it follows Idea Machine's privacy policy B: quotes go out, but speakers are pseudonymized per
answer ("me", "unknown", then S1, S2... in order of first appearance). Flags made here (star, link, note) are
feedback events like the app's, with source 'mcp'; so are review verdicts (spot checks one at a time, the rest
kept in bulk and flagged so).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from lattice import data

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]   # newest first; the first is offered when the client's isn't
SERVER = {"name": "lattice", "title": "Lattice", "version": "0.1.0"}
INSTRUCTIONS = """Lattice is the owner's idea lattice: ideas Idea Machine captured from their recorded conversations
(and seeded from an old ChatGPT archive), grouped into themes, connected to each other, each backed by evidence
quotes. Speak to the owner as "you". In quotes, "me" is the owner; S1, S2... are other people, renumbered in every
answer. Search or open a theme before asking the owner for ids. Starring, linking and notes are the owner's
feedback: do them when they ask, not on your own initiative.

To review new captures, call review_digest: read the owner the spot checks one at a time and record their own
verdict on each with decide_item (never your guess), then offer to keep the rest with keep_rest. The owner's
default is to keep: the lattice gets its value from connections as it grows. Captures kept that way come back
through look_again once their idea turns out to matter; offer them after the spot checks, and record the owner's
verdict with decide_item. Discards are the owner's high-signal verdict: when they discard, ask why in a few words
and pass it as the note.

When the owner wants to add an idea from your conversation: search_ideas first, and if an existing idea says the
same, offer a note or a link instead. Otherwise draft a short title and a one-to-three-sentence statement in the
owner's own terms, read them back, and call add_idea only once they approve the wording."""


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


def open_theme(conn, theme: str) -> dict:
    row = conn.execute("""SELECT theme_id, name, description FROM pub.themes
                          WHERE theme_id::text = %(t)s OR lower(name) = lower(%(t)s)
                          ORDER BY theme_id::text = %(t)s DESC LIMIT 1""", {"t": str(theme).strip()}).fetchone()
    if row is None:
        row = conn.execute("SELECT theme_id, name, description FROM pub.themes WHERE name ILIKE %s ORDER BY name LIMIT 1",
                           (f"%{str(theme).strip()}%",)).fetchone()
    if row is None:
        raise ToolError(f"no theme matches {theme!r}; list_themes shows them all")
    tid, name, description = row
    ideas = conn.execute(f"SELECT {BRIEF} FROM pub.ideas WHERE %s = ANY(themes) ORDER BY starred DESC, n_evidence DESC, title",
                         (name,)).fetchall()
    return {"id": str(tid), "name": name, "description": description, "ideas": [_brief(r) for r in ideas]}


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
TOOLS = {
    "search_ideas": (search_ideas, True, "Find ideas whose title, statement, notes or themes contain every word "
                     "of the query.",
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
    "whats_new": (whats_new, True, "Ideas added since a date (default: the last 7 days), proposed themes waiting "
                  "for a decision, and how many captures wait for review.",
                  {"since": {"type": "string", "description": "ISO date or time"}}, []),
    "review_digest": (review_digest, True, "The captures waiting for review: a few spot checks, picked across "
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
    "add_idea": (add_idea, False, "Add the owner's own idea to the lattice, worded as they approved: a short title, "
                 "a one-to-three-sentence statement, optional context, and optional ideas it connects to. Only after "
                 "they approve the wording; search_ideas first for one it may repeat.",
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


def tool_list(writes: bool = True) -> list[dict]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False},
             "annotations": {"readOnlyHint": ro, "destructiveHint": False, "openWorldHint": False}}
            for name, (_, ro, desc, props, req) in TOOLS.items() if ro or writes]


def call(conn, name: str, args: dict, writes: bool = True) -> dict:
    if name not in TOOLS or not (TOOLS[name][1] or writes):   # a write tool on a read-only grant doesn't exist
        raise UnknownTool(name)
    fn, _, _, props, req = TOOLS[name]
    try:
        missing = [k for k in req if k not in args]
        if missing:
            raise ToolError(f"missing {', '.join(missing)}")
        result = fn(conn, **{k: v for k, v in args.items() if k in props})
    except (ToolError, data.NotYet, ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}


def handle(message, connect=data.connect, writes: bool = True):
    """One JSON-RPC message in, its response out (None for a notification). `writes`: whether the caller may star,
    link and note (a read-only OAuth grant may not)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    mid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if "id" not in message:   # a notification (notifications/initialized and friends): nothing to say
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                             "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": tool_list(writes)})
    if method == "tools/call":
        try:
            with connect() as conn:
                return _result(mid, call(conn, params.get("name"), params.get("arguments") or {}, writes))
        except UnknownTool:
            return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
    return _error(mid, -32601, f"method not found: {method}")


def _result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
