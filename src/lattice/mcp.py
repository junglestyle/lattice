"""Talk to it: an MCP server over pub (Idea Machine's ROADMAP, Phase 2 slice 7).

Streamable HTTP at /mcp, answering each JSON-RPC request with plain JSON (no SSE stream, no sessions). It's behind
the Lattice token as a bearer header and reachable only on the tailnet, like the rest of Lattice.

What leaves through it follows Idea Machine's privacy policy B: quotes go out, but speakers are pseudonymized per
answer ("me", "unknown", then S1, S2... in order of first appearance). Flags made here (star, link, note) are
feedback events like the app's, with source 'mcp'.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from lattice import data

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]   # newest first; the first is offered when the client's isn't
SERVER = {"name": "lattice", "title": "Lattice", "version": "0.1.0"}
INSTRUCTIONS = """Lattice is the owner's idea lattice: ideas Idea Machine captured from their recorded conversations
(and seeded from an old ChatGPT archive), grouped into themes, connected to each other, each backed by evidence
quotes. Speak to the owner as "you". In quotes, "me" is the owner; S1, S2... are other people, renumbered in every
answer. Search or open a theme before asking the owner for ids. Starring, linking and notes are the owner's
feedback: do them when they ask, not on your own initiative."""


def _pseudonymize(rows: list[dict], key: str = "said_by") -> list[dict]:
    alias: dict = {}
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
        raise ToolError(f"{name} must be an idea id (a UUID) from search_ideas, open_theme or whats_new") from None


class ToolError(Exception):
    """A mistake the model can fix: reported as a tool result with isError, not a protocol error."""


class UnknownTool(Exception):
    pass


def _brief(row) -> dict:
    i, title, statement, themes, n, starred, made = row
    return {"id": str(i), "title": title, "statement": statement, "themes": list(themes), "evidence": n,
            "starred": starred, "created_at": made.isoformat()}


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
    total = conn.execute("SELECT count(*) FROM pub.ideas WHERE created_at >= %s", (start,)).fetchone()[0]
    return {"since": start.isoformat(), "new_ideas_total": total, "new_ideas": [_brief(r) for r in ideas],
            "proposed_themes": themes,
            "captures_waiting_for_review": waiting}


def star_idea(conn, idea_id: str, starred: bool = True) -> dict:
    data.record(conn, {"kind": "star" if starred else "unstar", "idea_id": _uuid(idea_id)}, source="mcp")
    return {"idea_id": idea_id, "starred": bool(starred)}


def link_ideas(conn, idea_id: str, other_idea_id: str, unlink: bool = False) -> dict:
    a, b = _uuid(idea_id), _uuid(other_idea_id, "other_idea_id")
    if a == b:
        raise ToolError("an idea can't link to itself")
    data.record(conn, {"kind": "unlink" if unlink else "link", "idea_id": a, "other_idea_id": b}, source="mcp")
    return {"idea_id": a, "other_idea_id": b, "linked": not unlink}


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
    "list_themes": (list_themes, True, "Every theme with its size and whether it's pinned or only proposed.", {}, []),
    "open_theme": (open_theme, True, "The ideas in one theme, starred and best-evidenced first.",
                   {"theme": {"type": "string", "description": "A theme's name (or part of it) or id"}}, ["theme"]),
    "whats_new": (whats_new, True, "Ideas added since a date (default: the last 7 days), proposed themes waiting "
                  "for a decision, and how many captures wait for review.",
                  {"since": {"type": "string", "description": "ISO date or time"}}, []),
    "star_idea": (star_idea, False, "Star an idea (or unstar it with starred=false). Only when the owner asks.",
                  {"idea_id": ID, "starred": {"type": "boolean", "default": True}}, ["idea_id"]),
    "link_ideas": (link_ideas, False, "Link two ideas the owner sees as connected (or remove the link with "
                   "unlink=true). Only when the owner asks.",
                   {"idea_id": ID, "other_idea_id": ID, "unlink": {"type": "boolean", "default": False}},
                   ["idea_id", "other_idea_id"]),
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
    except (ToolError, ValueError, TypeError) as e:
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
