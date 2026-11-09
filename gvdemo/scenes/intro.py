"""The opening page: the gVisor logo as pixel art, with a glare that sweeps
across it now and then."""

import collections
import random
import time

from rich.console import Group
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..logo_pixels import ART
from ..ui import ANIMATE, console, out

# The logo's own colours on the terminal's background. Its navy lettering
# would vanish on a dark terminal, so the wordmark is white (as logos do on
# dark backgrounds); the helmet keeps its navy, white and gold.
COLOURS = {"w": "#ffffff", "n": "#262262", "g": "#fbb03b", "t": "#ffffff"}
RGB = {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
       for k, v in COLOURS.items()}

# Quadrant blocks by which of a cell's 4 pixels are in the foreground
# colour: top-left 1, top-right 2, bottom-left 4, bottom-right 8.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"

# The glare: a band of light that sweeps across the logo, leaning right,
# every few seconds at random. Pixels in the band move toward GLARE_RGB,
# most at its middle. Only the helmet catches it, not the wordmark or the
# background. GLARE_RGB is a light icy blue, not white: the helmet's face is
# white already and would not show a white glare.
GLARE_S = 1.8            # one sweep, left to right
GLARE_GAP = (0.8, 2.0)   # seconds between sweeps, at random
GLARE_W = 0.08           # the band's half width, as a fraction of the logo
GLARE_MAX = 0.8          # how far the brightest pixel moves toward it
GLARE_RGB = (196, 236, 255)  # the glare's colour: a light icy blue
GLARE_LEAN = 0.8         # pixels to the right per pixel up


def hexc(c):
    return "#%02x%02x%02x" % c


def cell(px):
    """(glyph, Style) for one cell from its 4 pixels (TL, TR, BL, BR), each
    an RGB tuple or None (clear). A cell shows 2 colours at most; a third is
    drawn as the closer of the two.

    A cell's background colour fills the whole cell, but a block glyph can
    stop short of the line's full height and leave a thin gap between rows
    (Terminal.app does). So solid areas are background colour, and in a
    two-colour cell the larger part is the background, the smaller the
    glyph. Only cells on the logo's edge, half clear, need a glyph alone."""
    common = [k for k, _ in collections.Counter(px).most_common(2)]
    if len(common) == 1:
        k = common[0]
        return (" ", None if k is None else Style(bgcolor=hexc(k)))

    def near(k):
        return min(common, key=lambda c: sum(
            (a - b) ** 2 for a, b in zip(k or (0, 0, 0), c or (0, 0, 0))))
    px = [k if k in common else near(k) for k in px]
    if None in common:  # an edge: the coloured part as a glyph
        fg, bg = common[1] if common[0] is None else common[0], None
    else:  # common[0] is the larger part
        bg, fg = common
    mask = sum(bit for bit, k in zip((1, 2, 4, 8), px) if k == fg)
    return QUADRANTS[mask], Style(color=hexc(fg),
                                  bgcolor=hexc(bg) if bg else None)


class Logo:
    """The largest pixel-art logo that fits, drawn with quadrant blocks:
    2 x 2 pixels per character cell. glare is where the glare's band is,
    from 0 (left edge) to 1 (right edge), or None."""

    def __init__(self, glare=None):
        self.glare = glare

    def __rich_console__(self, console, options):
        for s in sorted(ART):  # finest first
            art = ART[s]
            if (len(art[0]) // 2 <= options.max_width and
                    (len(art) + 1) // 2 <= options.max_height - 6):
                break
        else:
            yield Text("gVisor", style="bold bright_white", justify="center")
            return
        w = len(art[0]) // 2 * 2
        art = [row[:w] for row in art]
        if len(art) % 2:
            art = art + ["." * w]
        h = len(art)
        span = w + h * GLARE_LEAN

        def colour(k, x, y):
            if k == ".":
                return None
            c = RGB[k]
            if self.glare is not None and k != "t":  # the helmet, not text
                d = abs((x + (h - y) * GLARE_LEAN) / span - self.glare)
                if d < GLARE_W:
                    f = GLARE_MAX * (1 - d / GLARE_W)
                    c = tuple(round(a + (g - a) * f)
                              for a, g in zip(c, GLARE_RGB))
            return c

        pad = Segment(" " * ((options.max_width - w // 2) // 2))
        for y in range(0, h, 2):
            top, bottom = art[y], art[y + 1]
            yield pad
            for x in range(0, w, 2):
                glyph, style = cell((colour(top[x], x, y),
                                     colour(top[x + 1], x + 1, y),
                                     colour(bottom[x], x, y + 1),
                                     colour(bottom[x + 1], x + 1, y + 1)))
                yield Segment(glyph, style)
            yield Segment.line()


class Glare:
    """When the glare sweeps: at random intervals of GLARE_GAP seconds."""

    def __init__(self):
        self.start = time.monotonic() + random.uniform(0.5, 1.5)

    def position(self):
        """Where the band is now (it starts and ends off the logo), or None
        between sweeps."""
        t = (time.monotonic() - self.start) / GLARE_S
        if t < 0:
            return None
        if t > 1:
            self.start = time.monotonic() + random.uniform(*GLARE_GAP)
            return None
        return -GLARE_W + t * (1 + 2 * GLARE_W)


def page(glare=None):
    return Group(Text(""), Logo(glare), Text(""),
                 Text("Live demo", style="bold bright_white",
                      justify="center"),
                 Text("Every number on the next pages is measured on this "
                      "machine", style="bright_white", justify="center"))


def run(rs, ref, args, results):
    console.clear()
    out(page())
    # While the page waits for the presenter, the glare sweeps now and then.
    glare = Glare()
    ANIMATE["render"] = lambda: page(glare.position())
    ANIMATE["moving"] = lambda: glare.position() is not None
