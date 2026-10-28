# Working on lattice

The presentation layer over Idea Machine's idea lattice.

Read before changing behavior:

- [README](README.md): what lattice does and how to run it
- [Coding standards](docs/standards.md): how the code should read, on top of the org's [principles](https://github.com/junglestyle/.github/blob/main/PRINCIPLES.md) and standards (`docs/org-standards*.md`)
- [Public MCP](docs/public-mcp.md): the one endpoint reachable from outside the tailnet

Docs change with behavior. Never commit hostnames, tokens, or deployment config (see
[Privacy](docs/standards.md#privacy)).

Run `make check` before committing. It runs everything CI runs, except the org standards check: `make standards` (needs the network).
