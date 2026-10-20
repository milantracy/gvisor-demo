"""Scene 2: time from `runsc run` to the app's first output line."""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, bar, console, fmt_ms, fmt_spread, out,
                  main_width, pause, percentile, source_line, title,
                  with_activity)


def kata_startup_ms(kref):
    """The fixed Kata startup reference, or None until one is measured."""
    return kref.get("value_ms")


def run(rs, ref, args, results):
    console.clear()
    out(title("2", "Startup latency",
              "Measured from `runsc run` until the sandbox is ready: the app "
              "inside is running and has printed its first line"))
    b = make_bundle(rs.workdir, "startup", "echo ready")
    kref = ref.get("startup", {})

    seq, burst = [], [None] * args.burst
    state = {"phase": "sequential"}

    def body():
        kata_ms = kata_startup_ms(kref)
        vmax = max([kata_ms or 0] + seq + [1])  # the burst has its own panel
        g = Table.grid(padding=(0, 1))
        g.add_column(width=16, justify="right")
        g.add_column(width=52)
        g.add_column(width=22)
        if seq:
            med = statistics.median(seq)
            # A lone bar compares nothing: show the number until Kata has one.
            g.add_row(Text("gVisor", style=f"bold {GV}"),
                      bar(med, vmax, 50, GV) if kata_ms else
                      Text(f"{fmt_ms(med)} median", style="bold white"),
                      Text(f"{fmt_ms(med)} median", style=f"bold {GV}")
                      if kata_ms else "")
            g.add_row("", Text(fmt_spread(seq), style="grey50"), "")
        if kata_ms:
            g.add_row(Text("Kata (ref.)", style=f"bold {KATA}"),
                      bar(kata_ms, vmax, 50, KATA),
                      Text(f"{fmt_ms(kata_ms)}", style=f"bold {KATA}"))
        else:
            g.add_row(Text("Kata", style="grey50"), Text(
                "not measured yet (see README TODO)", style="grey50"), "")
        parts = [Panel(g, title="One sandbox at a time", border_style="white",
                       subtitle=f"{len(seq)}/{args.runs} runs")]
        if kata_ms:
            parts.append(source_line(kref))
        if state["phase"] == "burst" or any(burst):
            done = [x for x in burst if x]
            grid = Text()
            per_row = max(10, min(50, (main_width() - 6) // 2))
            for i, x in enumerate(burst):
                grid.append("■ " if x else "□ ", style=GV if x else "grey37")
                if (i + 1) % per_row == 0 and i + 1 < len(burst):
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

    def render():
        return with_activity(body())

    with Live(console=console, refresh_per_second=20,
              get_renderable=render) as live:
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
    # TODO: measure Kata on a KVM host with this same bundle and put the
    # result in kata_reference.json as startup.value_ms. Until then there is
    # no Kata startup bar.
    pause(args)
