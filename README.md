# Lattice

The presentation layer over Idea Machine's idea lattice (`~/data/code/ideamachine`, ROADMAP §3.5 and Phase 2).
Idea Machine owns the truth and publishes it as views in schema `pub`. Lattice reads them as role `lattice_app`.
Its only write, from a later slice, will be appending to `pub.feedback_events`.

So far: slice 2, a thin read-only map. It shows ideas colored by theme (outlined if they came from the archive,
filled if captured from conversation, sized by evidence), connections (evolves, archive links, related), a list
view, search, and each idea's evidence on click.

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
