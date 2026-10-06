"""Reads Idea Machine's published lattice (schema pub) as role lattice_app. Read-only for now."""

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
                  first_said, last_said
           FROM pub.ideas""").fetchall()
    nodes = [{"id": str(i), "title": t, "statement": s, "origin": o, "ref": ref, "themes": list(th),
              "evidence": n, "kept": k, "discarded": d, "starred": st,
              "first_said": f.isoformat() if f else None, "last_said": last.isoformat() if last else None}
             for i, t, s, o, ref, th, n, k, d, st, f, last in ideas]
    links = [{"source": str(a), "target": str(b), "kind": kind, "weight": w}
             for a, b, kind, w in conn.execute("SELECT a, b, kind, weight FROM pub.connections")]
    themes = [{"id": str(i), "name": n, "pinned": p, "ideas": c}
              for i, n, p, c in conn.execute("SELECT theme_id, name, pinned, n_ideas FROM pub.themes ORDER BY name")]
    return {"nodes": nodes, "links": links, "themes": themes}


def idea(conn, idea_id: str) -> dict | None:
    UUID(idea_id)  # reject anything that isn't a UUID before it reaches SQL
    row = conn.execute("""SELECT title, statement, origin, archive_ref, notes, themes, starred
                          FROM pub.ideas WHERE idea_id = %s""", (idea_id,)).fetchone()
    if row is None:
        return None
    title, statement, origin, ref, notes, themes, starred = row
    evidence = [{"kind": k, "said_by": who, "quote": q, "gist": g, "confidence": c,
                 "said_at": at.isoformat(), "verdict": v, "relation": rel}
                for k, who, q, g, c, at, v, rel in conn.execute(
                    """SELECT kind, said_by, quote, gist, confidence, said_at, verdict, relation FROM pub.evidence
                       WHERE idea_id = %s ORDER BY said_at""", (idea_id,))]
    return {"title": title, "statement": statement, "origin": origin, "ref": ref, "notes": notes,
            "themes": list(themes), "starred": starred, "evidence": evidence}
