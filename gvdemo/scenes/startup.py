"""Scene 2: time from `runsc run` to the app's first output line."""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, bar, console, fmt_ms, fmt_spread, out,
                  pause, percentile, source_line, title)


def kata_startup_ms(kref, gvisor_ms):
    """The Kata startup reference: absolute, or a published gap to gVisor."""
    if "value_ms" in kref:
        return kref["value_ms"]
    if "delta_ms" in kref and gvisor_ms:
        return gvisor_ms + kref["delta_ms"]
    return None


def run(rs, ref, args, results):
    console.clear()
    out(title("2", "Startup latency",
              "Time from `run` until the app prints its first line"))
    b = make_bundle(rs.workdir, "startup", "echo ready")
    kref = ref.get("startup", {})

    seq, burst = [], [None] * args.burst
    state = {"phase": "sequential"}

    def render():
        kata_ms = kata_startup_ms(kref, statistics.median(seq) if seq else 0)
        vmax = max([kata_ms or 0] + seq + [x for x in burst if x] + [1])
        g = Table.grid(padding=(0, 1))
        g.add_column(width=16, justify="right")
        g.add_column(width=52)
        g.add_column(width=22)
        if seq:
            med = statistics.median(seq)
            g.add_row(Text("gVisor", style=f"bold {GV}"), bar(med, vmax, 50, GV),
                      Text(f"{fmt_ms(med)} median", style=f"bold {GV}"))
            g.add_row("", Text(fmt_spread(seq), style="grey50"), "")
        if kata_ms:
            g.add_row(Text("Kata (ref.)", style=f"bold {KATA}"),
                      bar(kata_ms, vmax, 50, KATA),
                      Text(f"{fmt_ms(kata_ms)}", style=f"bold {KATA}"))
            if "label" in kref:
                g.add_row("", Text(kref["label"], style="grey50"), "")
        parts = [Panel(g, title="One sandbox at a time", border_style="white",
                       subtitle=f"{len(seq)}/{args.runs} runs")]
        if kata_ms:
            parts.append(source_line(kref))
        if state["phase"] == "burst" or any(burst):
            done = [x for x in burst if x]
            grid = Text()
            for i, x in enumerate(burst):
                grid.append("■ " if x else "□ ", style=GV if x else "grey37")
                if (i + 1) % 25 == 0 and i + 1 < len(burst):
                    grid.append("\n")
            summary = Text()
            if "burst_wall" in state:
                summary = Text(f"\nall {len(burst)} ready in "
                               f"{fmt_ms(state['burst_wall'])} · "
                               f"p50 {fmt_ms(percentile(done, 50))} · "
                               f"p95 {fmt_ms(percentile(done, 95))} · "
                               f"{fmt_spread(done)}",
                               style=f"bold {GV}")
            parts.append(Panel(Group(grid, summary),
                               title=f"Burst: {len(burst)} sandboxes at once",
                               border_style="white",
                               subtitle=f"{len(done)}/{len(burst)} ready"))
        return Group(*parts)

    with Live(render(), console=console, refresh_per_second=20) as live:
        for i in range(args.runs):
            dt = time_to_ready(rs, b, f"s{i:04d}")
            if dt:
                seq.append(dt)
            live.update(render())
        state["phase"] = "burst"
        t0 = time.monotonic()

        def one(i):
            burst[i] = time_to_ready(rs, b, f"b{i:04d}")
            live.update(render())

        with ThreadPoolExecutor(args.burst) as ex:
            list(ex.map(one, range(args.burst)))
        state["burst_wall"] = (time.monotonic() - t0) * 1000
        live.update(render())

    FRAME.append(render())
    results["startup"] = {"seq_ms": seq, "burst_ms": burst,
                          "burst_wall_ms": state["burst_wall"]}
    # TODO: the Kata bar and this banner rest on a 2021 published gap
    # (Kata 1.11 vs gVisor 2020). Measure current Kata on a KVM host and put
    # the result in kata_reference.json as startup.value_ms.
    if seq and "delta_ms" in kref:
        x = kref["delta_ms"] / statistics.median(seq)
        out(Align.center(Text(
            f" Kata's published extra startup time alone is {x:.0f}× "
            "gVisor's whole startup here ", style="bold white on dark_green")))
    pause(args)
