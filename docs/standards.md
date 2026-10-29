# Coding standards

Lattice follows the [org standards](org-standards.md) and the org's standards for [Python](org-standards-python.md),
[SQL](org-standards-sql.md), [containers](org-standards-containers.md) and [shell scripts](org-standards-shell.md),
unchanged copies of [junglestyle's](https://github.com/junglestyle/.github/tree/main/code-standards). `.org-standards`
lists the topics it copies beyond the general one. This file adds lattice's own rules and examples. Where it differs
from an org rule, it says so, and it wins.

## Dependencies

- What's already in the stack: the stdlib's `http.server` and `urllib`, psycopg on Idea Machine's Postgres, fastembed
  for search by meaning, and one HTML page with d3 and no build step.

## Structure

- The server picks the spot checks (`data.audit`), and the page and the MCP server use its pick, so every device
  agrees.
- The accepted duplicates: the offline page works on its own copy, so it repeats four things in JavaScript. Bridges
  (`data.bridges`), "mattered" for Look again (`data.why_it_matters`), the swearing list (`mcp.SWEARING`), and
  applying events to the copy (`applyEvent`, as Idea Machine applies them). Each pair is kept alike by hand.
- One way to change anything: every change, from the page or from a conversation over MCP, is a feedback event
  appended by `data.record`.
- Lattice talks to Idea Machine only through schema `pub`, as role `lattice_app`. It reads the views and appends to
  `pub.feedback_events`.

## Errors and recovery

- A fallback for a `pub` view or event kind Idea Machine hasn't published yet names the migration it waits for, and
  goes once that migration ships. Never catch `InsufficientPrivilege`: it's a grants bug.
- `/api/feedback`'s status codes are the device queue's contract. 400 means the page drops the event for good, and
  503 means it keeps it and retries, so 400 is only for an event that is itself invalid.
- The device's copy converges: every sync replaces it whole with the server's snapshot plus whatever is still queued.

## Testing

- Tests run against a real Idea Machine database (`LATTICE_DATABASE_URL`), not mocks, and skip without one. A test
  that writes needs `IM_DEV_ADMIN_URL` too, to delete the events it appended.
- Only a test that uses the database skips without one. Security-boundary tests that don't (logging in, the cookie,
  the token, the MCP endpoint's bearer and origin checks) run in CI.
- The behaviors worth a test: spot checks and bulk keeps, feedback checked then appended, comparisons, the device's
  copy and logging out, the MCP tools and their pseudonymized speakers, the public listener's OAuth and Anthropic's
  range, demo mode, and that `lattice_app` can't read Idea Machine's tables.

## Running it

- CI has no Idea Machine database, so there the database tests skip.
- Lattice has no installer for the NAS: Idea Machine's installer builds the `Dockerfile`'s image.
  `systemd/install.sh` runs it on a workstation. The [README](../README.md) has the steps for both.

## Privacy

- There's no `.env.example`: the [README](../README.md) and [public-mcp.md](public-mcp.md) name each `LATTICE_` variable
  where it's used.
- Overrides the org rule for Idea Machine's install contract: the NAS paths its installer defines (`/mnt/fast/...`)
  and the `Dockerfile`'s UID may be committed. Personal layout (`~/data/code`) may not.
- Anything that hides content (demo mode, `/demo/mcp`) fails closed: without its source (`pub.sensitivity`) it
  errors rather than show everything.
- Evidence quotes are other people's words. Nothing serves them beyond the tailnet except the public MCP listener,
  and [public-mcp.md](public-mcp.md) says what guards it.

## Docs

- Overrides the org rule's `docs/stack.md`: there isn't one. Lattice's spec and decisions live in Idea Machine's
  ROADMAP §3.5.

## Known deviations

Where Lattice doesn't follow the org standards yet. These are debt to pay down, not overrides: each one should go.

- Dependencies: the page's JavaScript and d3 have no decision in Idea Machine's ROADMAP §3.5, where Lattice's decisions
  live.
- Versions and pins: the NAS runs whatever `git pull` brings (README, "On the NAS"). Nothing pins the commit it
  deploys.
- Versions and pins: `src/lattice/meaning.py` imports numpy, which `pyproject.toml` doesn't declare (fastembed brings
  it). The build backend, `hatchling`, has no lower bound and isn't in `uv.lock`.
- Versions and pins: `pyproject.toml` has no `[tool.uv] required-version`, so CI's `setup-uv` (`check.yml`) installs
  the newest uv while the `Dockerfile` pins 0.12.2.
- Versions and pins, Containers: the embedding model is downloaded without a revision or a checksum, by the
  `Dockerfile` (line 11) and, outside the image, by `meaning.py` on first use.
- Versions and pins: the page loads d3 7.9.0 from cdnjs (`index.html:12`, `static/sw.js:5`) with no `integrity`
  checksum.
- Versions and pins: the only tag, `v1`, is annotated but isn't `vX.Y.Z`, so it isn't a release.
- Versions and pins: `pyproject.toml` says `0.0.0`, and CI runs only on main and pull requests, so nothing yet fails a
  `vX.Y.Z` tag that doesn't match it.
- Config: `mcp.handle`'s `connect` parameter is set only by `tests/test_audit.py:234`, to its default.
- Privacy: the personal layout this file's Privacy rule forbids is committed: `~/data/code/ideamachine` (README lines
  10 and 95) and `%h/data/code/lattice` (`systemd/lattice.service`).
- Secrets: [public-mcp.md](public-mcp.md) (step 2) has the operator `echo` the tunnel's token into
  `config/cloudflared.env` and `chmod 600` it afterwards, so the file starts out with the umask's mode and the token
  lands in shell history.
- Docs: there's no `docs/SPEC.md` or `docs/ROADMAP.md`. Lattice's spec is a section of Idea Machine's ROADMAP (§3.5),
  and its slices are Idea Machine's Phase 2 slices, with none of its own.
- Deployment repos: the README and [public-mcp.md](public-mcp.md) carry the NAS deployment: its `/mnt/fast/...`
  paths, the tailnet port 8444 and the deploy steps. The Privacy override above predates the Deployment repos rule,
  and Idea Machine's standards list the same facts as belonging in a future `ideamachine-deployment` repo.
- Containers: `python:3.13-slim` doesn't name the Debian release.
- Containers: the `Dockerfile` installs uv with `pip`.
- Python: `make check` runs pytest only. Ruff and mypy aren't configured or run.
- Python: `scripts/make_icons.py` is a command outside `[project.scripts]`, with no `main()`.
- Python: many functions leave parameters unannotated, among them every `conn` in `data.py` and `mcp.py`,
  `mcp.handle`'s `message` and `server.main`'s `argv`.
- Python: records travel as plain `dict`s (`data.graph`, `data.idea`, `data.review`, every MCP tool's result), not
  frozen dataclasses.
- Python: the exceptions callers catch by type aren't named `<Thing>Error`: `data.NotYet`, `meaning.Unavailable`,
  `mcp.UnknownTool` and `oauth.Redirect`.
- Python: the public MCP listener's thread (`server.py:341`) is a daemon with no stop `Event`, never joined.
- Python: the tests write to an existing Idea Machine database, not a scratch one, and clean up through an admin URL
  that isn't checked for localhost.
- Python: `make check` runs `pytest` without `-rs`, and nothing configures it, so the skips don't show.
- Python: `tests/test_server.py` and `tests/test_mcp.py` are named for modules, not behaviors.
- Shell: `systemd/install.sh` starts with `#!/bin/bash`, and `make check` doesn't run shellcheck.
- SQL: `mcp._theme` (`mcp.py:152-154`) takes `LIMIT 1` of the themes whose name matches, and its `ORDER BY` doesn't
  decide between two with the same name. Idea Machine's `im.themes.name` has no unique constraint.
- SQL: `data.WINNERS`, `data.KEEP_AS` and `data.VOCAB_SOURCES` copy the lists in Idea Machine's
  `feedback_events_compare`, `feedback_events_idea_keep` and `feedback_events_vocab` checks, rather than rely on them.
  `data.record` already turns any check but the kind's into a `ValueError` (`data.py:260`).
- SQL: CI has no Postgres, so the database tests skip there.
