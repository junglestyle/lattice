"""The lattice as Markdown files I own: a zip that opens as an Obsidian vault, or reads anywhere.

ideas/<title>.md      one per idea: properties (id, themes, origin, starred, score, dates), the statement, my note
                      and context, its connections as [[links]], and its quotes with who said them and when
themes/<name>.md      one per theme: its description and its ideas
digests/<week>.md     one per weekly digest, its points linking their ideas
Lattice.md            an index

It's my own copy, from the app on the tailnet, so speakers keep their names (unlike MCP's answers).
"""

import io
import re
import zipfile
from datetime import datetime, timezone

from lattice import data

KIND = {"evolves": "evolves", "related": "related", "archive": "archive link", "mine": "my link"}
DIGEST_KIND = {"recurring": "Keeps coming up", "convergence": "Converging", "sleeper": "Sleeper",
               "project": "Project forming", "outside_value": "Worth sharing"}


def _file_names(titles: dict) -> dict:
    """id -> a file name from the title: no characters a file system or a [[link]] chokes on, unique."""
    out, taken = {}, set()
    for i, title in sorted(titles.items(), key=lambda x: (x[1] or "", x[0])):
        base = re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", title or "Untitled").strip(" .")[:80] or "Untitled"
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base} ({n})", n + 1
        taken.add(name.lower())
        out[i] = name
    return out


def _yaml(value) -> str:
    if isinstance(value, bool) or value is None:
        return {True: "true", False: "false", None: "null"}[value]
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _front(props: dict) -> str:
    return "---\n" + "".join(f"{k}: {_yaml(v)}\n" for k, v in props.items() if v not in (None, [], "")) + "---\n\n"


def build(conn) -> bytes:
    g, ideas, digests = data.graph(conn), data.details(conn), data.digests(conn)
    names = _file_names({i: d["title"] for i, d in ideas.items()})
    theme_files = _file_names({t["id"]: t["name"] for t in g["themes"]})
    theme_of_name = {t["name"]: t["id"] for t in g["themes"]}
    node = {n["id"]: n for n in g["nodes"]}
    link = lambda i: f"[[{names[i]}]]" if i in names else "(an idea no longer in the lattice)"   # noqa: E731
    theme_link = lambda name: f"[[{theme_files[theme_of_name[name]]}]]" if name in theme_of_name else name   # noqa: E731
    conns: dict = {}
    for edge in g["links"]:
        for a, b in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            conns.setdefault(a, []).append((b, KIND.get(edge["kind"], edge["kind"])))

    files = {}
    for i, d in ideas.items():
        n = node.get(i, {})
        body = [_front({"id": i, "themes": d["themes"], "origin": d["origin"], "starred": d["starred"],
                        "score": round(n["score"], 2) if n.get("score") is not None else None,
                        "first_said": (n.get("first_said") or "")[:10] or None,
                        "last_said": (n.get("last_said") or "")[:10] or None, "archive_ref": d.get("ref")}),
                f"# {d['title']}\n\n{d['statement']}\n"]
        if d["corrected"]:
            body.append(f"\n> Claude first heard: *{d['claude_title']}*: {d['claude_statement']}\n")
        if d.get("my_note"):
            body.append(f"\n**My note:** {d['my_note']}\n")
        if d.get("notes"):
            body.append(f"\n**Context:** {d['notes']}\n")
        if d["themes"]:
            body.append("\nThemes: " + ", ".join(theme_link(t) for t in d["themes"]) + "\n")
        if conns.get(i):
            body.append("\n## Connected\n\n" + "".join(f"- {link(o)} ({k})\n" for o, k in sorted(
                conns[i], key=lambda x: names.get(x[0], ""))))
        quotes = [e for e in d["evidence"] if e.get("verdict") != "discard"]
        if quotes:
            body.append("\n## Said\n\n" + "".join(
                f"> {e['quote']}\n>\n> {e['said_by']}, {e['said_at'][:10]}\n\n" for e in quotes))
        files[f"ideas/{names[i]}.md"] = "".join(body)

    for t in g["themes"]:
        members = sorted((i for i, d in ideas.items() if t["name"] in d["themes"]), key=lambda i: names[i])
        files[f"themes/{theme_files[t['id']]}.md"] = (
            _front({"id": t["id"], "pinned": t["pinned"], "proposed": t["proposed"]}) + f"# {t['name']}\n\n"
            + (f"{t['description']}\n\n" if t.get("description") else "")
            + "".join(f"- {link(i)}{' ★' if ideas[i]['starred'] else ''}\n" for i in members))

    for d in digests:
        files[f"digests/{d['week_start']}.md"] = (
            _front({"week_start": d["week_start"], "written_at": d["written_at"][:10]})
            + f"# Week of {d['week_start']}\n\n{d['summary']}\n\n"
            + "".join(f"## {p['title']}\n\n*{DIGEST_KIND.get(p['kind'], p['kind'])}*"
                      + {"star": " · ★ worth it", "dismiss": " · not useful"}.get(p.get("mine"), "")
                      + f"\n\n{p['body']}\n\n" + "".join(f"- {link(i)}\n" for i in p["idea_ids"]) + "\n"
                      for p in d["points"]))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    files["Lattice.md"] = (
        f"# Lattice\n\nExported {now}: {len(ideas)} ideas, {len(g['themes'])} themes, {len(digests)} digests.\n\n"
        "## Themes\n\n" + "".join(f"- [[{theme_files[t['id']]}]] ({t['ideas']})\n" for t in g["themes"])
        + "\n## Starred\n\n" + "".join(f"- {link(i)}\n" for i in sorted(
            (i for i, d in ideas.items() if d["starred"]), key=lambda i: names[i]))
        + ("\n## Digests\n\n" + "".join(f"- [[{d['week_start']}]]\n" for d in digests) if digests else ""))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in sorted(files.items()):
            z.writestr(f"Lattice/{path}", text)
    return buf.getvalue()
