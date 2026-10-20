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
                  main_width, percentile, source_line, title,
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

    # Per sandbox: None while starting, then its ms, or False if it failed.
    runs, burst = [None] * args.runs, [None] * args.burst
    seq = []  # successful sequential times, for the statistics
    state = {"phase": "sequential"}

    def boxes(results):
        """One square per sandbox: green when ready, red if it failed."""
        grid = Text()
        per_row = max(10, min(50, (main_width() - 6) // 2))
        for i, x in enumerate(results):
            grid.append(*(("■ ", GV) if x else ("■ ", "red") if x is False
                          else ("□ ", "grey37")))
            if (i + 1) % per_row == 0 and i + 1 < len(results):
                grid.append("\n")
        return grid

    def body():
        kata_ms = kata_startup_ms(kref)

        # Sequential panel: the grid, then the statistics at the bottom (as
        # in the burst panel). They update live; the wall time comes at the end.
        seq_parts = [boxes(runs)]
        if seq:
            stats = (f"p50 {fmt_ms(percentile(seq, 50))} · "
                     f"p95 {fmt_ms(percentile(seq, 95))} · {fmt_spread(seq)}")
            if "seq_wall" in state:
                stats = (f"all {len(seq)} ready in {fmt_ms(state['seq_wall'])},"
                         f" one after another · {stats}")
            seq_parts.append(Text("\n" + stats, style=f"bold {GV}"))
        if kata_ms and seq:
            # With a measured Kata value, compare the medians as bars.
            med = statistics.median(seq)
            vmax = max(kata_ms, med)
            g = Table.grid(padding=(0, 1))
            g.add_column(width=12, justify="right")
            g.add_column(width=52)
            g.add_column()
            g.add_row(Text("gVisor", style=f"bold {GV}"), bar(med, vmax, 50, GV),
                      Text(f"{fmt_ms(med)} median", style="bold white"))
            g.add_row(Text("Kata (ref.)", style=f"bold {KATA}"),
                      bar(kata_ms, vmax, 50, KATA),
                      Text(fmt_ms(kata_ms), style="bold white"))
            seq_parts += [Text(""), g]
        elif not kata_ms:
            seq_parts.append(Text("Kata: not measured yet (see README TODO)",
                                  style="grey50"))
        n_done = sum(1 for x in runs if x is not None)
        parts = [Panel(Group(*seq_parts),
                       title=f"One at a time: {args.runs} sandboxes in sequence",
                       border_style="white",
                       subtitle=f"{n_done}/{args.runs} ready")]
        if kata_ms:
            parts.append(source_line(kref))
        if state["phase"] == "burst" or any(burst):
            done = [x for x in burst if x]
            grid = boxes(burst)
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
        t0 = time.monotonic()
        for i in range(args.runs):
            dt = time_to_ready(rs, b, f"s{i:04d}")
            runs[i] = dt or False
            if dt:
                seq.append(dt)
            live.update(render())
        state["seq_wall"] = (time.monotonic() - t0) * 1000
        state["phase"] = "burst"
        t0 = time.monotonic()

        def one(i):
            burst[i] = time_to_ready(rs, b, f"b{i:04d}") or False
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
