"""The opening page: the gVisor logo as pixel art."""

import collections

from rich.console import Group
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..logo_pixels import ART
from ..ui import console, out

# The logo's own colours on the terminal's background. Its navy lettering
# would vanish on a dark terminal, so the wordmark is white (as logos do on
# dark backgrounds); the helmet keeps its navy, white and gold.
COLOURS = {"w": "#ffffff", "n": "#262262", "g": "#fbb03b", "t": "#ffffff"}


# Quadrant blocks by which of a cell's 4 pixels are in the foreground
# colour: top-left 1, top-right 2, bottom-left 4, bottom-right 8.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
RGB = {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
       for k, v in COLOURS.items()}
RGB["."] = (0, 0, 0)  # clear: the terminal's (dark) background


def cell(px):
    """(glyph, Style) for one cell from its 4 pixels (TL, TR, BL, BR). A cell
    shows 2 colours at most; a third is drawn as the closer of the two.

    A cell's background colour fills the whole cell, but a block glyph can
    stop short of the line's full height and leave a thin gap between rows
    (Terminal.app does). So solid areas are background colour, and in a
    two-colour cell the larger part is the background, the smaller the
    glyph. Only cells on the logo's edge, half clear, need a glyph alone."""
    common = [k for k, _ in collections.Counter(px).most_common(2)]
    if len(common) == 1:
        k = common[0]
        return (" ", None if k == "." else Style(bgcolor=COLOURS[k]))

    def near(k):
        return min(common, key=lambda c: sum((a - b) ** 2 for a, b in
                                             zip(RGB[k], RGB[c])))
    px = [k if k in common else near(k) for k in px]
    if "." in common:  # an edge: the coloured part as a glyph
        fg, bg = common[1] if common[0] == "." else common[0], None
    else:  # common[0] is the larger part
        bg, fg = common
    mask = sum(bit for bit, k in zip((1, 2, 4, 8), px) if k == fg)
    return QUADRANTS[mask], Style(color=COLOURS[fg],
                                  bgcolor=COLOURS[bg] if bg else None)


class Logo:
    """The largest pixel-art logo that fits, drawn with quadrant blocks:
    2 x 2 pixels per character cell."""

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
        pad = Segment(" " * ((options.max_width - w // 2) // 2))
        for top, bottom in zip(art[::2], art[1::2]):
            yield pad
            for x in range(0, w, 2):
                glyph, style = cell((top[x], top[x + 1],
                                     bottom[x], bottom[x + 1]))
                yield Segment(glyph, style)
            yield Segment.line()


def run(rs, ref, args, results):
    console.clear()
    out(Group(Text(""), Logo(), Text(""),
              Text("Live demo", style="bold bright_white", justify="center"),
              Text("Every number on the next pages is measured on this "
                   "machine", style="bright_white", justify="center")))
