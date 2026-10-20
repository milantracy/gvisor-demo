"""Shared rendering helpers. Scenes print through out() so --svg can replay them."""

import time

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

GV = "green3"     # gVisor colour
KATA = "orange3"  # Kata colour
SPARK_W = 60

console = Console()
FRAME = []  # what is on screen at the end of the current scene


def out(*renderables):
    console.print(*renderables)
    FRAME.extend(renderables)


def bar(value, vmax, width, color):
    """A horizontal bar with 1/8-cell resolution."""
    blocks = " ▏▎▍▌▋▊▉█"
    cells = 0 if vmax <= 0 else max(0.0, min(1.0, value / vmax)) * width
    full = int(cells)
    s = "█" * full
    if full < width:
        s += blocks[int((cells - full) * 8)]
    return Text(s.ljust(width), style=color)


def spark(values, color, width=SPARK_W):
    """A sparkline. Long series are squeezed by taking each bucket's max."""
    if not values:
        return Text("")
    if len(values) > width:
        k = len(values) / width
        values = [max(values[int(i * k):max(int((i + 1) * k), int(i * k) + 1)])
                  for i in range(width)]
    vmax = max(values) or 1
    t = Text()
    for v in values:
        if v <= 0:
            t.append("▁", style="grey30")
        else:
            t.append("▁▂▃▄▅▆▇█"[min(7, int(v / vmax * 8))], style=color)
    return t


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
