"""Reads Idea Machine's published lattice (schema pub) as role lattice_app. Its one write: appending feedback events."""

import json
import os
from uuid import UUID

import psycopg

from lattice import meaning


def connect() -> psycopg.Connection:
    try:
        dsn = os.environ["LATTICE_DATABASE_URL"]
    except KeyError:
        raise SystemExit("LATTICE_DATABASE_URL is not set (role lattice_app on Idea Machine's database)") from None
    return psycopg.connect(dsn, autocommit=True)


def graph(conn) -> dict:
    ideas = conn.execute(
        """SELECT idea_id, title, statement, origin, archive_ref, themes, n_evidence, kept, discarded, starred,
                  first_said, last_said, corrected, created_at
           FROM pub.ideas""").fetchall()
    where, ranked = positions(conn), scores(conn)
    nodes = [{"id": str(i), "title": t, "statement": s, "origin": o, "ref": ref, "themes": list(th),
              "evidence": n, "kept": k, "discarded": d, "starred": st, "corrected": c, "created_at": made.isoformat(),
              "first_said": f.isoformat() if f else None, "last_said": last.isoformat() if last else None,
              "pos": where.get(i), "score": ranked.get(i, {}).get("score")}
             for i, t, s, o, ref, th, n, k, d, st, f, last, c, made in ideas]
    links = [{"source": str(a), "target": str(b), "kind": kind, "weight": w}
             for a, b, kind, w in conn.execute("SELECT a, b, kind, weight FROM pub.connections")]
    # Pinned first, then oldest first: a stable order, so a theme keeps its color as others come and go.
    themes = [{"id": str(i), "name": n, "description": d, "pinned": p, "proposed": o == "clustered", "ideas": c}
              for i, n, d, p, o, c in conn.execute(
                  """SELECT theme_id, name, description, pinned, origin, n_ideas FROM pub.themes
                     ORDER BY pinned DESC, created_at, name""")]
    return {"nodes": nodes, "links": links, "themes": themes}


def positions(conn) -> dict:
    """Each idea's place on the semantic map (x, y in about [-1, 1]); ideas not placed yet are left out."""
    return {i: [x, y] for i, x, y in conn.execute("SELECT idea_id, x, y FROM pub.idea_positions")}


def scores(conn) -> dict:
    """Each scored idea's ranking from Idea Machine (pub.idea_scores): score 0-1 and the reasons. Ideas not scored
    yet are left out."""
    rows = conn.execute("""SELECT idea_id, score, generality, novelty, compression, outside_value, recurrence, why
                           FROM pub.idea_scores WHERE score IS NOT NULL""").fetchall()
    return {i: {"score": float(sc), "generality": g, "novelty": n, "compression": c, "outside_value": o,
                "recurrence": float(r) if r is not None else None, "why": why}
            for i, sc, g, n, c, o, r, why in rows}


def idea(conn, idea_id: str) -> dict | None:
    UUID(idea_id)  # reject anything that isn't a UUID before it reaches SQL
    row = conn.execute("""SELECT title, statement, origin, archive_ref, notes, themes, starred, claude_title,
                                 claude_statement, my_note, corrected
                          FROM pub.ideas WHERE idea_id = %s""", (idea_id,)).fetchone()
    if row is None:
        return None
    title, statement, origin, ref, notes, themes, starred, claude_title, claude_statement, my_note, corrected = row
    evidence = evidence_of(conn, idea_id).get(UUID(idea_id), [])
    return {"id": idea_id, "title": title, "statement": statement, "origin": origin, "ref": ref, "notes": notes,
            "themes": list(themes), "starred": starred, "evidence": evidence, "corrected": corrected,
            "claude_title": claude_title, "claude_statement": claude_statement, "my_note": my_note}


def evidence_of(conn, idea_id: str | None = None) -> dict:
    """Evidence by idea: one idea's, or every idea's. `how` is how its verdict was made ('one', 'audit', or 'bulk':
    kept unread), None without a verdict."""
    rows = conn.execute("""SELECT idea_id, item_id, kind, said_by, quote, gist, confidence, said_at, verdict, relation,
                                  how
                           FROM pub.evidence WHERE %(idea)s::uuid IS NULL OR idea_id = %(idea)s ORDER BY said_at""",
                        {"idea": idea_id}).fetchall()
    out: dict = {}
    for i, item, k, who, q, g, c, at, v, rel, how in rows:
        out.setdefault(i, []).append({"item_id": str(item), "kind": k, "said_by": who, "quote": q, "gist": g,
                                      "confidence": c, "said_at": at.isoformat(), "verdict": v, "relation": rel,
                                      "how": how})
    return out


def details(conn) -> dict:
    """Every idea as the map's panel shows it, for the copy the app keeps on the device."""
    evidence, ranked = evidence_of(conn), scores(conn)
    return {str(i): {"ranking": ranked.get(i), "id": str(i), "title": t, "statement": s, "origin": o, "ref": ref, "notes": notes,
                     "themes": list(th), "starred": st, "evidence": evidence.get(i, []), "corrected": c,
                     "claude_title": ct, "claude_statement": cs, "my_note": mine}
            for i, t, s, o, ref, notes, th, st, ct, cs, mine, c in conn.execute(
                """SELECT idea_id, title, statement, origin, archive_ref, notes, themes, starred, claude_title,
                          claude_statement, my_note, corrected FROM pub.ideas""")}


def snapshot(conn) -> dict:
    """Everything the app shows, in one piece. The device replaces its copy with this on every sync, so what Idea
    Machine forgets is gone from the phone too."""
    try:
        waiting = proposals(conn)
    except NotYet:
        waiting = []
    return {"graph": graph(conn), "ideas": details(conn), "proposals": waiting, "status": status(conn),
            "digests": digests(conn), "neighbours": meaning.neighbours(conn), "sensitivity": sensitivity(conn),
            "vocab": vocab(conn)}


VOCAB_SOURCES = ("correction", "repair", "archive", "added")


def vocab(conn) -> list[dict]:
    """The words Hearsay boosts when it transcribes (pub.vocab): in the list, suggested, or removed, each with where
    it came from and, for a suggestion, why. Idea Machine writes the approved ones to the vocab.json Hearsay reads."""
    rows = conn.execute("""SELECT term, source, heard_as, status, idea_id, example FROM pub.vocab
                           ORDER BY status, lower(term)""").fetchall()
    return [{"term": t, "source": s, "heard_as": h, "status": st, "idea_id": str(i) if i else None, "example": ex}
            for t, s, h, st, i, ex in rows]


def sensitivity(conn) -> dict:
    """What demo mode hides (pub.sensitivity): ideas and themes Claude tagged or I marked, with categories and who
    decided. {"ideas": {id: {...}}, "themes": {id: {...}}}. Without the view this raises: demo mode fails closed."""
    out: dict = {"ideas": {}, "themes": {}}
    for kind, i, sensitive, cats, by in conn.execute("SELECT kind, id, sensitive, categories, by FROM pub.sensitivity"):
        out["ideas" if kind == "idea" else "themes"][str(i)] = {"sensitive": sensitive, "categories": list(cats or []),
                                                               "by": by}
    return out


def digests(conn, week: str | None = None) -> list[dict]:
    """Idea Machine's weekly digests (pub.digests, slice 6), newest first; or the one for the week holding `week`
    (an ISO date). Each point: kind, title, body, idea_ids, and mine ('star', 'dismiss' or None). A digest can vanish
    between syncs: forgetting any speech behind it deletes it whole."""
    rows = conn.execute(
        """SELECT digest_id, week_start, period_start, period_end, summary, written_at, points FROM pub.digests
           WHERE %(week)s::date IS NULL OR week_start = date_trunc('week', %(week)s::date)::date
           ORDER BY week_start DESC""", {"week": week}).fetchall()
    return [{"id": i, "week_start": w.isoformat(), "period_start": a.isoformat(), "period_end": b.isoformat(),
             "summary": s, "written_at": at.isoformat(), "points": list(p or [])}
            for i, w, a, b, s, at, p in rows]


EVENTS = {"item_keep": "item_id", "item_discard": "item_id", "item_star": "item_id",
          "star": "idea_id", "unstar": "idea_id", "idea_correct": "idea_id", "idea_note": "idea_id",
          "pin_theme": "theme_id", "unpin_theme": "theme_id", "reject_theme": "theme_id",
          "rename_theme": "theme_id", "merge_theme": "theme_id", "split_theme": "theme_id",
          "file_idea": "idea_id", "unfile_idea": "idea_id",
          "idea_sensitive": "idea_id", "theme_sensitive": "theme_id",   # my call for demo mode
          "link": "idea_id", "unlink": "idea_id", "idea_compare": "idea_id", "idea_add": "idea_id",
          "idea_keep": "idea_id", "idea_unkeep": "idea_id", "idea_dismiss": "idea_id",   # M2: only I make ideas
          "digest_star": None, "digest_dismiss": None,   # a digest point: payload digest_id and point
          "vocab_add": None, "vocab_remove": None}       # a word for transcription: payload term
WINNERS = ("first", "second", "tie")
KEEP_AS = ("canonical", "pinned")


class NotYet(Exception):
    """Idea Machine's database doesn't take this kind of event yet (its migration isn't deployed): try again later."""


def proposals(conn) -> list[dict]:
    """The candidates Idea Machine proposes I keep as ideas (M2), heaviest first."""
    try:
        rows = conn.execute("""SELECT idea_id, title, statement, weight, made_at FROM pub.proposals
                               ORDER BY weight DESC, made_at, idea_id""").fetchall()
    except psycopg.errors.UndefinedTable:
        raise NotYet("Idea Machine doesn't make proposals yet") from None
    return [{"id": str(i), "title": t, "statement": st, "weight": w, "proposed_at": m.isoformat()}
            for i, t, st, w, m in rows]


def record(conn, event: dict, source: str = "lattice") -> int:
    """Append one feedback event. Raises ValueError for anything not in EVENTS. `source`: the app, or 'mcp'."""
    kind = event.get("kind")
    if kind not in EVENTS:
        raise ValueError(f"unknown kind {kind!r}")
    target = EVENTS[kind]
    if kind.startswith("vocab_"):   # a word, named by itself rather than an id column
        return _record_term(conn, kind, event, source)
    if target is None:   # a digest point, named by numbers rather than an id column
        return _record_digest(conn, kind, event, source)
    ref = str(UUID(str(event.get(target, ""))))   # ValueError unless it's a UUID
    payload = {}
    if kind.startswith("item_") and str(event.get("note") or "").strip():   # why I kept, starred or discarded it
        payload["note"] = str(event["note"]).strip()[:500]
    if kind == "item_keep" and event.get("bulk") is True:   # kept with the rest after an audit, not read one by one
        payload["bulk"] = True
    elif kind.startswith("item_") and event.get("audit") is True:   # a spot check: the unbiased sample by confidence
        payload["audit"] = True
    elif kind == "idea_correct":   # my wording; an empty field reverts to Claude's
        payload = {k: str(event[k]).strip()[:limit] for k, limit in (("title", 120), ("statement", 1000)) if k in event}
        if not payload:
            raise ValueError("a correction needs a title or a statement")
    elif kind == "idea_note":   # context for the idea; empty clears it
        payload = {"note": str(event.get("note") or "").strip()[:1000]}
    elif kind == "idea_add":   # my own idea; idea_id is new, made by the client so a resend is the same idea
        payload = {k: str(event.get(k) or "").strip()[:limit] for k, limit in (("title", 120), ("statement", 1000))}
        if not all(payload.values()):
            raise ValueError("a new idea needs a title and a statement")
        if str(event.get("note") or "").strip():
            payload["note"] = str(event["note"]).strip()[:1000]
        links = event.get("link_to") or []
        if not isinstance(links, list) or len(links) > 10:
            raise ValueError("link_to: up to 10 idea ids")
        links = sorted({str(UUID(str(x))) for x in links} - {ref})
        if links:
            payload["link_to"] = links
    elif kind == "idea_keep":   # canonical: weighted in the graph; pinned: kept and visible, no weight
        if event.get("as") not in KEEP_AS:
            raise ValueError(f"keep as one of {', '.join(KEEP_AS)}")
        payload = {"as": event["as"]}
    elif kind in ("idea_sensitive", "theme_sensitive"):
        if not isinstance(event.get("sensitive"), bool):
            raise ValueError("sensitive must be true or false")
        payload = {"sensitive": event["sensitive"]}
    elif kind == "rename_theme":
        payload = {"name": str(event.get("name") or "").strip()[:80]}
        if not payload["name"]:
            raise ValueError("a theme needs a name")
    elif kind == "merge_theme":   # theme_id is absorbed into `into`
        payload = {"into": str(UUID(str(event.get("into", ""))))}
        if payload["into"] == ref:
            raise ValueError("a theme can't merge into itself")
    elif kind == "split_theme":   # these ideas leave theme_id for a new theme of mine
        ideas = event.get("idea_ids")
        if not isinstance(ideas, list) or not 1 <= len(ideas) <= 500:
            raise ValueError("idea_ids: the ideas that move to the new theme")
        payload = {"name": str(event.get("name") or "").strip()[:80], "idea_ids": sorted({str(UUID(str(x))) for x in ideas})}
        if not payload["name"]:
            raise ValueError("the new theme needs a name")
        # Made by the client, so the app shows the theme (and can rename or file into it) before a sync.
        payload["new_theme_id"] = str(UUID(str(event.get("new_theme_id", ""))))
    elif kind in ("file_idea", "unfile_idea"):   # one idea into or out of one theme, by hand: theme_id goes below
        str(UUID(str(event.get("theme_id", ""))))
    other = None
    if kind in ("link", "unlink", "idea_compare"):   # links are undirected; a comparison keeps the order shown
        other = str(UUID(str(event.get("other_idea_id", ""))))
        if other == ref:
            raise ValueError("an idea can't link to or be compared with itself")
    if kind == "idea_compare":   # idea_id was shown first, other_idea_id second
        if event.get("winner") not in WINNERS:
            raise ValueError(f"winner must be one of {', '.join(WINNERS)}")
        payload = {"winner": event["winner"]}
        if str(event.get("note") or "").strip():
            payload["note"] = str(event["note"]).strip()[:500]
    theme = str(UUID(str(event["theme_id"]))) if kind in ("file_idea", "unfile_idea") else None
    try:
        if theme:   # filing names both the idea and the theme, each in its own column
            return conn.execute(
                """INSERT INTO pub.feedback_events (kind, idea_id, theme_id, payload, source)
                   VALUES (%s, %s, %s, '{}', %s) RETURNING event_id""", (kind, ref, theme, source)).fetchone()[0]
        return conn.execute(
            f"""INSERT INTO pub.feedback_events (kind, {target}, other_idea_id, payload, source)
                VALUES (%s, %s, %s, %s, %s) RETURNING event_id""",
            (kind, ref, other, json.dumps(payload), source)).fetchone()[0]
    except psycopg.errors.CheckViolation as e:
        # Only an unknown kind is worth retrying; any other check means the event itself is wrong, and always will be.
        if e.diag.constraint_name == "feedback_events_kind_check":
            raise NotYet(f"Idea Machine doesn't accept {kind} events yet") from None
        raise ValueError(f"Idea Machine refused the event ({e.diag.constraint_name})") from None


def _record_term(conn, kind: str, event: dict, source: str) -> int:
    term = " ".join(str(event.get("term") or "").split())
    if not term or len(term) > 60:
        raise ValueError("a word or short phrase, up to 60 characters")
    payload = {"term": term}
    if kind == "vocab_add" and event.get("source") is not None:   # approving a suggestion keeps where it came from
        if event["source"] not in VOCAB_SOURCES:
            raise ValueError(f"source must be one of {', '.join(VOCAB_SOURCES)}")
        payload["source"] = event["source"]
    return _insert_unnamed(conn, kind, payload, source)


def _insert_unnamed(conn, kind: str, payload: dict, source: str) -> int:
    try:
        return conn.execute("INSERT INTO pub.feedback_events (kind, payload, source) VALUES (%s, %s, %s) RETURNING event_id",
                            (kind, json.dumps(payload), source)).fetchone()[0]
    except psycopg.errors.CheckViolation as e:
        if e.diag.constraint_name == "feedback_events_kind_check":
            raise NotYet(f"Idea Machine doesn't accept {kind} events yet") from None
        raise ValueError(f"Idea Machine refused the event ({e.diag.constraint_name})") from None


def _record_digest(conn, kind: str, event: dict, source: str) -> int:
    payload = {}
    for k in ("digest_id", "point"):
        v = event.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"{k} must be a positive whole number")
        payload[k] = v
    if str(event.get("note") or "").strip():
        payload["note"] = str(event["note"]).strip()[:500]
    return _insert_unnamed(conn, kind, payload, source)


def status(conn) -> dict:
    """The recent hourly runs and this month's spend, for the header's status line."""
    runs = [{"started_at": a.isoformat(), "finished_at": b.isoformat() if b else None, "error": err,
             "warnings": list(w or []), "conversations": conv, "read": read, "captured": cap, "refused": ref,
             "cost": float(cost or 0), "cap": float(mc) if mc is not None else None}
            for a, b, err, w, conv, read, cap, ref, cost, mc in conn.execute(
                """SELECT started_at, finished_at, error, warnings, conversations_changed, episodes_read, captured,
                          refused, cost_usd, monthly_cap_usd FROM pub.runs ORDER BY started_at DESC LIMIT 24""")]
    spent = conn.execute("SELECT month_to_date FROM pub.spend").fetchone()[0]
    now = conn.execute("SELECT now()").fetchone()[0]
    return {"now": now.isoformat(), "spent": float(spent), "runs": runs}
