"""Reads Idea Machine's published lattice (schema pub) as role lattice_app. Its one write: appending feedback events."""

import hashlib
import json
import os
import random
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
    """Each idea's place on the semantic map (x, y in about [-1, 1]), once Idea Machine publishes them."""
    try:
        return {i: [x, y] for i, x, y in conn.execute("SELECT idea_id, x, y FROM pub.idea_positions")}
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        return {}


def scores(conn) -> dict:
    """Each scored idea's ranking from Idea Machine (pub.idea_scores): score 0-1 and the reasons, once it's published.
    Ideas not scored yet are left out."""
    try:
        rows = conn.execute("""SELECT idea_id, score, generality, novelty, compression, outside_value, recurrence, why
                               FROM pub.idea_scores WHERE score IS NOT NULL""").fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        return {}
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
    kept unread), None before Idea Machine publishes it."""
    sql = """SELECT idea_id, item_id, kind, said_by, quote, gist, confidence, said_at, verdict, relation, {how}
             FROM pub.evidence WHERE %(idea)s::uuid IS NULL OR idea_id = %(idea)s ORDER BY said_at"""
    try:
        rows = conn.execute(sql.format(how="how"), {"idea": idea_id}).fetchall()
    except psycopg.errors.UndefinedColumn:
        rows = conn.execute(sql.format(how="NULL::text"), {"idea": idea_id}).fetchall()
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


def compare_pairs(conn) -> list[dict]:
    """The pairs Idea Machine most wants my "which is better?" on (pub.compare_pairs), once it publishes them."""
    try:
        return [{"first": str(a), "second": str(b), "reason": r}
                for a, b, r in conn.execute("SELECT idea_id, other_idea_id, reason FROM pub.compare_pairs")]
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        return []


def compared(conn) -> list[list[str]]:
    """Pairs I've already compared, either way round, so they aren't asked again."""
    return [sorted([str(a), str(b)]) for a, b in conn.execute(
        "SELECT DISTINCT idea_id, other_idea_id FROM pub.feedback_events WHERE kind = 'idea_compare'")]


def bridges(g: dict) -> dict:
    """Ideas where themes meet, as the map's Bridges switch shows them (index.html's bridges(); keep the two alike):
    an idea touching two or more themes, through its own or its links' themes, that is in two itself or has a link
    to an idea sharing none of its themes. idea id -> the themes it spans."""
    themes_of = {n["id"]: n["themes"] for n in g["nodes"]}
    out = {}
    for n in g["nodes"]:
        touched, cross = set(n["themes"]), 0
        for link in g["links"]:
            other = link["target"] if link["source"] == n["id"] else link["source"] if link["target"] == n["id"] else None
            if not other or not themes_of.get(other):
                continue
            if not set(themes_of[other]) & set(n["themes"]):
                cross += 1
            touched |= set(themes_of[other])
        if len(touched) >= 2 and (len(n["themes"]) >= 2 or cross):
            out[n["id"]] = [t["name"] for t in g["themes"] if t["name"] in touched]
    return out


def why_it_matters(idea_id: str, g: dict, evidence: list, spans: dict, pairs: list) -> list[str]:
    """What makes a kept-unread capture worth a real verdict now (index.html's mattered(); keep the two alike).
    'related' links alone don't count: Idea Machine finds those for nearly everything."""
    kinds = {link["kind"] for link in g["links"] if idea_id in (link["source"], link["target"])}
    why = [w for k, w in (("mine", "you linked it"), ("evolves", "it evolves another idea"),
                          ("archive", "it echoes your archive")) if k in kinds]
    if idea_id in spans:
        why.append("it bridges " + " \u2194 ".join(spans[idea_id]))
    if any(idea_id in p for p in pairs):
        why.append("it came up in a comparison")
    if len(evidence) > 1:
        why.append("it was said again")
    return why


def look_again(conn) -> list[dict]:
    """Captures I kept in bulk without reading (pub.evidence.how = 'bulk') whose idea has since turned out to
    matter, most reasons first, then most recent."""
    evidence = evidence_of(conn)
    if not any(ev["how"] == "bulk" for evs in evidence.values() for ev in evs):
        return []
    g, pairs = graph(conn), compared(conn)
    spans, titles = bridges(g), {n["id"]: n["title"] for n in g["nodes"]}
    out = []
    for idea_id, evs in evidence.items():
        unread = [ev for ev in evs if ev["verdict"] == "keep" and ev["how"] == "bulk"]
        why = why_it_matters(str(idea_id), g, evs, spans, pairs) if unread else []
        out += [{**ev, "idea_id": str(idea_id), "idea": titles.get(str(idea_id)), "why": why} for ev in unread if why]
    return sorted(out, key=lambda x: (len(x["why"]), x["said_at"]), reverse=True)


def next_pair(conn, rng=random) -> dict | None:
    """One pair to ask "which is better?" about: Idea Machine's pick if it publishes any, else two ideas with
    neighbouring scores, else two ideas sharing a theme. Never a pair already compared; the order shown is random,
    so position bias averages out."""
    done = {tuple(p) for p in compared(conn)}
    fresh = lambda a, b: a != b and tuple(sorted([a, b])) not in done   # noqa: E731
    pair, reason = None, None
    suggested = [p for p in compare_pairs(conn) if fresh(p["first"], p["second"])]
    if suggested:
        pair, reason = (suggested[0]["first"], suggested[0]["second"]), suggested[0]["reason"]
    else:
        ideas = conn.execute("SELECT idea_id::text, themes FROM pub.ideas").fetchall()
        ranked = {str(k): v["score"] for k, v in scores(conn).items()}
        if len(ranked) >= 2:
            order = sorted(ranked, key=ranked.get)
            close = sorted((ranked[b] - ranked[a], a, b) for a, b in zip(order, order[1:]) if fresh(a, b))
            if close:
                _, a, b = rng.choice(close[:10])
                pair, reason = (a, b), "close_scores"
        if pair is None:
            by_theme: dict = {}
            for i, themes in ideas:
                for t in themes:
                    by_theme.setdefault(t, []).append(i)
            options = [(a, b) for members in by_theme.values() for a in members for b in members if a < b and fresh(a, b)]
            if options:
                pair, reason = rng.choice(options), "same_theme"
    if pair is None:
        return None
    first, second = rng.sample(pair, 2)
    return {"first": first, "second": second, "reason": reason}


def snapshot(conn) -> dict:
    """Everything the app shows, in one piece. The device replaces its copy with this on every sync, so what Idea
    Machine forgets is gone from the phone too."""
    return {"graph": graph(conn), "ideas": details(conn), "review": review(conn, limit=500), "status": status(conn),
            "pairs": compare_pairs(conn), "compared": compared(conn), "digests": digests(conn)}


def digests(conn, week: str | None = None) -> list[dict]:
    """Idea Machine's weekly digests (pub.digests, slice 6), newest first; or the one for the week holding `week`
    (an ISO date). Each point: kind, title, body, idea_ids, and mine ('star', 'dismiss' or None). A digest can vanish
    between syncs: forgetting any speech behind it deletes it whole."""
    try:
        rows = conn.execute(
            """SELECT digest_id, week_start, period_start, period_end, summary, written_at, points FROM pub.digests
               WHERE %(week)s::date IS NULL OR week_start = date_trunc('week', %(week)s::date)::date
               ORDER BY week_start DESC""", {"week": week}).fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        return []
    return [{"id": i, "week_start": w.isoformat(), "period_start": a.isoformat(), "period_end": b.isoformat(),
             "summary": s, "written_at": at.isoformat(), "points": list(p or [])}
            for i, w, a, b, s, at, p in rows]


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
    return {"total": total, "items": items, "audit": audit(items)}


AUDIT = 5


def audit(items: list[dict], n: int = AUDIT) -> list[str]:
    """The captures I judge myself before keeping the rest in one go: one from each band of Idea Machine's
    confidence, lowest first. Within a band the pick is by a hash of the id, so it looks random but every device
    (and the MCP server) picks the same ones, and an item stays picked while the queue around it changes."""
    if len(items) <= n:
        return [i["id"] for i in sorted(items, key=lambda i: i["confidence"])]
    ranked = sorted(items, key=lambda i: (i["confidence"], i["id"]))
    bands = [ranked[len(ranked) * k // n:len(ranked) * (k + 1) // n] for k in range(n)]
    return [min(b, key=lambda i: hashlib.sha256(i["id"].encode()).digest())["id"] for b in bands]


# What Lattice may say, and about what (ROADMAP §3.5). Everything else is refused before it reaches the database.
EVENTS = {"item_keep": "item_id", "item_discard": "item_id", "item_star": "item_id",
          "star": "idea_id", "unstar": "idea_id", "idea_correct": "idea_id", "idea_note": "idea_id",
          "pin_theme": "theme_id", "unpin_theme": "theme_id", "reject_theme": "theme_id",
          "link": "idea_id", "unlink": "idea_id", "idea_compare": "idea_id",
          "digest_star": None, "digest_dismiss": None}   # a digest point: payload digest_id and point
WINNERS = ("first", "second", "tie")


class NotYet(Exception):
    """Idea Machine's database doesn't take this kind of event yet (its migration isn't deployed): try again later."""


def record(conn, event: dict, source: str = "lattice") -> int:
    """Append one feedback event. Raises ValueError for anything not in EVENTS. `source`: the app, or 'mcp'."""
    kind = event.get("kind")
    if kind not in EVENTS:
        raise ValueError(f"unknown kind {kind!r}")
    target = EVENTS[kind]
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
    try:
        return conn.execute(
            f"""INSERT INTO pub.feedback_events (kind, {target}, other_idea_id, payload, source)
                VALUES (%s, %s, %s, %s, %s) RETURNING event_id""",
            (kind, ref, other, json.dumps(payload), source)).fetchone()[0]
    except psycopg.errors.CheckViolation as e:
        # Only an unknown kind is worth retrying; any other check means the event itself is wrong, and always will be.
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
    try:
        return conn.execute("INSERT INTO pub.feedback_events (kind, payload, source) VALUES (%s, %s, %s) RETURNING event_id",
                            (kind, json.dumps(payload), source)).fetchone()[0]
    except psycopg.errors.CheckViolation as e:
        if e.diag.constraint_name == "feedback_events_kind_check":
            raise NotYet(f"Idea Machine doesn't accept {kind} events yet") from None
        raise ValueError(f"Idea Machine refused the event ({e.diag.constraint_name})") from None


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
