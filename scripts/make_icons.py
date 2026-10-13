"""Draw Lattice's app icons as PNGs with the standard library only: python3 scripts/make_icons.py

The Tree of Life on a dark square: the 22 paths faint, the serpent climbing from Malkuth to Kether beneath the
lightning flash coming down, the ten sephiroth on top. The same mark as docs/assets/lattice-mark-dark.svg. The art
stays inside the central 76% so iOS's rounded mask and Android's maskable crop never cut it.
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "lattice" / "static"
BG = (26, 25, 24)
INK = (232, 228, 218)
FLASH_COLOR = (157, 176, 255)
# The mark is drawn in a 120 x 122 box, the SVG's viewBox.
BOX_W, BOX_H = 120, 122
SEPHIROTH = [(60, 10), (84, 24), (36, 24), (84, 52), (36, 52), (60, 66), (84, 80), (36, 80), (60, 94), (60, 112)]
PATHS = [(0, 1), (0, 2), (0, 5), (1, 2), (1, 5), (1, 3), (2, 5), (2, 4), (3, 4), (3, 5), (3, 6), (4, 5), (4, 7),
         (5, 6), (5, 8), (5, 7), (6, 7), (6, 8), (6, 9), (7, 8), (7, 9), (8, 9)]
# The serpent as cubic Béziers, Malkuth to Kether, weaving across the middle pillar.
SERPENT = [((60, 116), (44, 110), (42, 98), (60, 94)), ((60, 94), (78, 90), (84, 72), (60, 66)),
           ((60, 66), (36, 60), (34, 44), (60, 38)), ((60, 38), (86, 32), (86, 18), (60, 10))]
SS = 3  # supersampling factor, for anti-aliasing


def _bezier_points(p0, p1, p2, p3, steps=40):
    for i in range(steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        yield (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class Canvas:
    """Painter's-order drawing in the mark's coordinates onto an n x n supersampled grid."""

    def __init__(self, n: int):
        self.n = n
        self.pixels = [[BG] * n for _ in range(n)]
        self.scale = 0.76 * n / BOX_H
        self.ox = (n - BOX_W * self.scale) / 2
        self.oy = (n - BOX_H * self.scale) / 2

    def _covered(self, a, b, width):
        """The pixels a round-capped line of this width covers."""
        ax, ay = self.ox + a[0] * self.scale, self.oy + a[1] * self.scale
        bx, by = self.ox + b[0] * self.scale, self.oy + b[1] * self.scale
        r = width * self.scale / 2
        for py in range(max(0, int(min(ay, by) - r)), min(self.n, int(max(ay, by) + r) + 2)):
            for px in range(max(0, int(min(ax, bx) - r)), min(self.n, int(max(ax, bx) + r) + 2)):
                if _segment_distance(px + 0.5, py + 0.5, ax, ay, bx, by) <= r:
                    yield px, py

    def lines(self, segments, width, color, alpha=1.0):
        # Collect coverage first so overlapping segments blend once, not once per segment.
        covered = {p for a, b in segments for p in self._covered(a, b, width)}
        for px, py in covered:
            old = self.pixels[py][px]
            self.pixels[py][px] = tuple(round(old[k] + (color[k] - old[k]) * alpha) for k in range(3))

    def polyline(self, points, width, color):
        self.lines(list(zip(points, points[1:])), width, color)

    def disc(self, center, radius, color):
        self.lines([(center, center)], radius * 2, color)


def render(size: int) -> bytes:
    canvas = Canvas(size * SS)
    canvas.lines([(SEPHIROTH[a], SEPHIROTH[b]) for a, b in PATHS], 1.5, INK, 0.16)
    canvas.polyline([p for segment in SERPENT for p in _bezier_points(*segment)], 2.6, INK)
    canvas.polyline(SEPHIROTH, 7.5, BG)  # the flash in emanation order: first the gap where it crosses the serpent
    canvas.polyline(SEPHIROTH, 3.5, FLASH_COLOR)
    for i, point in enumerate(SEPHIROTH):
        canvas.disc(point, 4 if i == 0 else 3.5, INK)
    out = bytearray()
    for y in range(size):  # box-filter SSxSS blocks down to one pixel
        out.append(0)
        for x in range(size):
            block = [canvas.pixels[y * SS + j][x * SS + i] for j in range(SS) for i in range(SS)]
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
