# Lattice's MCP endpoint for claude.ai and the phone app

claude.ai and the Claude phone app (and so voice) connect to MCP servers from Anthropic's cloud, not from your
device, so they can't reach the tailnet. This publishes **only the MCP endpoint** through a Cloudflare Tunnel. The
app, the snapshot and the review queue stay tailnet-only, because the public listener doesn't serve them at all.

## What protects it

1. **A separate listener.** With `LATTICE_MCP_SECRET` set, `lattice serve` opens a second port (8791) that answers
   exactly one path, `POST /mcp/<secret>`, and a bare 404 to everything else. The tunnel points only at this port.
2. **A path secret.** claude.ai's custom connectors can't send a bearer header, so the secret goes in the URL.
3. **Anthropic's address range, checked twice.** A Cloudflare WAF rule blocks every source outside Anthropic's
   published outbound range, `160.79.104.0/21`
   ([Anthropic: IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)). Lattice checks it again from
   `CF-Connecting-IP` (`LATTICE_MCP_ALLOW`), so a missing WAF rule doesn't open it up, and a request that skipped
   the tunnel has no such header and is refused. That range is every Claude user's: it limits who can knock, and
   the secret decides who gets in.
4. **What goes out** is what the tailnet MCP endpoint gives: ideas, themes, quotes with speakers pseudonymized
   ("me", "unknown", S1, S2...), as in Idea Machine's policy B. Writes are only star, link and note.

Read this before turning it on. It reverses the rule in Idea Machine's `install/compose.yaml` ("never the LAN,
never 0.0.0.0, never tailscale funnel") for this one endpoint:
- **The secret in the path is the only real authentication.** `160.79.104.0/21` is all of Anthropic's outbound
  traffic, every Claude user's connectors included, not just yours. Anyone who has the URL can use it from their own
  Claude.
- **That includes the write tools.** Whoever has the URL can read every idea and quote (speakers pseudonymized),
  and can star, link and set notes. Those writes are feedback events marked `source = 'mcp'`, so they can be found
  and deleted, but they'd steer Idea Machine until then.
- **A path leaks more easily than a header.** The URL sits in Cloudflare's request logs, cloudflared's logs, any
  proxy in between, connector settings and screenshots. Lattice itself never logs requests.
- Cloudflare terminates TLS, so it sees this traffic in plain text.

OAuth would replace the path secret with a login that never sits in a URL, and it's the way to make this solid.

## Set it up (on the NAS)

1. **The secret.** Add it to Lattice's env file and keep the value for step 5:

   ```sh
   echo "LATTICE_MCP_SECRET=$(openssl rand -hex 24)" >> /mnt/storage/ideamachine/config/lattice.env
   ```

2. **Compose** (Idea Machine's `install/compose.yaml`): publish the second port on loopback and add the tunnel.

   ```yaml
     lattice:
       ports:
         - "127.0.0.1:8790:8790"
         - "127.0.0.1:8791:8791"   # the public MCP listener; only cloudflared talks to it

     # Publishes only Lattice's MCP listener (docs/public-mcp.md in Lattice). Outbound-only: no ports opened.
     cloudflared:
       image: cloudflare/cloudflared:<a current release>
       container_name: lattice-tunnel
       command: tunnel --no-autoupdate run
       env_file: /mnt/storage/ideamachine/config/cloudflared.env   # TUNNEL_TOKEN=..., root only
       network_mode: host          # to reach 127.0.0.1:8791
       restart: unless-stopped
   ```

3. **The tunnel** (Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared).
   Put its token in `config/cloudflared.env` as `TUNNEL_TOKEN=...`. Add one public hostname, e.g.
   `lattice-mcp.<your domain>`, with service `HTTP` → `127.0.0.1:8791`. Nothing else goes on this tunnel.

4. **The WAF rule** (your domain → Security → WAF → Custom rules), action **Block**:

   ```
   (http.host eq "lattice-mcp.<your domain>" and not ip.src in {160.79.104.0/21})
   or (http.host eq "lattice-mcp.<your domain>" and not starts_with(http.request.uri.path, "/mcp/"))
   ```

   If Bot Fight Mode or a managed challenge is on for the domain, skip it for this hostname: Claude's requests
   can't solve a challenge.

5. **Restart and connect.** Re-run `install/nas.sh` (or `docker compose up -d lattice cloudflared`). Then in
   claude.ai → Settings → Connectors → Add custom connector, the URL is
   `https://lattice-mcp.<your domain>/mcp/<the secret>`, with no OAuth. The connector shows up in the phone app too.

## Rotate the secret

Replace `LATTICE_MCP_SECRET` in `lattice.env`, restart the `lattice` container, and edit the connector's URL. Do it
whenever the URL may have been seen (a screenshot, a shared chat), and to be rid of old copies in Cloudflare's logs.

## Turn it off

Remove `LATTICE_MCP_SECRET` and restart Lattice: the listener isn't opened at all. Then delete the tunnel.
