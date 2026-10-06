"""`lattice serve`: a thin, read-only view of the idea lattice (ROADMAP slice 2 in Idea Machine).

Binds to localhost by default. Use --host with a tailnet address to open it from another device;
never bind it to the LAN or the internet, since evidence quotes include other people's words.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from lattice import data

PAGE = resources.files("lattice").joinpath("index.html")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        try:
            with data.connect() as conn:
                if self.path == "/":
                    # "</" is escaped so no string in the data can close the script element.
                    graph = json.dumps(data.graph(conn)).replace("</", "<\\/")
                    page = PAGE.read_text().replace("/*GRAPH*/null", graph, 1)
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif self.path == "/api/graph":
                    self._json(data.graph(conn))
                elif self.path.startswith("/api/idea/"):
                    found = data.idea(conn, self.path.removeprefix("/api/idea/"))
                    self._json(found or {"error": "no such idea"}, 200 if found else 404)
                else:
                    self._json({"error": "not found"}, 404)
        except ValueError:
            self._json({"error": "bad idea id"}, 400)

    def log_message(self, fmt, *args) -> None:  # quieter than the default: no client addresses
        pass


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="lattice", description="Lattice: navigate the idea lattice")
    sub = p.add_subparsers(required=True, metavar="command")
    s = sub.add_parser("serve", help="serve the read-only map")
    s.add_argument("--host", default="127.0.0.1", help="address to bind (localhost, or a tailnet address)")
    s.add_argument("--port", type=int, default=8790)
    args = p.parse_args(argv)
    with data.connect() as conn:  # fail fast on a bad DSN or missing grants
        conn.execute("SELECT 1 FROM pub.ideas LIMIT 1")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Lattice on http://{args.host}:{args.port}/")
    server.serve_forever()
