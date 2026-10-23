"""Scene 2: time from `runsc run` to the app's first output line."""

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, bar, console, fmt_ms, histogram, out,
                  main_width, percentile, source_line, title,
                  with_activity)


TIME = "bold cyan"  # times in the statistics lines
LABEL = "white"     # the words around them
STARTING = "starting"  # a sandbox launched but not ready yet
# Box colours by sandbox state. Amber, not orange: orange is Kata's colour.
BOX = {"waiting": "grey30", "starting": "yellow3", "ready": GV, "failed": "red"}


def state_of(x):
    """The state of one sandbox's entry: waiting, starting, ready, failed."""
    if x is None:
        return "waiting"
    if x is STARTING:
        return "starting"
    return "failed" if x is False else "ready"


HIST = "deep_sky_blue1"       # histogram line: not the boxes' green
HIST_FILL = "deep_sky_blue4"  # and its fill


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

    # Per sandbox: None before it starts, STARTING while it starts, then
    # its ms (a float), or False if it failed.
    runs, burst = [None] * args.runs, [None] * args.burst
    seq = []  # successful sequential times, for the statistics
    state = {"phase": "sequential"}

    def ready_line(n, wall):
        """"all N sandboxes ready in X": the time in cyan, the words in
        white. Latency details are in the histograms below."""
        return Text.assemble((f"all {n} sandboxes ready in ", f"bold {LABEL}"),
                             (fmt_ms(wall), TIME))

    def boxes(results):
        """One box per sandbox: grey before it starts, amber while it starts,
        green when ready, red if it failed. Each box is two cells wide and
        3/4 of a cell tall, which is roughly square in a terminal and leaves
        a gap between rows."""
        grid = Text()
        per_row = max(10, min(50, (main_width() - 6) // 3))
        for i, x in enumerate(results):
            grid.append("▆▆", style=BOX[state_of(x)])
            grid.append(" ")
            if (i + 1) % per_row == 0 and i + 1 < len(results):
                grid.append("\n")
        return grid

    def latency(ms):
        """A histogram of ms under a bold heading, or a wait marker."""
        head = Text(f"Latency of {len(ms)} sandboxes", style="bold white")
        if len(ms) < 2:
            return [Text(""), head, Text("…", style="grey50")]
        hw = max(10, main_width() - 4 - 6)
        return [Text(""), head] + histogram(
            ms, hw, 4, HIST, [("p50", percentile(ms, 50)),
                              ("p95", percentile(ms, 95))], fill=HIST_FILL,
            ends_style=TIME, mark_style=f"bold {LABEL}", mark_time_style=TIME)

    def body():
        kata_ms = kata_startup_ms(kref)

        # What the boxes mean, once for both panels.
        legend = Text.assemble(
            ("▆▆", BOX["waiting"]), (" not started   ", "white"),
            ("▆▆", BOX["starting"]), (" started   ", "white"),
            ("▆▆", BOX["ready"]), (" ready   ", "white"),
            ("▆▆", BOX["failed"]), (" failed", "white"))

        # One at a time: the boxes, the wall time once all are ready, then
        # the latency histogram.
        seq_parts = [boxes(runs)]
        if "seq_wall" in state:
            seq_parts.append(Text("\n").append_text(
                ready_line(len(seq), state["seq_wall"])))
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
        if seq:
            seq_parts += latency(seq)
        n_done = sum(1 for x in runs if state_of(x) in ("ready", "failed"))
        parts = [legend,
                 Panel(Group(*seq_parts),
                       title=f"One at a time: {args.runs} sandboxes in sequence",
                       border_style="white",
                       subtitle=f"{n_done}/{args.runs} ready")]
        if kata_ms:
            parts.append(source_line(kref))

        # At once: shown from the start, all grey until the burst begins.
        done = [x for x in burst if state_of(x) == "ready"]
        burst_parts = [boxes(burst)]
        if "burst_wall" in state:
            burst_parts.append(Text("\n").append_text(
                ready_line(len(done), state["burst_wall"])))
        if state["phase"] == "burst":
            if done:
                burst_parts += latency(done)
            sub = f"{len(done)}/{len(burst)} ready"
        else:
            sub = "starts when one at a time finishes"
        parts.append(Panel(Group(*burst_parts),
                           title=f"At once: {len(burst)} sandboxes concurrently",
                           border_style="white", subtitle=sub))
        return Group(*parts)

    def render():
        return with_activity(body())

    with Live(console=console, refresh_per_second=20,
              get_renderable=render) as live:
        t0 = time.monotonic()
        for i in range(args.runs):
            runs[i] = STARTING
            dt = time_to_ready(rs, b, f"s{i:04d}")
            runs[i] = dt or False
            if dt:
                seq.append(dt)
            live.update(render())
        state["seq_wall"] = (time.monotonic() - t0) * 1000
        state["phase"] = "burst"

        # Every worker waits at a starting line; when all are ready, all
        # boxes turn amber and the workers are released together. Latency is
        # still timed from each sandbox's own `runsc run`. Workers do not
        # redraw the screen: Live redraws on its own, and redraws from 200
        # threads slowed the launches down.
        go = threading.Barrier(args.burst + 1)

        def one(i):
            go.wait()
            burst[i] = time_to_ready(rs, b, f"b{i:04d}") or False

        with ThreadPoolExecutor(args.burst) as ex:
            futs = [ex.submit(one, i) for i in range(args.burst)]
            for i in range(args.burst):
                burst[i] = STARTING
            t0 = time.monotonic()  # the wall clock starts at the release
            go.wait()
            for f in futs:
                f.result()
        state["burst_wall"] = (time.monotonic() - t0) * 1000
        live.update(render())

    FRAME.append(render())
    results["startup"] = {"seq_ms": seq, "burst_ms": burst,
                          "burst_wall_ms": state["burst_wall"]}
    # TODO: measure Kata on a KVM host with this same bundle and put the
    # result in kata_reference.json as startup.value_ms. Until then there is
    # no Kata startup bar.
