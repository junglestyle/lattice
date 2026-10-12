# Lattice's MCP endpoint for claude.ai and the phone app

claude.ai and the Claude phone app (and so voice) connect to MCP servers from Anthropic's cloud, not from your
device, so they can't reach the tailnet. This publishes **only the MCP endpoint**, with OAuth in front of it,
through a Cloudflare Tunnel. The app, the snapshot and the review queue stay tailnet-only: the public listener
doesn't serve them at all.

Read this before turning it on. It reverses the rule in Idea Machine's `install/compose.yaml` ("never the LAN,
never 0.0.0.0, never tailscale funnel") for this one endpoint.

## How it's protected

1. **A separate listener.** With `LATTICE_PUBLIC_URL` set, `lattice serve` opens a second port (8791). It serves
   the MCP endpoint, the OAuth endpoints and nothing else, and answers a bare 404 to everything else.
2. **OAuth 2.1, with Lattice as its own authorization server** (`src/lattice/oauth.py`). Claude registers itself,
   opens a sign-in page in your browser where you enter the **Lattice token**, and gets an access token (1 hour)
   and a refresh token (30 days, rotated on every use; a reused one revokes its family). PKCE (S256) is required.
   Codes go only to Claude's hosted callback (`https://claude.ai/api/mcp/auth_callback`, and `claude.com`).
3. **Read-only or read and write.** The sign-in page has a checkbox for whether Claude may also star, link and set
   notes. Without it the write tools don't exist for that connector.
4. **Anthropic's address range, checked twice.** Claude's servers make every request except the sign-in page, and
   they come from `160.79.104.0/21` ([Anthropic: IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)).
   A Cloudflare WAF rule blocks anything else, and Lattice checks again from `CF-Connecting-IP`
   (`LATTICE_MCP_ALLOW`), so a missing WAF rule doesn't open it up. That range is every Claude user's, so it only
   limits who can knock: OAuth decides who gets in.
5. **The sign-in page is open to any address**, because your own browser opens it. It's guarded by the Lattice
   token and refuses all sign-ins for the rest of the hour after 10 wrong tokens. It can't be framed.
6. **What goes out** is what the tailnet MCP endpoint gives: ideas, themes and quotes, with speakers pseudonymized
   ("me", "unknown", S1, S2...) as in Idea Machine's policy B.

What's still different from the tailnet:
- Cloudflare terminates TLS, so it sees this traffic in plain text, tokens included.
- The Lattice token now unlocks something reachable from the internet (the sign-in page). Use a long random one,
  as `install/nas.sh` makes.
- Grants are signed with a key derived from the Lattice token, and nothing is stored. To cut off one connector,
  remove it in Claude. To cut off all of them, change the Lattice token: that also logs out every device using
  the app.

Claude Code doesn't use this. It connects to the tailnet endpoint with the token (see the README).

## Set it up (on the NAS)

1. **The public URL.** Pick a hostname on a domain you have on Cloudflare, and add it to Lattice's env file:

   ```sh
   echo "LATTICE_PUBLIC_URL=https://lattice-mcp.<your domain>" >> /mnt/storage/ideamachine/config/lattice.env
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
   Put its token in `config/cloudflared.env` as `TUNNEL_TOKEN=...`. Add one public hostname, the one in
   `LATTICE_PUBLIC_URL`, with service `HTTP` → `127.0.0.1:8791`. Nothing else goes on this tunnel.

4. **The WAF rule** (your domain → Security → WAF → Custom rules), action **Block**. It lets your browser reach the
   sign-in page, and Anthropic reach the rest:

   ```
   http.host eq "lattice-mcp.<your domain>"
   and not (http.request.uri.path eq "/authorize")
   and not (ip.src in {160.79.104.0/21} and (http.request.uri.path in {"/mcp" "/token" "/register"}
            or starts_with(http.request.uri.path, "/.well-known/oauth-")))
   ```

   If Bot Fight Mode or a managed challenge is on for the domain, skip it for this hostname: Claude's servers
   can't solve a challenge.

5. **Restart and connect.** Re-run `install/nas.sh` (or `docker compose up -d lattice cloudflared`). Then in
   claude.ai → Settings → Connectors → Add custom connector, enter `https://lattice-mcp.<your domain>/mcp` and
   leave the OAuth fields empty. Claude opens Lattice's sign-in page: enter the Lattice token, decide about changes,
   and Allow. The connector shows up in the phone app too.

## Turn it off

Remove `LATTICE_PUBLIC_URL` and restart Lattice: the listener isn't opened at all. Then delete the tunnel and the
connector.
