"""Shared rendering helpers. Scenes print through out() so --svg can replay them."""

import time

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .activity import activity

GV = "green3"     # gVisor colour
KATA = "orange3"  # Kata colour

console = Console()
FRAME = []  # what is on screen at the end of the current scene
ACTIVITY = {"mode": "auto", "width": None}  # mode: auto | side | bottom | off


def out(*renderables):
    console.print(*renderables)
    FRAME.extend(renderables)


def _layout():
    """(mode, side panel width) for the current terminal."""
    mode = ACTIVITY["mode"]
    if mode == "auto":
        mode = "side" if console.width >= 170 else "bottom"
    # The scene keeps 120 columns; the panel takes the rest, 60 to 100.
    side_w = ACTIVITY["width"] or max(60, min(100, console.width - 121))
    return mode, side_w


def main_width():
    """Columns available to a scene's live view, beside any side panel."""
    mode, side_w = _layout()
    return console.width - side_w - 1 if mode == "side" else console.width


def with_activity(main):
    """Put the behind-the-scenes panel beside main, or below it if narrow."""
    mode, side_w = _layout()
    width = console.width
    if mode == "off":
        return main
    if mode == "side":
        # Leave room for the scene title printed above the live view.
        panel = activity.panel(height=max(12, console.height - 6), width=side_w)
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=width - side_w - 1)
        grid.add_column(width=side_w)
        grid.add_row(main, panel)
        return grid
    return Group(main, activity.panel(height=12, width=width))


def bar(value, vmax, width, color):
    """A horizontal bar with 1/8-cell resolution."""
    blocks = " ▏▎▍▌▋▊▉█"
    cells = 0 if vmax <= 0 else max(0.0, min(1.0, value / vmax)) * width
    full = int(cells)
    s = "█" * full
    if full < width:
        s += blocks[int((cells - full) * 8)]
    return Text(s.ljust(width), style=color)


def time_axis(width, t_max, marks, idle_t=None):
    """Axis rows under time charts: ┬ at each mark, ┊ at idle, seconds below."""
    line = ["─"] * width
    col = lambda t: min(width - 1, int(t / t_max * (width - 1))) if t_max else 0
    for t in marks:
        line[col(t)] = "┬"
    if idle_t is not None:
        line[col(idle_t)] = "┊"
    axis = Text("".join(line), style="grey42")
    end = f"{t_max:.0f} s"
    labels = Text("0 s" + end.rjust(width - 3), style="grey50")
    return axis, labels


# Braille cells hold 2 x 4 dots; these are the bits for (column, row-from-top).
_DOT = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def line_chart(points, t_max, width, height, color, fmt, marks=(),
               idle_t=None, ylab_w=9):
    """A thin line chart in braille dots, from zero, as a list of Text rows.

    points are (t, value). Each cell is 2 x 4 dots, so the line has 2*width
    time steps and 4*height levels. Nothing is drawn before the first point.
    Rows: the plot with a y axis (max at the top, 0 at the bottom), then the
    time axis with marks, then its labels.
    """
    xs, ys = 2 * width, 4 * height
    cols = [None] * xs
    for t, v in points:
        c = min(xs - 1, int(t / t_max * (xs - 1))) if t_max > 0 else 0
        cols[c] = v if cols[c] is None else max(cols[c], v)
    seen = None
    for i, v in enumerate(cols):  # carry the last value forward
        seen = v if v is not None else seen
        cols[i] = seen
    vmax = max([v for v in cols if v is not None] + [0]) or 1
    cells = [[0] * width for _ in range(height)]
    prev = None
    for x, v in enumerate(cols):
        if v is None:
            continue
        y = round(v / vmax * (ys - 1))
        # Join to the previous point with a vertical run, so the line is
        # continuous through jumps.
        lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
        for yy in range(lo, hi + 1):
            cells[height - 1 - yy // 4][x // 2] |= _DOT[x % 2][3 - yy % 4]
        prev = y
    rows = []
    for r in range(height):
        label = fmt(vmax) if r == 0 else "0" if r == height - 1 else ""
        t = Text(label.rjust(ylab_w), style="grey50")
        t.append("┤" if r in (0, height - 1) else "│", style="grey42")
        t.append("".join(chr(0x2800 + b) if b else " " for b in cells[r]),
                 style=color)
        rows.append(t)
    axis, labels = time_axis(width, t_max, marks, idle_t)
    rows.append(Text(" " * ylab_w + "└", style="grey42") + axis)
    rows.append(Text(" " * (ylab_w + 1)) + labels)
    return rows


def meter(fraction, width, color, track="grey23"):
    """A progress track: filled part in color, the rest a dim track."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return Text.assemble(("━" * filled, color), ("━" * (width - filled), track))


def tile(label, value, sub=""):
    """A stat tile: label, value in ink (not series colour), context line."""
    parts = [Text(label, style="grey62"), Text(value, style="bold white")]
    if sub:
        parts.append(Text(sub, style="grey50"))
    for p in parts:
        p.no_wrap = True
        p.overflow = "ellipsis"
    return Panel(Group(*parts), border_style="grey30", padding=(0, 1))


def millicores(cores):
    """CPU as a number of millicores (1/1000 core), digits for tiny values."""
    m = cores * 1000
    if m == 0:
        return "0"
    return f"{m:.0f}" if m >= 10 else f"{m:.1f}" if m >= 0.1 else f"{m:.2f}"


def fmt_cpu(cores):
    """Absolute CPU: cores when 1 or more, else millicores."""
    if cores >= 1:
        return f"{cores:.1f} cores"
    return f"{millicores(cores)} millicores"


def fmt_ms(ms):
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.0f} ms"


def fmt_spread(xs):
    """Sample size and range, e.g. "n=10, 72–80 ms"."""
    lo, hi = min(xs), max(xs)
    rng = f"{lo:.0f}–{hi:.0f} ms" if hi < 1000 else f"{fmt_ms(lo)}–{fmt_ms(hi)}"
    return f"n={len(xs)}, {rng}"


def percentile(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def title(n, text, sub):
    return Panel(Text.assemble((text, "bold white"), "\n",
                               (sub, "italic grey70"), justify="center"),
                 title=f"[bold]{n}[/]", border_style="bright_blue",
                 box=box.HEAVY)


def source_line(ref):
    return Text(f"Kata source: {ref.get('source', 'n/a')}", style="grey50")


def pause(args, msg="Press Enter to continue"):
    if args.auto:
        time.sleep(args.auto)
    else:
        console.input(f"\n[grey50]{msg}…[/]")
