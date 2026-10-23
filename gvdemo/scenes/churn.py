"""Scene "Sandbox churn": how fast sandboxes are created and destroyed."""

import threading
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..colors import MUTED
from ..activity import activity
from ..hostmon import LIMIT, cpu_busy_s, cpu_capacity
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, bar, console, fmt_cpu, fmt_ms, main_width, meter,
                  out, percentile, title, with_activity)

START = "yellow3"   # create → ready, as "started" in startup latency
# The worker grid: one box per worker, coloured by what its sandbox is doing.
BOX = {"starting": START, "ready": GV, "terminating": "magenta3",
       "idle": "grey30"}
# Ready lasts ~0 ms and terminating a few ms, too short to see. So a box
# shows its sandbox's real state (amber while starting), and when the sandbox
# becomes ready it blinks green, then magenta, for BLINK_S each (about one
# frame). Measurements are unaffected.
BLINK_S = 0.05


def run(rs, ref, args, results):
    console.clear()
    out(title("⇉", "Sandbox churn",
              "How fast sandboxes are created and destroyed, as more run at "
              "once"))
    b = make_bundle(rs.workdir, "churn", "echo ready")
    levels = [int(x) for x in args.churn_levels.split(",") if x.strip()]
    ncpu = cpu_capacity()
    rows = []  # one dict per finished level
    # The level running now: its sandboxes so far, as (ready_at, create→ready
    # ms, ready→destroyed ms).
    cur = {"level": None, "t0": 0.0, "lives": [], "stop": None}
    lock = threading.Lock()
    # Per worker: its sandbox's real state, and when it last became ready.
    boxes = []

    def mark(w, **kw):
        with lock:
            boxes[w].update(kw)

    def worker_grid(W):
        """One box per worker: its sandbox's real state, with a short green
        then magenta blink each time a sandbox becomes ready."""
        now = time.monotonic()
        grid = Text()
        per_row = max(10, min(50, (W - 6) // 3))
        with lock:
            for w, b in enumerate(boxes):
                since = now - b["ready_at"]
                shown = "ready" if since < BLINK_S else \
                    "terminating" if since < 2 * BLINK_S else b["state"]
                grid.append("▆▆", style=BOX[shown])
                grid.append(" ")
                if (w + 1) % per_row == 0 and w + 1 < len(boxes):
                    grid.append("\n")
        legend = Text.assemble(
            ("▆▆", BOX["starting"]), (" starting   ", "white"),
            ("▆▆", BOX["ready"]), (" ready   ", "white"),
            ("▆▆", BOX["terminating"]), (" terminating   ", "white"),
            ("▆▆", BOX["idle"]), (" idle", "white"),
            (f"   (ready and terminating blink for {BLINK_S * 1000:.0f} ms each;"
             " they really take a few ms)", MUTED))
        n = len(boxes)
        return Panel(Group(grid if n else Text("…", style=MUTED), Text(""),
                           legend),
                     border_style="grey30", title_align="left",
                     title=f"Workers: {n}, one sandbox each at a time" if n
                     else "Workers")

    def life_stats(lives, stop=None):
        """p50s of a level's sandboxes, and its rate, from (ready_at, s, d)."""
        inside = [(s, d) for ready_at, s, d in lives
                  if stop is None or ready_at <= stop]
        if not inside:
            return None
        s = [x[0] for x in inside]
        d = [x[1] for x in inside]
        return {"n": len(inside), "start": percentile(s, 50),
                "start95": percentile(s, 95), "destroy": percentile(d, 50),
                "life": percentile([a + z for a, z in inside], 50)}

    def body():
        W = main_width()
        # Progress through the levels, and through the current one.
        head = Table.grid(padding=(0, 2), expand=True)
        head.add_column(no_wrap=True)
        head.add_column(ratio=1)
        head.add_column(no_wrap=True)
        with lock:
            lives = list(cur["lives"])
        if cur["level"] is not None:
            frac = min(1.0, (time.monotonic() - cur["t0"]) / args.churn_seconds)
            done = sum(1 for r, _, _ in lives if r <= cur["t0"] +
                       args.churn_seconds)
            head.add_row(Text(f"{cur['level']} at once", style="bold white"),
                         meter(frac, max(10, W - 50), GV),
                         Text(f"{done} created and destroyed", style="white"))
        else:
            head.add_row(Text("Finished" if rows else "Starting",
                              style="bold white"),
                         meter(1.0 if rows else 0.0, max(10, W - 50), GV),
                         Text(f"{len(rows)} / {len(levels)} levels",
                              style="white"))
        progress = Panel(head, border_style="grey30", title_align="left",
                         title=f"{args.churn_seconds:g} s per level; each worker "
                               "creates a sandbox, waits until it is destroyed, "
                               "then creates the next")

        return Group(progress, worker_grid(W), chart(W))

    def chart(W):
        """One bar per level: sandboxes created and destroyed per second."""
        bw = max(10, W - 4 - 12 - 7 - 54 - 6)  # room for the text columns
        peak = max((r["rate"] for r in rows), default=0)
        g = Table.grid(padding=(0, 2))
        g.add_column(width=12, justify="right", no_wrap=True)
        g.add_column(width=bw, no_wrap=True, overflow="crop")
        g.add_column(width=7, justify="right", no_wrap=True)
        g.add_column(no_wrap=True, style=MUTED)
        for r in rows:
            best = r["rate"] == peak
            g.add_row(Text(f"{r['level']} at once", style="white"),
                      bar(r["rate"], peak, bw, GV if best else MUTED),
                      Text(f"{r['rate']:.0f}/s", style="bold white"),
                      f"ready {fmt_ms(r['start'])} · destroyed "
                      f"+{fmt_ms(r['destroy'])} · host {fmt_cpu(r['cpu'])}")
        for lv in levels[len(rows):]:
            g.add_row(Text(f"{lv} at once", style=MUTED), Text(""),
                      Text("…", style=MUTED), "")
        lines = [g]
        if rows:
            r = max(rows, key=lambda r: r["rate"])
            lines[:0] = [Text(
                f"Peak: {r['rate']:.0f} sandboxes created and destroyed per "
                f"second with {r['level']} at once (≈ {r['rate'] * 60:,.0f} "
                "per minute)", style="bold white"), Text("")]
        return Panel(Group(*lines), border_style="grey30", title_align="left",
                     title="Sandboxes created and destroyed per second (p50 "
                           "times)",
                     subtitle=f"limited to {ncpu} CPUs" if LIMIT["cpus"] else
                     f"host has {ncpu} vCPUs", subtitle_align="right")

    def render():
        return with_activity(body())

    def level(n, idx):
        """Run n workers for args.churn_seconds; return that level's numbers."""
        seq = iter(range(10**6))
        with lock:
            cur.update(level=n, lives=[])
            boxes[:] = [{"state": "idle", "ready_at": -1.0} for _ in range(n)]
        b0 = cpu_busy_s()
        cur["t0"] = t0 = time.monotonic()
        stop = t0 + args.churn_seconds

        def worker(w):
            while time.monotonic() < stop:
                with lock:
                    k = next(seq)
                # Fixed-width IDs: no ID is a prefix of another.
                mark(w, state="starting")
                launched = time.monotonic()
                ms = time_to_ready(rs, b, f"ch{idx:02d}-{k:06d}",
                                   on_ready=lambda: mark(
                                       w, ready_at=time.monotonic()))
                # time_to_ready returns once `runsc run` has returned: the
                # app has exited and the sandbox has been destroyed.
                life = (time.monotonic() - launched) * 1000
                if ms:
                    # Count a sandbox by when it became ready.
                    with lock:
                        cur["lives"].append((launched + ms / 1000, ms,
                                             life - ms))

        threads = [threading.Thread(target=worker, args=(w,))
                   for w in range(n)]
        for t in threads:
            t.start()
        while time.monotonic() < stop:
            time.sleep(0.1)
        b1 = cpu_busy_s()  # CPU over the window, not the drain after it
        for t in threads:
            t.join()  # let in-flight sandboxes finish; they don't count
        for w in range(n):
            mark(w, state="idle")
        with lock:
            st = life_stats(cur["lives"], stop)
        st = st or {"n": 0, "start": 0, "start95": 0, "destroy": 0, "life": 0}
        return {"level": n, "ready": st["n"],
                "rate": st["n"] / args.churn_seconds,
                "start": st["start"], "start95": st["start95"],
                "destroy": st["destroy"], "life": st["life"],
                "cpu": (b1 - b0) / args.churn_seconds}

    with Live(console=console, refresh_per_second=20,  # for the blinks
              get_renderable=render) as live:
        for i, n in enumerate(levels):
            activity.note(f"level {n}: {n} workers for {args.churn_seconds:g} s")
            rows.append(level(n, i))
            cur["level"] = None
            live.update(render())
            time.sleep(1)  # let the host settle before the next level
        FRAME.append(render())

    results["churn"] = {"seconds_per_level": args.churn_seconds,
                        "levels": rows}
