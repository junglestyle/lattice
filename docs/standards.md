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

## Zen

- One way to change anything: every change, from the page or from a conversation over MCP, is a feedback event
  appended by `data.record`.

## Wu wei

- The device's copy converges: every sync replaces it whole with the server's snapshot plus whatever is still queued.

## Pragmatic Programmer

- Orthogonality: Lattice talks to Idea Machine only through schema `pub`, as role `lattice_app`. It reads the views and
  appends to `pub.feedback_events`.

## Testing

- Tests run against a real Idea Machine database (`LATTICE_DATABASE_URL`), not mocks, and skip without one. A test
  that writes needs `IM_DEV_ADMIN_URL` too, to delete the events it appended.
- The behaviors worth a test: spot checks and bulk keeps, feedback checked then appended, comparisons, the device's
  copy and logging out, the MCP tools and their pseudonymized speakers, the public listener's OAuth and Anthropic's
  range, demo mode, and that `lattice_app` can't read Idea Machine's tables.

## Running it

- `make check` runs everything CI runs. CI has no Idea Machine database, so there the database tests skip.
- `make check` also diffs [org-standards.md](org-standards.md) against the org's current file, so a hand edit or a
  stale copy fails. That step needs the network. `make sync-standards` refreshes it.
- Lattice has no installer for the NAS: Idea Machine's installer builds the `Dockerfile`'s image.
  `systemd/install.sh` runs it on a workstation. The [README](../README.md) has the steps for both.

## Privacy

- Overrides the org rule's `.env.example`: there isn't one. The [README](../README.md) and
  [public-mcp.md](public-mcp.md) name each `LATTICE_` variable where it's used.
- Evidence quotes are other people's words. Nothing serves them beyond the tailnet except the public MCP listener,
  and [public-mcp.md](public-mcp.md) says what guards it.
