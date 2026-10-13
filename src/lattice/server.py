"""`lattice serve`: the Lattice app (ROADMAP slices 2 and 5 in Idea Machine).

Behind a token login (lattice.auth). Binds to localhost by default; `tailscale serve` puts it on the tailnet
over HTTPS. Never bind it to the LAN or the internet, since evidence quotes include other people's words.
"""

import argparse
import datetime
import html
import re
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from lattice import auth, data, export, mcp, meaning, oauth

HERE = resources.files("lattice")
STATIC = {"/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon-192.png": ("icon-192.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
          "/sw.js": ("sw.js", "text/javascript")}


def answer_mcp(h: BaseHTTPRequestHandler, writes: bool = True, demo: bool = False) -> None:
    """One MCP exchange, JSON responses only, once the caller is authenticated. A browser page can't use it: a
    request with a foreign Origin is refused outright."""
    origin = h.headers.get("Origin")
    if origin and urllib.parse.urlsplit(origin).netloc != h.headers.get("Host"):
        h._json({"error": "foreign origin"}, 403)
        return
    length = min(int(h.headers.get("Content-Length") or 0), 65536)
    try:
        message = json.loads(h.rfile.read(length) or b"null")
    except ValueError:
        h._json(mcp._error(None, -32700, "parse error"), 400)
        return
    if isinstance(message, list):   # a batch (older protocol versions)
        answers = [a for a in (mcp.handle(m, writes=writes, demo=demo) for m in message) if a is not None]
        h._json(answers) if answers else h._send(202, b"", "application/json")
        return
    answer = mcp.handle(message, writes=writes, demo=demo)
    h._json(answer) if answer is not None else h._send(202, b"", "application/json")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _redirect(self, to: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _secure(self) -> bool:
        return self.headers.get("X-Forwarded-Proto") == "https" or self.headers.get("Host", "").endswith(".ts.net")

    def _login_page(self, status: int = 200, message: str = "Enter your token.") -> None:
        page = HERE.joinpath("login.html").read_text().replace("/*MESSAGE*/", message)
        self._send(status, page.encode(), "text/html; charset=utf-8")

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        if self._path() in ("/mcp", "/demo/mcp"):
            self._mcp(demo=self._path() == "/demo/mcp")
            return
        if self._path() == "/api/feedback":
            self._feedback()
            return
        if self._path() != "/login":
            self._json({"error": "not found"}, 404)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
        if auth.token_ok(form.get("token", [""])[0]):
            self._redirect("/", auth.set_cookie(self._secure()))
        else:
            self._login_page(401, "That token didn't work.")

    def _feedback(self) -> None:
        """Append one feedback event. JSON only: a cross-site form can't send it, and the cookie is SameSite=Strict."""
        if not auth.cookie_ok(self.headers.get("Cookie")):
            self._json({"error": "log in first"}, 401)
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json({"error": "JSON only"}, 415)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            event = json.loads(self.rfile.read(length) or b"{}")
            with data.connect() as conn:
                self._json({"event_id": data.record(conn, event)}, 201)
        except (ValueError, TypeError, AttributeError) as e:
            self._json({"error": str(e)}, 400)
        except data.NotYet:
            # A kind Idea Machine's database doesn't take yet: 503, so the app keeps it queued and tries later.
            self._json({"error": "Idea Machine doesn't accept this yet"}, 503)

    def _mcp(self, demo: bool = False) -> None:
        """MCP over Streamable HTTP (lattice.mcp) for tailnet clients. The bearer token is LATTICE_TOKEN. /demo/mcp is
        the read-only view with sensitive content held back."""
        if not auth.bearer_ok(self.headers.get("Authorization")):
            self._send(401, b'{"error": "bearer token required"}', "application/json", {"WWW-Authenticate": "Bearer"})
            return
        answer_mcp(self, writes=not demo, demo=demo)

    def do_DELETE(self) -> None:  # noqa: N802  (MCP session teardown: there are no sessions)
        self._json({"error": "no sessions"}, 405)

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path in ("/mcp", "/demo/mcp"):   # no server-initiated stream
            self._send(405, b'{"error": "POST only"}', "application/json", {"Allow": "POST"})
            return
        if path in STATIC:
            name, kind = STATIC[path]
            self._send(200, HERE.joinpath("static", name).read_bytes(), kind)
            return
        if path == "/login":
            self._login_page()
            return
        if path == "/logout":
            # Logging out wipes this device's copy of the lattice and any unsent feedback, not just the cookie.
            self._send(200, HERE.joinpath("logout.html").read_bytes(), "text/html; charset=utf-8",
                       {"Set-Cookie": auth.clear_cookie(), "Clear-Site-Data": '"cache", "storage"'})
            return
        if not auth.cookie_ok(self.headers.get("Cookie")):
            if path.startswith("/api/"):
                self._json({"error": "log in first"}, 401)
            else:
                self._redirect("/login")
            return
        try:
            with data.connect() as conn:
                if path == "/":
                    # The page carries no data: it renders the copy kept on the device and syncs it from
                    # /api/snapshot, so the service worker can keep the page itself for offline use.
                    self._send(200, HERE.joinpath("index.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/snapshot":
                    self._json(data.snapshot(conn))
                elif path == "/api/graph":
                    self._json(data.graph(conn))
                elif path == "/api/review":
                    self._json(data.review(conn))
                elif path == "/api/status":
                    self._json(data.status(conn))
                elif path == "/api/export":   # the whole lattice as Markdown, mine to keep
                    self._send(200, export.build(conn), "application/zip",
                               {"Content-Disposition": f'attachment; filename="lattice-{datetime.date.today()}.zip"'})
                elif path == "/api/near":   # search by meaning; the app falls back to words without it
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    try:
                        hits = meaning.near(conn, q.get("q", [""])[0][:1000], int(q.get("k", ["15"])[0]))
                        self._json({"available": True, "ideas": [{"id": i, "similarity": round(s, 3)} for i, s in hits]})
                    except meaning.Unavailable as e:
                        self._json({"available": False, "why": str(e), "ideas": []})
                elif path.startswith("/api/idea/"):
                    found = data.idea(conn, path.removeprefix("/api/idea/"))
                    self._json(found or {"error": "no such idea"}, 200 if found else 404)
                else:
                    self._json({"error": "not found"}, 404)
        except ValueError:
            self._json({"error": "bad idea id"}, 400)

    def log_message(self, fmt, *args) -> None:  # quieter than the default: no client addresses
        pass


class PublicMcpHandler(Handler):
    """The MCP endpoint alone, for Claude apps that connect from Anthropic's cloud (claude.ai, the phone app),
    reached through a Cloudflare Tunnel at LATTICE_PUBLIC_URL, with OAuth (lattice.oauth) in front of it. Nothing
    else is served here: not the app, the snapshot or the review queue.

    Everything but the sign-in page must come from LATTICE_MCP_ALLOW (by default Anthropic's outbound range), by the
    client address Cloudflare reports. The sign-in page is opened in my own browser, so it's open to any address,
    guarded by the Lattice token and a cap on wrong guesses. Requests are never logged.
    """

    def _base(self) -> str:
        return auth.public_url()

    def _from_claude(self) -> bool:
        return auth.mcp_source_ok(self.headers.get("CF-Connecting-IP"))

    def _nothing(self) -> None:
        self._send(404, b"", "text/plain")

    def _oauth_error(self, e: oauth.OAuthError) -> None:
        self._send(e.status, json.dumps({"error": e.error, "error_description": e.description}).encode(),
                   "application/json")

    def _form(self, limit: int = 8192) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), limit)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace")).items()}

    def _page(self, status: int, body: str) -> None:
        # The sign-in page can't be framed (clickjacking) and loads nothing from elsewhere.
        self._send(status, body.encode(), "text/html; charset=utf-8",
                   {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                                               "form-action 'self' https://claude.ai https://claude.com; "
                                               "frame-ancestors 'none'"})

    def _sign_in(self, status: int = 200, message: str = "", request: str = "", client: str = "", host: str = "") -> None:
        esc = html.escape
        page = (HERE.joinpath("authorize.html").read_text().replace("/*MESSAGE*/", esc(message))
                .replace("/*REQUEST*/", esc(request)).replace("/*CLIENT*/", esc(client)).replace("/*HOST*/", esc(host)))
        if (oauth.verify("request", request) or {}).get("res") == oauth.DEMO:   # the demo view: read-only, curated
            page = re.sub(r"<!--WRITES-->.*?<!--/WRITES-->", "<p><b>The demo connector:</b> read-only, with ideas tagged "
                          "sensitive held back and swearing blacked out, for showing Lattice to someone.</p>",
                          page, flags=re.S).replace("/*DEMO*/", " (demo)")
        self._page(status, page.replace("/*DEMO*/", ""))

    def _back_to(self, uri: str, **params) -> None:
        params = {k: v for k, v in params.items() if v is not None}
        self._redirect(uri + ("&" if "?" in uri else "?") + urllib.parse.urlencode(params))

    def do_GET(self) -> None:  # noqa: N802
        path, base = self._path(), self._base()
        if path == "/authorize":
            self._authorize_page()
            return
        if not self._from_claude():
            self._nothing()
        elif path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            self._json(oauth.protected_resource(base))
        elif path == "/.well-known/oauth-protected-resource/demo/mcp":
            self._json(oauth.protected_resource(base, oauth.DEMO))
        elif path == "/.well-known/oauth-authorization-server":
            self._json(oauth.authorization_server(base))
        elif path in ("/mcp", "/demo/mcp"):   # no server-initiated stream
            self._send(405, b'{"error": "POST only"}', "application/json", {"Allow": "POST"})
        else:
            self._nothing()

    def do_POST(self) -> None:  # noqa: N802
        path, base = self._path(), self._base()
        if path == "/authorize":
            self._authorize_submit()
            return
        if not self._from_claude():
            self._nothing()
        elif path in ("/mcp", "/demo/mcp"):
            header = self.headers.get("Authorization")
            # The demo view takes its own tokens, or a full one (it shows less, and never writes); /mcp only its own.
            grant = oauth.access(header, path) or (oauth.access(header) if path == "/demo/mcp" else None)
            if grant is None:
                self._send(401, b'{"error": "sign in first"}', "application/json",
                           {"WWW-Authenticate": oauth.challenge_header(base, "invalid_token" if header else None, path)})
                return
            if path == "/demo/mcp":
                answer_mcp(self, writes=False, demo=True)
            else:
                answer_mcp(self, writes=oauth.WRITE in grant["scopes"])
        elif path == "/register":
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 8192)
                meta = json.loads(self.rfile.read(length) or b"{}")
                self._json(oauth.register(meta if isinstance(meta, dict) else {}), 201)
            except ValueError:
                self._oauth_error(oauth.OAuthError("invalid_client_metadata", "JSON only"))
            except oauth.OAuthError as e:
                self._oauth_error(e)
        elif path == "/token":
            try:
                self._send(200, json.dumps(oauth.token(base, self._form())).encode(), "application/json",
                           {"Pragma": "no-cache"})
            except oauth.OAuthError as e:
                self._oauth_error(e)
        else:
            self._nothing()

    def do_DELETE(self) -> None:  # noqa: N802
        self._nothing()

    def _authorize_page(self) -> None:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        try:
            client, request = oauth.check_request(self._base(), q)
        except oauth.Redirect as r:
            self._back_to(r.uri, error=r.error, error_description=r.description, state=r.state, iss=self._base())
            return
        except oauth.OAuthError as e:
            self._sign_in(400, f"Can't sign in: {e.description}")
            return
        host = urllib.parse.urlsplit(q["redirect_uri"]).hostname
        self._sign_in(200, "", request, client["name"], host)

    def _authorize_submit(self) -> None:
        form = self._form()
        if not oauth.login_allowed():
            self._sign_in(429, "Too many wrong tokens this hour. Try again later.")
            return
        r = oauth.verify("request", form.get("request", ""))
        if r is None:
            self._sign_in(400, "This sign-in page expired. Start again from Claude.")
            return
        if not auth.token_ok(form.get("token", "")):
            oauth.login_failed()
            host = urllib.parse.urlsplit(r["redirect"]).hostname
            self._sign_in(401, "That token didn't work.", form["request"], "Claude", host)
            return
        g = oauth.grant(form["request"], allow_writes=form.get("writes") == "on")
        self._back_to(g["redirect"], code=g["code"], state=g["state"], iss=self._base())


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="lattice", description="Lattice: navigate the idea lattice")
    sub = p.add_subparsers(required=True, metavar="command")
    s = sub.add_parser("serve", help="serve the app")
    s.add_argument("--host", default="127.0.0.1", help="address to bind (localhost; tailscale serve does the rest)")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--mcp-port", type=int, default=8791,
                   help="the public MCP listener's port, opened only when LATTICE_PUBLIC_URL is set")
    args = p.parse_args(argv)
    auth.token()  # fail fast without a token
    public = auth.public_url()   # and on a bad public URL or allow list
    if public:
        auth.mcp_allow()
    with data.connect() as conn:  # and on a bad DSN or missing grants
        conn.execute("SELECT 1 FROM pub.ideas LIMIT 1")
    if public:
        mcp_server = ThreadingHTTPServer((args.host, args.mcp_port), PublicMcpHandler)
        threading.Thread(target=mcp_server.serve_forever, daemon=True).start()
        print(f"Public MCP listener on http://{args.host}:{args.mcp_port}/, published as {public}/mcp (OAuth), "
              f"for {', '.join(map(str, auth.mcp_allow()))}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Lattice on http://{args.host}:{args.port}/")
    server.serve_forever()
