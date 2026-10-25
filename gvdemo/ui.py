"""Shared rendering helpers. Scenes print through out() so --svg can replay them."""

import contextlib
import os
import shutil
import signal
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.live_render import LiveRender
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from .activity import activity
from .hostmon import LIMIT
from .colors import MUTED

GV = "green3"     # gVisor colour
KATA = "orange3"  # Kata colour

console = Console()
FRAME = []  # what is on screen at the end of the current scene
# A scene's heading (title and cgroup line), drawn at the top of its live
# view so the whole scene can be redrawn when the terminal is resized.
HEADER = []
_VIEW = {"live": False, "resized": False, "prompt": None}


def _fit():
    """Draw at most one line less than the window: the last line is kept
    for the prompt, so showing it never scrolls the scene up."""
    console.height = max(10, shutil.get_terminal_size().lines - 1)


def _on_resize(signum, frame):
    """Terminal resized: text on screen re-wraps, so redraw from scratch."""
    if _VIEW["live"]:
        _VIEW["resized"] = True  # the live view clears on its next redraw
        return
    # Waiting at the prompt: reprint the scene's last frame, cut to fit
    # above the prompt line, and the prompt.
    _fit()
    console.clear()
    console.print(LiveRender(Group(*FRAME), vertical_overflow="ellipsis"))
    if _VIEW["prompt"]:
        console.print(_VIEW["prompt"], end="")


def watch_resize():
    signal.signal(signal.SIGWINCH, _on_resize)


@contextlib.contextmanager
def scene_live(render, refresh_per_second=8):
    """A scene's live view: its heading and body as one redrawable frame.

    What the scene printed so far (its title) moves into HEADER and is drawn
    inside the view; taller-than-the-terminal frames are cut at the bottom.
    """
    HEADER[:] = FRAME
    FRAME.clear()
    console.clear()
    _VIEW["live"] = True
    # When Live stops, it draws the frame once more with nothing cut off,
    # which scrolls a scene taller than the window up (even when transient).
    # So its last draw is empty, it erases itself, and the final frame is
    # drawn below, cut to fit above the prompt line.
    stopping = []
    view = lambda: Text("") if stopping else render()  # noqa: E731
    try:
        with Live(console=console, refresh_per_second=refresh_per_second,
                  get_renderable=view, transient=True) as live:
            try:
                yield live
            finally:
                stopping.append(True)
    finally:
        _VIEW["live"] = False
        console.print(LiveRender(render(), vertical_overflow="ellipsis"))
ACTIVITY = {"mode": "auto", "width": None}  # mode: auto | side | bottom | off


def out(*renderables):
    console.print(*renderables)
    FRAME.extend(renderables)


def _layout():
    """(mode, side panel width) for the current terminal."""
    mode = ACTIVITY["mode"]
    if mode == "auto":
        mode = "side" if console.width >= 170 else "bottom"
    # The scene keeps 130 columns; the panel takes the rest, 56 to 90.
    side_w = ACTIVITY["width"] or max(56, min(90, console.width - 131))
    return mode, side_w


def main_width():
    """Columns available to a scene's live view, beside any side panel."""
    mode, side_w = _layout()
    return console.width - side_w - 1 if mode == "side" else console.width


def with_activity(main):
    """The scene's heading, then main with the behind-the-scenes panel
    beside it (or below it if the terminal is narrow)."""
    _fit()
    if _VIEW["resized"]:
        _VIEW["resized"] = False
        console.clear()
    mode, side_w = _layout()
    width = console.width
    if mode == "off":
        return Group(*HEADER, main)
    if mode == "side":
        # Leave room for the heading above the panel.
        panel = activity.panel(height=max(12, console.height - 6), width=side_w)
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=width - side_w - 1)
        grid.add_column(width=side_w)
        grid.add_row(main, panel)
        return Group(*HEADER, grid)
    return Group(*HEADER, main, activity.panel(height=12, width=width))


def bar(value, vmax, width, color):
    """A horizontal bar in whole cells. Partial blocks (▌) gave the bars a
    ragged end; a value above zero still shows at least one cell."""
    frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, value / vmax))
    cells = round(frac * width) or (1 if frac > 0 else 0)
    return Text(("█" * cells).ljust(width), style=color)


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
    labels = Text("0 s" + end.rjust(width - 3), style=MUTED)
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
        # Zero is drawn at the bottom edge of the last row, so "0" goes on
        # the axis line below it, not beside that row.
        label = fmt(vmax) if r == 0 else ""
        t = Text(label.rjust(ylab_w), style=MUTED)
        t.append("┤" if r == 0 else "│", style="grey42")
        t.append("".join(chr(0x2800 + b) if b else " " for b in cells[r]),
                 style=color)
        rows.append(t)
    axis, labels = time_axis(width, t_max, marks, idle_t)
    rows.append(Text("0".rjust(ylab_w), style=MUTED) +
                Text("└", style="grey42") + axis)
    rows.append(Text(" " * (ylab_w + 1)) + labels)
    return rows


def histogram(values, width, height, color, marks=(), bins=20,
              fill="dark_green", ends_style=MUTED, mark_style="white",
              mark_time_style="white"):
    """A histogram drawn as a smooth outline, as Text rows.

    The values are counted in bins equal intervals over their range; a thin
    braille line joins each interval's count (at its middle), from zero at
    the left edge to zero at the right, with a light fill of one dot per
    cell underneath.
    marks are (label, value) pairs, drawn as ┴ on the axis and labelled
    under it. Rows: the plot with a count axis, the value axis, then the
    range and the mark labels.
    """
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / span * bins))] += 1
    cmax = max(counts)
    xs, ys = 2 * width, 4 * height
    # The outline's corner points, in dot columns: zero, each interval's
    # middle, zero.
    pts = [(0.0, 0)] + [((i + 0.5) / bins * (xs - 1), c)
                        for i, c in enumerate(counts)] + [(xs - 1.0, 0)]
    line = [[0] * width for _ in range(height)]
    shade = [[0] * width for _ in range(height)]

    def dot(cells, x, y):
        cells[height - 1 - y // 4][x // 2] |= _DOT[x % 2][3 - y % 4]

    prev, j = None, 0
    for x in range(xs):
        while j < len(pts) - 2 and pts[j + 1][0] < x:
            j += 1
        (x0, c0), (x1, c1) = pts[j], pts[j + 1]
        c = c0 + (c1 - c0) * ((x - x0) / (x1 - x0) if x1 > x0 else 0)
        y = round(c / cmax * (ys - 1))
        lo_y, hi_y = (y, y) if prev is None else (min(prev, y), max(prev, y))
        for yy in range(lo_y, hi_y + 1):
            dot(line, x, yy)
        for yy in range(0, lo_y):
            if x % 2 == 0 and yy % 4 == 1:  # a light fill: one dot per cell
                dot(shade, x, yy)
        prev = y
    rows = []
    for r in range(height):
        # Zero is at the bottom edge of the last row: "0" goes on the axis.
        t = Text(f"{cmax if r == 0 else '':>4} ", style="bright_white")
        t.append("┤" if r == 0 else "│", style="grey42")
        for cl, cs in zip(line[r], shade[r]):
            bits = cl | cs
            t.append(chr(0x2800 + bits) if bits else " ",
                     style=color if cl else fill)
        rows.append(t)
    axis = ["─"] * width
    used = [False] * width
    placed = []  # (start column, mark name, value text)
    col = lambda v: min(width - 1, int((v - lo) / span * width))
    for name, v in marks:
        axis[col(v)] = "┴"
    for name, v in marks:  # labels left to right, skipping overlaps
        text, c = f"{name} {fmt_ms(v)}", col(v)
        c = max(0, min(c - len(text) // 2, width - len(text)))
        if not any(used[max(0, c - 1):c + len(text) + 1]):
            used[c:c + len(text)] = [True] * len(text)
            placed.append((c, name, fmt_ms(v)))
    labels = Text("      ")
    at = 0
    for c, name, value in sorted(placed):
        labels.append(" " * (c - at))
        labels.append(name, style=mark_style)
        labels.append(" ")
        labels.append(value, style=mark_time_style)
        at = c + len(name) + 1 + len(value)
    rows.append(Text("   0 ", style="bright_white") +
                Text("└" + "".join(axis), style="grey42"))
    # The fastest and slowest values, labelled, at the two ends of the axis.
    lo_t, hi_t = fmt_ms(lo), fmt_ms(hi)
    ends = Text("      ")
    ends.append("min ", style=mark_style)
    ends.append(lo_t, style=ends_style)
    ends.append(" " * max(1, width - 8 - len(lo_t) - len(hi_t)))
    ends.append("max ", style=mark_style)
    ends.append(hi_t, style=ends_style)
    rows.append(ends)
    rows.append(labels)
    return rows


def meter(fraction, width, color, track="grey23"):
    """A progress track: filled part in color, the rest a dim track."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return Text.assemble(("━" * filled, color), ("━" * (width - filled), track))


def tile(label, value, sub=""):
    """A stat tile: label, value in ink (not series colour), context line."""
    parts = [Text(label, style=MUTED), Text(value, style="bold white")]
    if sub:
        parts.append(Text(sub, style=MUTED))
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


# Large titles use DEC double-size lines: "ESC # 3" draws a line's top half
# and "ESC # 4" its bottom half, each cell twice as wide and tall. macOS
# Terminal.app, xterm, Windows Terminal and Konsole support them; other
# terminals (iTerm2) show the title twice, so --title-size normal turns
# them off.
TITLE = {"large": True}
_RAW = (("raw",),)  # marks a segment as an escape code that takes no cells


class Large:
    """One line of text, drawn double size (centred)."""

    def __init__(self, text):
        self.text = text

    def __rich_console__(self, console, options):
        text = self.text.copy()
        text.justify = "center"
        half = options.max_width // 2
        # An SVG export (record) and a pipe get the title once, normal size.
        if not TITLE["large"] or not console.is_terminal or console.record:
            yield text
            return
        if text.cell_len > half:
            # Too wide: normal size, still two rows so nothing below moves.
            # "ESC # 5" makes each row single size again, in case it was
            # drawn double before (a live count that grew a digit).
            line = console.render_lines(text, options, pad=False)[0]
            for row in (line, []):
                yield Segment("\x1b#5", control=_RAW)
                yield from row
                yield Segment.line()
            return
        line = console.render_lines(text, options.update_width(half),
                                    pad=False)[0]
        for half_code in ("\x1b#3", "\x1b#4"):
            yield Segment(half_code, control=_RAW)
            yield from line
            yield Segment.line()


# Solid digits: 3 x 5 pixels, each pixel a full block two cells wide.
_DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", "###", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", "..#", "..#", "..#"],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    ",": [".", ".", ".", "#", "#"],
}


def big_number(text, style):
    """A number (digits and commas) five rows tall, as one Text."""
    rows = ["  ".join("".join("██" if p == "#" else "  "
                              for p in _DIGITS[c][r]) for c in text)
            for r in range(5)]
    return Text("\n".join(rows), style=style, no_wrap=True)


def title(text, sub):
    """A scene's heading, with the resource setup right under it."""
    # Capitalize each word's first letter; words with capitals already
    # ("gVisor", "VMs") stay as they are.
    text = " ".join(word if any(c.isupper() for c in word)
                    else word[:1].upper() + word[1:] for word in text.split())
    return Group(Large(Text(text, style="bold bright_white")),
                 Text(sub, style="italic bright_white", justify="center"),
                 limit_banner())


def limit_banner():
    """One line naming the cgroup limit every sandbox runs under (or none)."""
    if LIMIT["cgroup"]:
        parts = []
        if LIMIT["cpus"]:
            parts.append(f"{LIMIT['cpus']} CPUs")
        if LIMIT["mem_bytes"]:
            parts.append(f"{LIMIT['mem_bytes'] / 2**30:g} GiB memory")
        return Text.assemble(
            ("⚙ cgroup limit ", "bold black on yellow"),
            (f" all sandboxes share {' and '.join(parts)}", "yellow"))
    return Text.assemble(("⚙ no cgroup limit ", f"bold black on {MUTED}"),
                         (f" sandboxes may use the whole host "
                          f"({os.cpu_count()} CPUs)", MUTED))


def source_line(ref):
    return Text(f"Kata source: {ref.get('source', 'n/a')}",
                style="bright_white")


def ask_next(args, last=False, upcoming=None, note="", first=False):
    """After a scene: "next", "back" (to the previous scene), "again" (rerun
    this one) or "quit". Back and again run the scene from a clean state.

    With --auto there is no question; the demo moves on after a delay,
    counting down to the upcoming scene so nobody thinks it froze.
    """
    if args.auto:
        end = time.monotonic() + args.auto
        with console.status("") as status:
            while (left := end - time.monotonic()) > 0:
                status.update(
                    f"[{MUTED}]{note}Next: [bold white]{upcoming or 'finish'}"
                    f"[/] in {left:.0f} s · Ctrl-C to stop[/]")
                time.sleep(min(0.2, left))
        return "next"
    nxt = "finish" if last else "next scene"
    back = "" if first else " · [bold]b[/bold]: previous scene"
    prompt = (f"[grey50]Enter: {nxt}{back} · [bold]r[/bold]: run this scene "
              "again from a clean state · [bold]q[/bold]: quit[/] ")
    _VIEW["prompt"] = prompt
    try:
        answer = console.input(prompt).strip().lower()
    finally:
        _VIEW["prompt"] = None
    choice = {"b": "back", "r": "again", "q": "quit"}.get(answer[:1], "next")
    # The first scene has no previous one: run it again instead.
    return "again" if first and choice == "back" else choice
