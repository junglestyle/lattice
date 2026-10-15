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
3. **Read-only or read and write.** The sign-in page has a checkbox for whether Claude may also record review verdicts,
   star, link and set notes. Without it the write tools don't exist for that connector.
4. **Anthropic's address range, checked twice.** Claude's servers make every request except the sign-in page, and
   they come from `160.79.104.0/21` ([Anthropic: IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)).
   A Cloudflare WAF rule blocks anything else, and Lattice checks again from `CF-Connecting-IP`
   (`LATTICE_MCP_ALLOW`), so a missing WAF rule doesn't open it up. That range is every Claude user's, so it only
   limits who can knock: OAuth decides who gets in.
5. **The sign-in page is closed except while you connect.** Anthropic's servers never load it: your own browser
   does, so it can't be limited to Anthropic's range. Instead a second WAF rule blocks it outright, and you switch
   that rule off for the few minutes it takes to add a connector (see [Add or re-add a connector](#add-or-re-add-a-connector)).
   Connectors you've already added keep working with it closed, since refreshing a token goes to `/token`, from
   Anthropic's range. While it's open, it's guarded by the Lattice token, refuses all sign-ins for the rest of the
   hour after 10 wrong tokens, and can't be framed.
6. **What goes out** is what the tailnet MCP endpoint gives: ideas, themes and quotes, with speakers pseudonymized
   ("me", "unknown", S1, S2...) as in Idea Machine's policy B.

What's still different from the tailnet:
- Cloudflare terminates TLS, so it sees this traffic in plain text, tokens included.
- While the sign-in page is open, the Lattice token unlocks something reachable from the internet. Use a long
  random one, as `install/nas.sh` makes, and switch the page closed again when you're done.
- Grants are signed with a key derived from the Lattice token, and nothing is stored. To cut off one connector,
  remove it in Claude. To cut off all of them, change the Lattice token: that also logs out every device using
  the app.

Claude Code doesn't use this. It connects to the tailnet endpoint with the token (see the README).

## Set it up (on the NAS)

1. **The public URL.** Pick a hostname on a domain you have on Cloudflare, and add it to Lattice's env file
   (Idea Machine's data and config live in `/mnt/fast/ideamachine`):

   ```sh
   echo "LATTICE_PUBLIC_URL=https://lattice-mcp.<your domain>" >> /mnt/fast/ideamachine/config/lattice.env
   ```

2. **The tunnel's token.** Idea Machine's `install/compose.yaml` already has the tunnel (`cloudflared`, profile
   `public`) and publishes port 8791 on loopback. `install/nas.sh` starts the tunnel whenever `lattice.env` has
   `LATTICE_PUBLIC_URL`, checks that the public listener answers 401, and removes the tunnel when the URL is gone.
   It only needs the token from step 3, in `config/cloudflared.env` (it asks for it if it's missing):

   ```sh
   echo "TUNNEL_TOKEN=<the tunnel's token>" > /mnt/fast/ideamachine/config/cloudflared.env
   chmod 600 /mnt/fast/ideamachine/config/cloudflared.env
   ```

3. **The tunnel** (Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared).
   Put its token in `config/cloudflared.env` as `TUNNEL_TOKEN=...`. Add one public hostname, the one in
   `LATTICE_PUBLIC_URL`, with service `HTTP` → `127.0.0.1:8791`. Nothing else goes on this tunnel.

4. **Two WAF rules** (your domain → Security → WAF → Custom rules), both with action **Block**.

   *Lattice: only Anthropic*, always on. Anthropic's servers may reach the MCP and OAuth endpoints, and nothing
   else gets through, except the sign-in page, which the next rule handles:

   ```
   http.host eq "lattice-mcp.<your domain>"
   and not (http.request.uri.path eq "/authorize")
   and not (ip.src in {160.79.104.0/21} and (http.request.uri.path in {"/mcp" "/demo/mcp" "/token" "/register"}
            or starts_with(http.request.uri.path, "/.well-known/oauth-")))
   ```

   *Lattice: sign-in closed*, on except while you connect:

   ```
   http.host eq "lattice-mcp.<your domain>" and http.request.uri.path eq "/authorize"
   ```

   If Bot Fight Mode or a managed challenge is on for the domain, skip it for this hostname: Claude's servers
   can't solve a challenge.

5. **Deploy, then connect** as below. As admin, `git pull` in both clones (`/mnt/fast/repos/ideamachine` and
   `/mnt/fast/repos/lattice`), then run `sudo /mnt/fast/repos/ideamachine/install/nas.sh`. The tunnel should show
   **Healthy** in Cloudflare. Logs go to journald: `journalctl -t lattice` and `journalctl -t lattice-tunnel`.
   To check the listener from the NAS, say the request came from Anthropic's range, as Cloudflare would:

   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'CF-Connecting-IP: 160.79.104.10' http://127.0.0.1:8791/mcp   # 401
   ```

   Without that header it answers an empty 404, as it does to anything not from Anthropic's range.

## Add or re-add a connector

You need this the first time, after removing a connector, after changing the Lattice token, and if Claude ever
says the connector needs to be reconnected (its refresh token lapsed after 30 days unused, or was revoked).

1. In Cloudflare (your domain → Security → WAF → Custom rules), switch **Lattice: sign-in closed** off.
2. In claude.ai → Settings → Connectors → Add custom connector, enter `https://lattice-mcp.<your domain>/mcp` and
   leave the OAuth fields empty. Claude opens Lattice's sign-in page: enter the Lattice token, decide about changes,
   and Allow. The connector shows up in the phone app too.
3. Switch **Lattice: sign-in closed** back on. Ask Claude something from your lattice to check the connector still
   works with the page closed.

If you forget step 3, the page stays guarded by the token and the wrong-token cap, so it's not open to everyone,
but it is exposed to the internet until you switch the rule back on.

## The demo connector

For showing Lattice to someone through a Claude app (an interview, a demo), add a second connector at
`https://lattice-mcp.<your domain>/demo/mcp`, the same way as above. Name it something like "Lattice (demo)". It is
read-only, offers no review tools, and holds back what demo mode in the app does: ideas and themes Idea Machine
tagged sensitive (`pub.sensitivity`), digest points citing them and digest summaries; swearing is blacked out. Your
own Hide it / Show it marks decide. Its sign-in page says "demo connector", and its tokens only work on `/demo/mcp`.

Which tagged categories it hides is the server's setting, since the app's choices live on your phone. The default is
`sex,gender_politics,political_extremism,violence,racism`; to change it, add a line to `lattice.env` and re-run
`sudo /mnt/fast/repos/ideamachine/install/nas.sh`:

```sh
echo "LATTICE_DEMO_HIDE=sex,gender_politics,political_extremism,violence,racism,profanity" >> /mnt/fast/ideamachine/config/lattice.env
```

Before a demo: in a claude.ai chat, turn the demo connector on and the full one off (the connectors menu in the chat),
and ask it something to check. Your existing WAF rule needs `/demo/mcp` in its list of paths (as in step 4 above),
or Cloudflare blocks the demo connector.

## Turn it off

Remove `LATTICE_PUBLIC_URL` from `lattice.env` and re-run `sudo /mnt/fast/repos/ideamachine/install/nas.sh`: Lattice no longer opens the listener,
and the tunnel container is removed. Then delete the tunnel in Cloudflare and the connector in claude.ai.
