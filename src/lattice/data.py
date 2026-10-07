"""Reads Idea Machine's published lattice (schema pub) as role lattice_app. Its one write: appending feedback events."""

import json
import os
from uuid import UUID

import psycopg


def connect() -> psycopg.Connection:
    try:
        dsn = os.environ["LATTICE_DATABASE_URL"]
    except KeyError:
        raise SystemExit("LATTICE_DATABASE_URL is not set (role lattice_app on Idea Machine's database)") from None
    return psycopg.connect(dsn, autocommit=True)


def graph(conn) -> dict:
    ideas = conn.execute(
        """SELECT idea_id, title, statement, origin, archive_ref, themes, n_evidence, kept, discarded, starred,
                  first_said, last_said, corrected
           FROM pub.ideas""").fetchall()
    nodes = [{"id": str(i), "title": t, "statement": s, "origin": o, "ref": ref, "themes": list(th),
              "evidence": n, "kept": k, "discarded": d, "starred": st, "corrected": c,
              "first_said": f.isoformat() if f else None, "last_said": last.isoformat() if last else None}
             for i, t, s, o, ref, th, n, k, d, st, f, last, c in ideas]
    links = [{"source": str(a), "target": str(b), "kind": kind, "weight": w}
             for a, b, kind, w in conn.execute("SELECT a, b, kind, weight FROM pub.connections")]
    # Pinned first, then oldest first: a stable order, so a theme keeps its color as others come and go.
    themes = [{"id": str(i), "name": n, "pinned": p, "proposed": o == "clustered", "ideas": c}
              for i, n, p, o, c in conn.execute("""SELECT theme_id, name, pinned, origin, n_ideas FROM pub.themes
                                                   ORDER BY pinned DESC, created_at, name""")]
    return {"nodes": nodes, "links": links, "themes": themes}


def idea(conn, idea_id: str) -> dict | None:
    UUID(idea_id)  # reject anything that isn't a UUID before it reaches SQL
    row = conn.execute("""SELECT title, statement, origin, archive_ref, notes, themes, starred, claude_title,
                                 claude_statement, my_note, corrected
                          FROM pub.ideas WHERE idea_id = %s""", (idea_id,)).fetchone()
    if row is None:
        return None
    title, statement, origin, ref, notes, themes, starred, claude_title, claude_statement, my_note, corrected = row
    evidence = [{"kind": k, "said_by": who, "quote": q, "gist": g, "confidence": c,
                 "said_at": at.isoformat(), "verdict": v, "relation": rel}
                for k, who, q, g, c, at, v, rel in conn.execute(
                    """SELECT kind, said_by, quote, gist, confidence, said_at, verdict, relation FROM pub.evidence
                       WHERE idea_id = %s ORDER BY said_at""", (idea_id,))]
    return {"id": idea_id, "title": title, "statement": statement, "origin": origin, "ref": ref, "notes": notes,
            "themes": list(themes), "starred": starred, "evidence": evidence, "corrected": corrected,
            "claude_title": claude_title, "claude_statement": claude_statement, "my_note": my_note}


def review(conn, limit: int = 50) -> dict:
    """Captures waiting for my verdict, most confident first, each with the conversation around it."""
    rows = conn.execute(
        """SELECT r.item_id, r.kind, r.said_by, r.quote, r.gist, r.themes, r.confidence, r.said_at, r.idea_id,
                  d.title, d.statement, d.my_note
           FROM pub.review_items r LEFT JOIN pub.ideas d USING (idea_id)
           ORDER BY r.confidence DESC, r.said_at DESC LIMIT %s""", (limit,)).fetchall()
    total = conn.execute("SELECT count(*) FROM pub.review_items").fetchone()[0]
    ids = [r[0] for r in rows]
    context: dict = {}
    for item_id, source, speaker, text in conn.execute(
            """SELECT item_id, source, speaker, text FROM pub.item_context WHERE item_id = ANY(%s)
               ORDER BY item_id, ord""", (ids,)):
        context.setdefault(item_id, []).append({"source": source, "speaker": speaker, "text": text})
    items = [{"id": str(i), "kind": k, "said_by": who, "quote": q, "gist": g, "themes": list(th or []),
              "confidence": c, "said_at": at.isoformat(), "context": context.get(i, []),
              "idea": {"id": str(iid), "title": t, "statement": st, "note": note} if iid and t else None}
             for i, k, who, q, g, th, c, at, iid, t, st, note in rows]
    return {"total": total, "items": items}


# What Lattice may say, and about what (ROADMAP §3.5). Everything else is refused before it reaches the database.
EVENTS = {"item_keep": "item_id", "item_discard": "item_id", "item_star": "item_id",
          "star": "idea_id", "unstar": "idea_id", "idea_correct": "idea_id", "idea_note": "idea_id",
          "pin_theme": "theme_id", "unpin_theme": "theme_id", "reject_theme": "theme_id"}


def record(conn, event: dict) -> int:
    """Append one feedback event. Raises ValueError for anything not in EVENTS."""
    kind = event.get("kind")
    if kind not in EVENTS:
        raise ValueError(f"unknown kind {kind!r}")
    target = EVENTS[kind]
    ref = str(UUID(str(event.get(target, ""))))   # ValueError unless it's a UUID
    payload = {}
    if kind.startswith("item_") and str(event.get("note") or "").strip():   # why I kept, starred or discarded it
        payload["note"] = str(event["note"]).strip()[:500]
    elif kind == "idea_correct":   # my wording; an empty field reverts to Claude's
        payload = {k: str(event[k]).strip()[:limit] for k, limit in (("title", 120), ("statement", 1000)) if k in event}
        if not payload:
            raise ValueError("a correction needs a title or a statement")
    elif kind == "idea_note":   # context for the idea; empty clears it
        payload = {"note": str(event.get("note") or "").strip()[:1000]}
    return conn.execute(
        f"INSERT INTO pub.feedback_events (kind, {target}, payload) VALUES (%s, %s, %s) RETURNING event_id",
        (kind, ref, json.dumps(payload))).fetchone()[0]


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
