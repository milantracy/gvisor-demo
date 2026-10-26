"""Scene "Sandbox churn": how fast sandboxes are created and destroyed."""

import threading
import time

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..hostmon import LIMIT, cpu_busy_s, cpu_capacity
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, bar, big_number, box_title, console, fmt_ms,
                  main_width, meter, out, percentile, scene_live, title,
                  with_activity)


def workers(n):
    """A level's label: "1 worker", "20 workers at once"."""
    return "1 worker" if n == 1 else f"{n} workers at once"


def run(rs, ref, args, results):
    console.clear()
    out(title("Sandbox churn",
              "How fast sandboxes are created and destroyed, as more run at "
              "once"))
    b = make_bundle(rs.workdir, "churn", "echo ready")
    levels = [int(x) for x in args.churn_levels.split(",") if x.strip()]
    ncpu = cpu_capacity()
    scale = {}  # "bar": the rate bars' full length, once fixed
    rows = []  # one dict per finished level
    # The level running now: its sandboxes so far, as (ready_at, create→ready
    # ms, ready→destroyed ms).
    cur = {"level": None, "t0": 0.0, "lives": [], "stop": None}
    lock = threading.Lock()
    # How the scene works, in bullets at the bottom of the progress box.
    rules = Text("\n".join(f"• {x}" for x in (
        f"Each level runs for {args.churn_seconds:g} seconds",
        "Each worker creates a sandbox, waits until it is destroyed, then "
        "creates the next")), style="bright_white")

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
        secs = args.churn_seconds
        if cur["level"] is not None:
            # The level running now: its count so far, and its time so far.
            elapsed = min(secs, time.monotonic() - cur["t0"])
            frac = elapsed / secs
            n = sum(1 for r, _, _ in lives if r <= cur["t0"] + secs)
            label = Text(workers(cur["level"]), style="bold white")
            note = f"with {workers(cur['level'])}"
            step = f"level {len(rows) + 1} / {len(levels)}"
        else:
            # Between levels, the last one; when all are done, the best one.
            done = len(rows) == len(levels)
            last = None
            if rows:
                last = max(rows, key=lambda r: r["rate"]) if done else rows[-1]
            elapsed = secs if last else 0.0
            frac = 1.0 if rows else 0.0
            n = last["ready"] if last else 0
            label = Text("Finished" if done else "Starting" if not rows
                         else workers(last["level"]), style="bold white")
            note = (f"best level: {workers(last['level'])}" if done
                    else f"with {workers(last['level'])}" if last
                    else "")
            step = f"level {len(rows)} / {len(levels)}"
        when = (f"{secs:g} seconds" if elapsed >= secs
                else f"{elapsed:.1f} seconds")
        # The count in big digits, its words in three lines beside it.
        headline = Table.grid(padding=(0, 3))
        headline.add_column()
        headline.add_column(vertical="middle")
        headline.add_row(
            big_number(f"{n:,}", f"bold {GV}"),
            Text.assemble(("sandboxes created and destroyed\n",
                           "bold bright_white"),
                          ("in ", "bold bright_white"), (when, "bold cyan"),
                          "\n", (note, "white")))
        count = Text(step, style="white")
        # The bar takes what the label and the step leave (panel edges 4,
        # column gaps 4).
        room = W - 4 - 4 - label.cell_len - count.cell_len
        head.add_row(label, meter(frac, max(10, room), GV), count)
        progress = Panel(Group(headline, Text(""), head, Text(""), rules),
                         border_style="grey30")

        return Group(progress, chart(W))

    def nice_ceiling(x):
        """The bars' full length: the first 1, 2 or 5 × 10^k at or above x.
        Scaling to the peak itself would draw a lone bar full length."""
        k = 1
        while True:
            for m in (1, 2, 5):
                if m * k >= x:
                    return m * k
            k *= 10

    def bar_scale(peak, live):
        """The bars' full length, fixed once the first level is done, so
        finished bars never move. It is the most this host could reach:
        its CPUs over the CPU one sandbox costs, measured with 1 worker
        (more workers only add contention). Until then the first level's
        live numbers give it. If a level still beats it, it grows."""
        if "bar" not in scale:
            first = rows[0] if rows else live
            if not first or first["rate"] <= 0 or first["cpu"] <= 0:
                return nice_ceiling(peak)
            bound = nice_ceiling(ncpu * first["rate"] / first["cpu"])
            if not rows:
                return max(bound, nice_ceiling(peak))
            scale["bar"] = bound
        scale["bar"] = max(scale["bar"], nice_ceiling(peak))
        return scale["bar"]

    def live_row():
        """The running level's numbers so far, or None. Its rate is the
        sandboxes ready so far over the time so far, so the bar grows in real
        time and settles on the level's final rate."""
        if cur["level"] is None:
            return None
        elapsed = min(args.churn_seconds, time.monotonic() - cur["t0"])
        if elapsed < 0.5:  # too few sandboxes yet for a steady rate
            return None
        with lock:
            st = life_stats(list(cur["lives"]),
                            cur["t0"] + args.churn_seconds)
        if not st:
            return None
        return {"level": cur["level"], "rate": st["n"] / elapsed,
                "start": st["start"], "destroy": st["destroy"],
                "cpu": (cpu_busy_s() - cur["b0"]) / elapsed}

    def chart(W):
        """One bar per level: sandboxes created and destroyed per second.
        The running level's bar is updated live."""
        lw = max(len(workers(lv)) for lv in levels)
        bw = max(10, W - 4 - lw - 7 - 54 - 6)  # room for the text columns
        live = live_row()
        shown = rows + ([live] if live else [])
        peak = max((r["rate"] for r in shown), default=0)
        scale = bar_scale(peak, live)
        g = Table.grid(padding=(0, 2))
        g.add_column(width=lw, justify="right", no_wrap=True)
        g.add_column(width=bw, no_wrap=True, overflow="crop")
        g.add_column(width=7, justify="right", no_wrap=True)
        g.add_column(no_wrap=True, style="bright_white")
        for r in shown:
            best = r["rate"] == peak
            g.add_row(Text(workers(r["level"]), style="bright_white"),
                      bar(r["rate"], scale, bw,
                          GV if best else "bright_white"),
                      Text(f"{r['rate']:.0f}/s", style="bold white"),
                      Text.assemble(  # times in cyan, as in the other scenes
                          "ready ", (fmt_ms(r["start"]), "bold cyan"),
                          " · destroyed ",
                          (f"+{fmt_ms(r['destroy'])}", "bold cyan"),
                          f" · {r['cpu']:.1f} CPUs"))
        for lv in levels[len(shown):]:
            # Levels not run yet are grey; they turn white once they start.
            g.add_row(Text(workers(lv), style="grey50"), Text(""),
                      Text("…", style="grey50"), "")
        lines = [g]
        if rows:
            r = max(rows, key=lambda r: r["rate"])
            lines[:0] = [Text(
                f"Peak: {r['rate']:.0f} sandboxes created and destroyed per "
                f"second with {workers(r['level'])} (≈ {r['rate'] * 60:,.0f} "
                "per minute)", style="bold white"), Text("")]
        return Panel(Group(*lines), border_style="grey30", title_align="left",
                     title=box_title("Sandboxes created and destroyed per "
                                     "second (p50 times)"),
                     subtitle=f"limited to {ncpu} CPUs" if LIMIT["cpus"] else
                     f"host has {ncpu} vCPUs", subtitle_align="right")

    def render():
        return with_activity(body())

    def level(n, idx):
        """Run n workers for args.churn_seconds; return that level's numbers."""
        seq = iter(range(10**6))
        with lock:
            cur.update(level=n, lives=[])
        cur["b0"] = b0 = cpu_busy_s()
        cur["t0"] = t0 = time.monotonic()
        stop = t0 + args.churn_seconds

        def worker():
            while time.monotonic() < stop:
                with lock:
                    k = next(seq)
                # Fixed-width IDs: no ID is a prefix of another.
                launched = time.monotonic()
                ms = time_to_ready(rs, b, f"ch{idx:02d}-{k:06d}")
                # time_to_ready returns once `runsc run` has returned: the
                # app has exited and the sandbox has been destroyed.
                life = (time.monotonic() - launched) * 1000
                if ms:
                    # Count a sandbox by when it became ready.
                    with lock:
                        cur["lives"].append((launched + ms / 1000, ms,
                                             life - ms))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        while time.monotonic() < stop:
            time.sleep(0.1)
        b1 = cpu_busy_s()  # CPU over the window, not the drain after it
        for t in threads:
            t.join()  # let in-flight sandboxes finish; they don't count
        with lock:
            st = life_stats(cur["lives"], stop)
        st = st or {"n": 0, "start": 0, "start95": 0, "destroy": 0, "life": 0}
        return {"level": n, "ready": st["n"],
                "rate": st["n"] / args.churn_seconds,
                "start": st["start"], "start95": st["start95"],
                "destroy": st["destroy"], "life": st["life"],
                "cpu": (b1 - b0) / args.churn_seconds}

    with scene_live(render, refresh_per_second=8) as live:
        for i, n in enumerate(levels):
            activity.note(f"level {n}: {n} workers for {args.churn_seconds:g} s")
            rows.append(level(n, i))
            cur["level"] = None
            live.update(render())
            time.sleep(1)  # let the host settle before the next level
        FRAME.append(render())

    results["churn"] = {"seconds_per_level": args.churn_seconds,
                        "levels": rows}
