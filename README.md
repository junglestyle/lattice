# Lattice

The presentation layer over Idea Machine's idea lattice (`~/data/code/ideamachine`, ROADMAP §3.5 and Phase 2).
Idea Machine owns the truth and publishes it as views in schema `pub`. Lattice reads them as role `lattice_app`.
Its only write, from a later slice, will be appending to `pub.feedback_events`.

What it does (Idea Machine's ROADMAP, Phase 2 slices 2 and 5):

- **Review** one capture at a time, with keep / ★ / discard and one-tap reasons, on the phone.
- **Map**: ideas laid out by meaning (`pub.idea_positions`, a 2D projection Idea Machine keeps stable), colored by
  theme, sized by weight (evidence, and a star counts for two), with connections. Tap an idea for its evidence:
  star it, correct it, link it to another idea, or keep / discard its captures in place. A dashed ring marks ideas
  that are new since your last visit (the app was away for over half an hour).
- **List** and search.
- **Local-first.** The page carries no data. It renders a copy of `pub` kept in IndexedDB (`/api/snapshot`), and a
  service worker keeps the page itself, so the app opens and navigates offline. Feedback goes into a queue,
  applied to the copy at once and sent when the server can be reached; the copy is rebuilt from the server's
  snapshot plus whatever is still queued. Every sync replaces the copy whole, so what Idea Machine forgets is gone
  from the phone too. Logging out (in the run-status panel) clears the copy and the queue.

- **Talk to it (MCP).** `/mcp` is an MCP server (Streamable HTTP, JSON responses) over `pub`: `search_ideas`,
  `get_idea`, `list_themes`, `open_theme`, `whats_new`, and, when you ask, `star_idea`, `link_ideas`, `note_idea`.
  It takes the Lattice token as a bearer header and is tailnet-only like the rest, so a Claude app that connects
  from Anthropic's cloud (claude.ai, the phone app) can't reach it. Speakers are pseudonymized per answer ("me",
  "unknown", S1, S2...), as in Idea Machine's privacy policy B. From Claude Code on a tailnet machine:

  ```sh
  claude mcp add --transport http lattice https://<host>.ts.net:8444/mcp --header "Authorization: Bearer $LATTICE_TOKEN"
  ```

Every change is a feedback event, Lattice's only write (with `source` 'mcp' for changes made in conversation). Service workers need HTTPS or localhost, which `tailscale
serve` provides.

```sh
set -a; . ~/data/code/ideamachine/.env; set +a      # LATTICE_DATABASE_URL
uv run lattice serve                                # http://127.0.0.1:8790/
uv run pytest                                       # smoke tests against the real database (read-only)
```

Don't bind it to the LAN or the internet: evidence quotes include other people's words.

## Always on

`systemd/install.sh` runs `lattice serve` as a user service on localhost:8790. It reads `.env`, which holds only
`LATTICE_DATABASE_URL`, and the service restarts after code changes only when you restart it. The script then puts
it on the tailnet with `tailscale serve`, at https://eeyore.example.ts.net/ with Tailscale's certificate.
That step needs root once: `sudo tailscale serve --bg http://127.0.0.1:8790`, or `sudo tailscale set
--operator=$USER` to allow serve changes without root. Never `tailscale funnel`, which would publish it to the
internet.
