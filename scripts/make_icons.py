"""Draw Lattice's app icons as PNGs with the standard library only: python3 scripts/make_icons.py

A small lattice (nodes in the map's theme colors, joined by thin light lines) on a dark square. The art stays
inside the central 76% so iOS's rounded mask and Android's maskable crop never cut it.
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "lattice" / "static"
BG = (26, 26, 25)
EDGE = (160, 159, 151)
# (x, y, radius) in a 0..1 square, and the dark-mode theme colors they wear
NODES = [((0.50, 0.24, 0.075), (57, 135, 229)), ((0.26, 0.42, 0.065), (25, 158, 112)),
         ((0.74, 0.40, 0.065), (217, 89, 38)), ((0.36, 0.71, 0.070), (201, 133, 0)),
         ((0.68, 0.72, 0.060), (213, 81, 129)), ((0.52, 0.50, 0.045), (144, 133, 233))]
EDGES = [(0, 1), (0, 2), (1, 3), (2, 4), (3, 4), (5, 0), (5, 1), (5, 3), (5, 4)]
SS = 3  # supersampling factor, for anti-aliasing


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def render(size: int) -> bytes:
    n = size * SS
    # Map the 0..1 art space into the central 76%.
    art = lambda v: (0.12 + 0.76 * v) * n  # noqa: E731
    nodes = [((art(x), art(y), r * 0.76 * n), c) for (x, y, r), c in NODES]
    width = max(1.0, 0.0055 * n)
    rows = []
    for py in range(n):
        row = []
        for px in range(n):
            color = BG
            if any(_segment_distance(px, py, nodes[a][0][0], nodes[a][0][1], nodes[b][0][0], nodes[b][0][1]) <= width
                   for a, b in EDGES):
                color = EDGE
            for (cx, cy, r), c in nodes:
                d = math.hypot(px - cx, py - cy)
                if d <= r:
                    color = c
                elif d <= r + 0.018 * n:  # a ring of background around each node, like the map's surface gap
                    color = BG
            row.append(color)
        rows.append(row)
    out = bytearray()
    for y in range(size):  # box-filter SSxSS blocks down to one pixel
        out.append(0)
        for x in range(size):
            block = [rows[y * SS + j][x * SS + i] for j in range(SS) for i in range(SS)]
            out.extend(round(sum(p[k] for p in block) / len(block)) for k in range(3))
    return _png(size, size, bytes(out))


def _png(w: int, h: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)]:
        (OUT / name).write_bytes(render(size))
        print(f"wrote {OUT / name}")
