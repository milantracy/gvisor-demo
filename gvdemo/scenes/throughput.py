"""Scene: how many sandboxes per second this host can start, by concurrency."""

import statistics
import threading
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..hostmon import LIMIT, cpu_busy_s, cpu_capacity
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, bar, console, fmt_cpu, fmt_ms, main_width, meter,
                  out, percentile, title, with_activity)


def run(rs, ref, args, results):
    console.clear()
    out(title("⇉", "Start throughput",
              "How many sandboxes per second this host starts, as more start "
              "at once"))
    b = make_bundle(rs.workdir, "throughput", "echo ready")
    levels = [int(x) for x in args.tp_levels.split(",") if x.strip()]
    ncpu = cpu_capacity()
    rows = []  # one dict per finished level
    cur = {"level": None, "t0": 0.0, "ready": 0}

    def body():
        W = main_width()
        # Progress through the levels, and through the current one.
        head = Table.grid(padding=(0, 2), expand=True)
        head.add_column(no_wrap=True)
        head.add_column(ratio=1)
        head.add_column(no_wrap=True)
        if cur["level"] is not None:
            frac = min(1.0, (time.monotonic() - cur["t0"]) / args.tp_seconds)
            head.add_row(Text(f"{cur['level']} at once", style="bold white"),
                         meter(frac, max(10, W - 50), GV),
                         Text(f"{cur['ready']} ready so far", style="white"))
        else:
            head.add_row(Text("Finished" if rows else "Starting",
                              style="bold white"),
                         meter(1.0 if rows else 0.0, max(10, W - 50), GV),
                         Text(f"{len(rows)} / {len(levels)} levels",
                              style="white"))
        progress = Panel(head, border_style="grey30", title_align="left",
                         title=f"{args.tp_seconds:g} s per level, each worker "
                               "starts its next sandbox as soon as one is ready")

        # One bar per level: sandboxes per second.
        bw = max(10, W - 70)
        peak = max((r["rate"] for r in rows), default=0)
        g = Table.grid(padding=(0, 2))
        g.add_column(width=12, justify="right", no_wrap=True)
        g.add_column(width=bw)
        g.add_column(no_wrap=True)
        g.add_column(no_wrap=True, style="grey62")
        for r in rows:
            best = r["rate"] == peak
            g.add_row(Text(f"{r['level']} at once", style="white"),
                      bar(r["rate"], peak, bw, GV if best else "grey50"),
                      Text(f"{r['rate']:.0f}/s", style="bold white"),
                      f"p50 {fmt_ms(r['p50'])} · p95 {fmt_ms(r['p95'])} · "
                      f"host {fmt_cpu(r['cpu'])}")
        for lv in levels[len(rows):]:
            g.add_row(Text(f"{lv} at once", style="grey50"), Text(""),
                      Text("…", style="grey50"), "")
        lines = [g]
        if rows:
            r = max(rows, key=lambda r: r["rate"])
            lines.insert(0, Text(
                f"Peak: {r['rate']:.0f} sandboxes per second with {r['level']} "
                f"at once (≈ {r['rate'] * 60:,.0f} per minute)",
                style="bold white"))
            lines.insert(1, Text(""))
        chart = Panel(Group(*lines), border_style="grey30", title_align="left",
                      title="Sandboxes started per second (ready, then exited)",
                      subtitle=f"limited to {ncpu} CPUs" if LIMIT["cpus"] else
                      f"host has {ncpu} vCPUs", subtitle_align="right")
        return Group(progress, chart)

    def render():
        return with_activity(body())

    def level(n, idx):
        """Run n workers for args.tp_seconds; return that level's numbers."""
        lat, lock = [], threading.Lock()
        seq = iter(range(10**6))
        cur.update(level=n, ready=0)
        b0 = cpu_busy_s()
        cur["t0"] = t0 = time.monotonic()
        stop = t0 + args.tp_seconds

        def worker():
            while time.monotonic() < stop:
                with lock:
                    k = next(seq)
                # Fixed-width IDs: no ID is a prefix of another.
                launched = time.monotonic()
                ms = time_to_ready(rs, b, f"tp{idx:02d}-{k:06d}")
                if ms:
                    # Count a start by when it became ready, not by when the
                    # sandbox exited (time_to_ready returns after the exit).
                    ready_at = launched + ms / 1000
                    with lock:
                        lat.append((ready_at, ms))
                        cur["ready"] += ready_at <= stop

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        while time.monotonic() < stop:
            time.sleep(0.1)
        b1 = cpu_busy_s()  # CPU over the window, not the drain after it
        for t in threads:
            t.join()  # let in-flight starts finish; they don't count
        inside = [ms for ready_at, ms in lat if ready_at <= stop]
        cores = (b1 - b0) / args.tp_seconds
        return {"level": n, "ready": len(inside),
                "rate": len(inside) / args.tp_seconds,
                "p50": percentile(inside, 50) if inside else 0,
                "p95": percentile(inside, 95) if inside else 0,
                "cpu": cores}

    with Live(console=console, refresh_per_second=8,
              get_renderable=render) as live:
        for i, n in enumerate(levels):
            activity.note(f"level {n}: {n} workers for {args.tp_seconds:g} s")
            rows.append(level(n, i))
            cur["level"] = None
            live.update(render())
            time.sleep(1)  # let the host settle before the next level
        FRAME.append(render())

    results["throughput"] = {"seconds_per_level": args.tp_seconds,
                             "levels": rows}
