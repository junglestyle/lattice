# Coding standards

Lattice follows the [org standards](org-standards.md), an unchanged copy of
[junglestyle's](https://github.com/junglestyle/.github/blob/main/STANDARDS.md). This file adds lattice's own rules and
examples. Where it differs from an org rule, it says so, and it wins.

## Jungle Style

- What's already in the stack: the stdlib's `http.server` and `urllib`, psycopg on Idea Machine's Postgres, fastembed
  for search by meaning, and one HTML page with d3 and no build step.

## DRY

- The server picks the spot checks (`data.audit`), and the page and the MCP server use its pick, so every device
  agrees.
- The accepted duplicates: the offline page works on its own copy, so it repeats four things in JavaScript. Bridges
  (`data.bridges`), "mattered" for Look again (`data.why_it_matters`), the swearing list (`mcp.SWEARING`), and
  applying events to the copy (`applyEvent`, as Idea Machine applies them). Each pair is kept alike by hand.

## Zen

- One way to change anything: every change, from the page or from a conversation over MCP, is a feedback event
  appended by `data.record`.
- A fallback for a `pub` view or event kind Idea Machine hasn't published yet names the migration it waits for, and
  goes once that migration ships. Never catch `InsufficientPrivilege`: it's a grants bug.
- `/api/feedback`'s status codes are the device queue's contract. 400 means the page drops the event for good, and
  503 means it keeps it and retries, so 400 is only for an event that is itself invalid.

## Wu wei

- The device's copy converges: every sync replaces it whole with the server's snapshot plus whatever is still queued.

## Pragmatic Programmer

- Orthogonality: Lattice talks to Idea Machine only through schema `pub`, as role `lattice_app`. It reads the views and
  appends to `pub.feedback_events`.
- Overrides the org rule's `docs/stack.md`: there isn't one. Lattice's spec and decisions live in Idea Machine's
  ROADMAP §3.5.

## Testing

- Tests run against a real Idea Machine database (`LATTICE_DATABASE_URL`), not mocks, and skip without one. A test
  that writes needs `IM_DEV_ADMIN_URL` too, to delete the events it appended.
- Only a test that uses the database skips without one. Security-boundary tests that don't (logging in, the cookie,
  the token, the MCP endpoint's bearer and origin checks) run in CI.
- The behaviors worth a test: spot checks and bulk keeps, feedback checked then appended, comparisons, the device's
  copy and logging out, the MCP tools and their pseudonymized speakers, the public listener's OAuth and Anthropic's
  range, demo mode, and that `lattice_app` can't read Idea Machine's tables.

## Running it

- `make check` runs everything CI runs except the org standards diff. CI has no Idea Machine database, so there the database tests skip.
- `make standards` diffs [org-standards.md](org-standards.md) against the org's current file, so a hand edit or a
  stale copy fails. It needs the network, so it isn't part of `make check`; CI runs it as its own job.
  `make sync-standards` refreshes the copy.
- Lattice has no installer for the NAS: Idea Machine's installer builds the `Dockerfile`'s image.
  `systemd/install.sh` runs it on a workstation. The [README](../README.md) has the steps for both.

## Privacy

- Overrides the org rule's `.env.example`: there isn't one. The [README](../README.md) and
  [public-mcp.md](public-mcp.md) name each `LATTICE_` variable where it's used.
- Overrides the org rule for Idea Machine's install contract: the NAS paths its installer defines (`/mnt/fast/...`)
  and the `Dockerfile`'s UID may be committed. Personal layout (`~/data/code`) may not.
- Anything that hides content (demo mode, `/demo/mcp`) fails closed: without its source (`pub.sensitivity`) it
  errors rather than show everything.
- Evidence quotes are other people's words. Nothing serves them beyond the tailnet except the public MCP listener,
  and [public-mcp.md](public-mcp.md) says what guards it.
