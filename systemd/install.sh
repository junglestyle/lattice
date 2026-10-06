#!/bin/bash
# Link and enable Lattice's user unit, and expose it on the tailnet over HTTPS. Idempotent.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.config/systemd/user
ln -sf "$here/lattice.service" ~/.config/systemd/user/lattice.service
systemctl --user daemon-reload
systemctl --user enable --now lattice.service
# Tailnet only, never the LAN or the internet (that would be `tailscale funnel`): evidence quotes include other
# people's words.
tailscale serve --bg http://127.0.0.1:8790
tailscale serve status
