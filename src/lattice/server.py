"""`lattice serve`: the Lattice app (ROADMAP slices 2 and 5 in Idea Machine).

Behind a token login (lattice.auth). Binds to localhost by default; `tailscale serve` puts it on the tailnet
over HTTPS. Never bind it to the LAN or the internet, since evidence quotes include other people's words.
"""

import argparse
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from lattice import auth, data, mcp

HERE = resources.files("lattice")
STATIC = {"/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon-192.png": ("icon-192.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
          "/sw.js": ("sw.js", "text/javascript")}


def answer_mcp(h: BaseHTTPRequestHandler) -> None:
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
        answers = [a for a in (mcp.handle(m) for m in message) if a is not None]
        h._json(answers) if answers else h._send(202, b"", "application/json")
        return
    answer = mcp.handle(message)
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
        if self._path() == "/mcp":
            self._mcp()
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

    def _mcp(self) -> None:
        """MCP over Streamable HTTP (lattice.mcp) for tailnet clients. The bearer token is LATTICE_TOKEN."""
        if not auth.bearer_ok(self.headers.get("Authorization")):
            self._send(401, b'{"error": "bearer token required"}', "application/json", {"WWW-Authenticate": "Bearer"})
            return
        answer_mcp(self)

    def do_DELETE(self) -> None:  # noqa: N802  (MCP session teardown: there are no sessions)
        self._json({"error": "no sessions"}, 405)

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/mcp":   # no server-initiated stream
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
    reached through a Cloudflare Tunnel. Nothing else is served here: not the app, the snapshot or the review
    queue. Two locks, since those clients can't send a bearer header:

    - the path carries a secret, /mcp/<LATTICE_MCP_SECRET>;
    - the client address Cloudflare reports (CF-Connecting-IP) must be in LATTICE_MCP_ALLOW, by default Anthropic's
      published outbound range. That range is every Claude user's, so it narrows who can knock, not who gets in.

    Every refusal is a bare 404, so a scanner learns nothing. Requests are never logged: the path is the secret.
    """

    def _allowed(self) -> bool:
        return auth.mcp_path_ok(self._path()) and auth.mcp_source_ok(self.headers.get("CF-Connecting-IP"))

    def _nothing(self) -> None:
        self._send(404, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        answer_mcp(self) if self._allowed() else self._nothing()

    def do_GET(self) -> None:  # noqa: N802
        if self._allowed():   # no server-initiated stream
            self._send(405, b'{"error": "POST only"}', "application/json", {"Allow": "POST"})
        else:
            self._nothing()

    def do_DELETE(self) -> None:  # noqa: N802
        self._nothing()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="lattice", description="Lattice: navigate the idea lattice")
    sub = p.add_subparsers(required=True, metavar="command")
    s = sub.add_parser("serve", help="serve the app")
    s.add_argument("--host", default="127.0.0.1", help="address to bind (localhost; tailscale serve does the rest)")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--mcp-port", type=int, default=8791,
                   help="the public MCP listener's port, opened only when LATTICE_MCP_SECRET is set")
    args = p.parse_args(argv)
    auth.token()  # fail fast without a token
    public = auth.mcp_secret() is not None   # and on a short secret or a bad allow list
    with data.connect() as conn:  # and on a bad DSN or missing grants
        conn.execute("SELECT 1 FROM pub.ideas LIMIT 1")
    if public:
        mcp_server = ThreadingHTTPServer((args.host, args.mcp_port), PublicMcpHandler)
        threading.Thread(target=mcp_server.serve_forever, daemon=True).start()
        print(f"Public MCP listener on http://{args.host}:{args.mcp_port}/mcp/<LATTICE_MCP_SECRET>, "
              f"for {', '.join(map(str, auth.mcp_allow()))}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Lattice on http://{args.host}:{args.port}/")
    server.serve_forever()
