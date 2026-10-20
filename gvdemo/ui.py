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
SPARK_W = 60

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


def squeeze(values, width):
    """Fit a series into exactly width columns.

    Long series keep each bucket's max; short ones are stretched, so a chart
    always spans its width from the first sample to the latest.
    """
    if len(values) <= width:
        return [values[i * len(values) // width] for i in range(width)]
    k = len(values) / width
    return [max(values[int(i * k):max(int((i + 1) * k), int(i * k) + 1)])
            for i in range(width)]


def spark(values, color, width=SPARK_W, accent=None):
    """A one-row sparkline from zero. accent colours the latest point."""
    if not values:
        return Text("")
    values = squeeze(values, width)
    vmax = max(values) or 1
    t = Text()
    for i, v in enumerate(values):
        style = accent if accent and i == len(values) - 1 else color
        if v <= 0:
            t.append("▁", style="grey30")
        else:
            t.append("▁▂▃▄▅▆▇█"[min(7, int(v / vmax * 8))], style=style)
    return t


def area(values, width, height, color):
    """A multi-row area chart from zero, as a list of Text rows (top first)."""
    values = squeeze(values, width) if values else []
    vmax = max(values) if values and max(values) > 0 else 1
    rows = []
    for r in range(height - 1, -1, -1):  # r = row index from the bottom
        t = Text()
        for v in values:
            eighths = v / vmax * height * 8 - r * 8
            if eighths >= 8:
                t.append("█", style=color)
            elif eighths >= 1:
                t.append("▁▂▃▄▅▆▇"[int(eighths) - 1], style=color)
            else:
                t.append(" ")
        rows.append(t)
    return rows


def on_time(points, t_max, width):
    """Place (t, value) points into width columns by time, not by index.

    A column holds the largest value that fell into it; empty columns repeat
    the previous value, so sparse series (like probes) draw as steps.
    """
    cols = [None] * width
    for t, v in points:
        c = min(width - 1, int(t / t_max * (width - 1))) if t_max > 0 else 0
        cols[c] = v if cols[c] is None else max(cols[c], v)
    last = 0
    for i, v in enumerate(cols):
        last = v if v is not None else last
        cols[i] = last
    return cols


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


def meter(fraction, width, color, track="grey23"):
    """A progress track: filled part in color, the rest a dim track."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return Text.assemble(("━" * filled, color), ("━" * (width - filled), track))


def tile(label, value, sub="", trend=None):
    """A stat tile: label, value in ink (not series colour), context line."""
    parts = [Text(label, style="grey62"), Text(value, style="bold white")]
    if sub:
        parts.append(Text(sub, style="grey50"))
    if trend is not None:
        parts.append(trend)
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


def pct(fraction):
    """A fraction of one CPU as a percentage, with digits for tiny values."""
    v = fraction * 100
    if v == 0:
        return "0%"
    return f"{v:.1f}%" if v >= 1 else f"{v:.2f}%" if v >= 0.1 else f"{v:.3f}%"


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
