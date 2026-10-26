"""Scene 3: warm an app once, checkpoint it, then restore from it."""

import re
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..hostmon import drop_caches
from ..runsc import make_bundle
from ..ui import (FRAME, GV, bar, box_title, console, fmt_ms, fmt_spread, out,
                  scene_live, title, wait_to_start, with_activity)

TICK_S = 0.5  # the app counts one "request" per tick

# A Python app with a real warm-up: it imports libraries, then builds an
# in-memory index of {mb} MiB of records (CPU work, a few seconds). The
# counter n and the index live only in its memory, so a restored copy that
# continues the count, and can still look keys up, proves its memory came
# back.
APP = r"""
import time
t0 = time.monotonic()
print("booting: importing libraries", flush=True)
import argparse, asyncio, csv, decimal, email.parser, hashlib, http.server
import json, logging, random, sqlite3, ssl, statistics, unittest
import urllib.request, xml.etree.ElementTree
t1 = time.monotonic()
print("booting: building a {mb} MiB in-memory index", flush=True)
rnd = random.Random(1)
index = {{}}
for _ in range({mb} * 2**20 // 256):
    rec = rnd.randbytes(256)
    index[hashlib.sha256(rec).digest()] = rec
keys = sorted(index)
t2 = time.monotonic()
print(f"warm: ready to serve (imports {{t1 - t0:.1f}} s, "
      f"index {{t2 - t1:.1f}} s)", flush=True)
# A restored copy does not sleep out the rest of its tick: its sleep ends
# at once (measured: restores took the same time with a 0.2 s tick as with
# 10 ms polling), so the first line after a restore times the restore.
n = 0
while True:
    n += 1
    index[keys[n % len(keys)]]  # each request is an index lookup
    print(f"requests served: {{n}}", flush=True)
    time.sleep({tick})
"""


COUNT = re.compile(r"requests served: (\d+)")
BLINK_S = 0.3  # a new count flashes on a green block this long, then grey
# How long each stage runs, so the audience can follow: the original serves
# this long, then is checkpointed; the single restore serves this long, then
# the batch of restores starts.
STAGE_S = 3
# The original is checkpointed at the count it reaches in STAGE_S (it counts
# 1 at once, then one every TICK_S), so every run shows the same history.
CHECKPOINT_AT = 1 + round(STAGE_S / TICK_S)


def fit_left(counts, width):
    """The newest counts that fit in width (joined by spaces), and whether
    older ones were left out."""
    shown, used = [], 0
    for c in reversed(counts):
        if used + len(c) + (1 if shown else 0) > width:
            break
        used += len(c) + (1 if shown else 0)
        shown.append(c)
    return list(reversed(shown)), len(shown) < len(counts)


def live_output(r, width, before=(), stopped=False, flash=True):
    """Every count the sandbox has printed, oldest first, cut on the left to
    fit width. A restored copy first shows the original's counts up to the
    checkpoint (before), then ↻, then its own: the count runs on across the
    restore, which is the proof its memory came back. When a new count
    arrives it flashes on a green block for a moment. stopped ends the row with
    ■: the original, frozen by the checkpoint."""
    try:
        counts = COUNT.findall(r["log"].read_text(errors="replace"))
    except OSError:
        counts = []
    if not counts:
        return Text(last_line(r["log"]), style="grey62", no_wrap=True,
                    overflow="ellipsis")
    now = time.monotonic()
    if counts[-1] != r.get("count"):
        r["count"], r["count_at"] = counts[-1], now
    lit = flash and now - r["count_at"] < BLINK_S
    # Pieces joined by single spaces. The newest count is padded by a space
    # on each side (in place of the joining spaces), so its flash, black on
    # a green block, has a margin and the row does not shift when it ends.
    pieces = []
    if before:
        # The last few counts before the checkpoint, so ↻ stays in view.
        old, cut = fit_left(list(before), min(14, width // 3))
        if cut:
            pieces.append(("…", "grey42"))
        pieces += [(" ".join(old), "grey42"), ("↻", f"bold {GV}")]
    used = sum(len(t) + 1 for t, _ in pieces)
    room = width - used - len(counts[-1]) - 2 - (2 if stopped else 0)
    own, cut = fit_left(counts[:-1], max(0, room - 2))
    if cut:
        pieces.append(("…", "grey42"))
    if own:
        pieces.append((" ".join(own), "grey62"))
    out = Text(no_wrap=True)
    for k, (text, style) in enumerate(pieces):
        out.append(text + (" " if k < len(pieces) - 1 else ""), style=style)
    out.append(f" {counts[-1]} ",
               style=f"bold black on {GV}" if lit else "grey62")
    if stopped:
        out.append("■", style="bold red")
    return out


class History:
    """live_output() at the width the table actually gives the column, so
    the history is never cut a second time (with the table's own "…")."""

    def __init__(self, r, before, stopped=False, flash=True):
        self.r, self.before, self.stopped = r, before, stopped
        self.flash = flash

    def __rich_console__(self, console, options):
        yield live_output(self.r, options.max_width, self.before,
                          self.stopped, self.flash)


def last_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256))
            lines = f.read().decode(errors="replace").splitlines()
            return lines[-1] if lines else ""
    except OSError:
        return ""


# A number with a unit in a timeline message: 3.92 s, 115 ms, 128 MiB, 34×.
NUMBER = r"\b\d[\d,.]*\s?(?:ms|s|MiB)\b|\b\d+×"


def event_line(t, msg):
    """One timeline line: the time stamp in green, the subject (before ':')
    in bold, numbers with units in cyan, the rest in white."""
    body = Text()
    subject, sep, rest = msg.partition(": ")
    if sep:
        body.append(subject + ":", style="bold white")
        body.append(" " + rest, style="white")
    else:
        body.append(msg, style="white")
    body.highlight_regex(NUMBER, "bold cyan")  # not the time stamp
    line = Text()
    line.append(f"{t:5.1f} s  ", style=f"bold {GV}")
    return line + body


def wait_for(path, needle, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if needle in last_line(path) or (
                path.exists() and needle in path.read_text(errors="replace")):
            return True
        time.sleep(0.005)
    return False


def run(rs, ref, args, results):
    console.clear()
    out(title("Checkpoint & restore",
              "Warm an app once, checkpoint it, then restore from the "
              "snapshot"))
    b = make_bundle(rs.workdir, "cr",
                    APP.format(mb=args.cr_mb, tick=TICK_S), python=True)
    logs = rs.workdir / "logs"
    logs.mkdir(exist_ok=True)
    img = rs.workdir / "image"
    img.mkdir(exist_ok=True)

    rows = {}  # name -> {"how", "log", "ms", "t_ready"}
    events = []
    where = ["from page cache"]  # set before the restore; read by compare()
    frozen = []  # the original's counts up to the checkpoint
    finished = False  # the final frame: no flash left on a number

    def body():
        t = Table(box=box.SIMPLE_HEAVY, header_style="bold", expand=True)
        t.add_column("Sandbox", width=12)
        t.add_column("How it started", width=38)
        t.add_column("Time to serving", justify="right", width=16)
        t.add_column("Live output", ratio=1)
        for name, r in list(rows.items()):
            t.add_row(Text(name, style="bold"), r["how"],
                      Text(fmt_ms(r["ms"]) if r.get("ms") else "…",
                           style="bold cyan"),
                      History(r, frozen if name != "original" else (),
                              stopped=name == "original" and bool(frozen),
                              flash=not finished))
        # Room for 5 events from the start, so the table below doesn't move
        # down as events arrive (a run has 4, or 5 with --cr-cold).
        shown = [event_line(t, m) for t, m in events[-8:]]
        ev = Text("\n").join(shown + [Text("")] * (5 - len(shown)))
        parts = [Panel(ev, title=box_title("Timeline", "bold bright_white"),
                       title_align="left", border_style="bright_white"), t]
        cmp = compare()
        if cmp:
            parts.append(cmp)
        return Group(*parts)

    def compare():
        """Bars of this run's own numbers: cold start vs one restore vs
        many restores at once."""
        cold = rows.get("original", {}).get("ms")
        warm = rows.get("restore-one", {}).get("ms")
        if not (cold and warm):
            return None
        many = [r["ms"] for n, r in rows.items()
                if n != "restore-one" and n.startswith("restore-")
                and r.get("ms")]
        g = Table.grid(padding=(0, 1))
        g.add_column(width=32, justify="right")
        g.add_column(width=42)
        g.add_column()
        # Green labels, white bars; times in cyan, as in startup latency.
        label, time_s, words = f"bold {GV}", "bold cyan", "bright_white"
        g.add_row(Text("Python app, fresh start", style=label),
                  bar(cold, cold, 40, "bright_white"),
                  Text(fmt_ms(cold), style=time_s))
        g.add_row(Text("Python app, 1 restore", style=label),
                  bar(warm, cold, 40, "bright_white"),
                  Text.assemble((fmt_ms(warm), time_s), ("  ·  ", words),
                                (f"{cold / warm:.0f}× faster",
                                 "bold gold1")))
        if many:
            g.add_row(Text(f"Python app, {len(many)} restores at once",
                           style=label),
                      bar(statistics.median(many), cold, 40, "bright_white"),
                      Text.assemble((fmt_ms(statistics.median(many)), time_s),
                                    (f" median ({fmt_spread(many)})", words)))
        return Panel(g, title=box_title("Same app, two ways to start",
                                        "bold white"),
                     title_align="left", border_style="white")

    def render():
        return with_activity(body())

    def restore(name, how):
        lg = logs / f"{name}.log"
        rows[name] = {"how": how, "log": lg}
        t0 = time.monotonic()
        with open(lg, "w") as f:
            rs.call("restore", f"--image-path={img}", "--bundle", str(b),
                    "-detach", name, out=f)
        if not wait_for(lg, "requests served"):
            rows[name]["how"] += " (no output within 30 s)"
        rows[name]["t_ready"] = time.monotonic()
        rows[name]["ms"] = (rows[name]["t_ready"] - t0) * 1000
        return rows[name]["ms"]

    wait_to_start(render)  # everything is prepared: start on Enter
    with scene_live(render, refresh_per_second=10) as live:
        t_scene = time.monotonic()

        def ev(msg):
            """Add a timeline event, stamped with seconds since the scene began."""
            events.append((time.monotonic() - t_scene, msg))

        # 1. Cold start: the app pays its full warm-up.
        log = logs / "original.log"
        rows["original"] = {"how": "cold start", "log": log}
        activity.note(f"app warms up: Python imports its libraries, then "
                      f"builds a {args.cr_mb} MiB index")
        for attempt in (1, 2):
            t0 = time.monotonic()
            with open(log, "w") as f:
                rs.call("run", "--bundle", str(b), "-detach", "original", out=f)
            if wait_for(log, "requests served"):
                break
            # Seen once and not reproduced: the app printed nothing at all.
            # Keep its log, then retry rather than hang the demo.
            keep = rs.workdir.parent / f"failed-original-{attempt}.log"
            shutil.copy(log, keep)
            rs.destroy("original")
            if attempt == 2:
                raise RuntimeError(f"the app did not start serving twice; "
                                   f"logs in {rs.workdir.parent}")
            ev(f"original did not start serving within 30 s (log kept in "
               f"{keep}); retrying")
        cold = (time.monotonic() - t0) * 1000
        rows["original"]["ms"] = cold
        ev(f"original: cold start of a Python app → serving in "
           f"{fmt_ms(cold)}, including its warm-up")
        live.update(render())
        # Let it serve for STAGE_S, then checkpoint at the count that takes,
        # so every run shows the same history before the restore.
        wait_for(log, f"requests served: {CHECKPOINT_AT}\n")

        # 2. Checkpoint, then stop the original. runsc writes the image
        # through the page cache and returns before it reaches the disk.
        t0 = time.monotonic()
        rs.call("checkpoint", f"--image-path={img}", "original")
        ck = (time.monotonic() - t0) * 1000
        size = sum(p.stat().st_size for p in img.rglob("*") if p.is_file())
        # The counts the original printed before it was frozen: restored
        # copies show them ahead of their own.
        frozen[:] = COUNT.findall(log.read_text(errors="replace"))
        ev(f"checkpoint taken in {fmt_ms(ck)}, then the sandbox is stopped")
        rows["original"]["how"] = "cold start → checkpointed"
        # The checkpoint stops the sandbox (no --leave-running); it is
        # deleted with the others at the end of the scene.
        live.update(render())
        time.sleep(3)

        # 3. Restore one copy. By default the image is still in the page
        # cache; --cr-cold flushes it so the restore reads from disk.
        if args.cr_cold:
            ev("flushing the image to disk and dropping the host page cache")
            activity.note("sync; echo 3 > /proc/sys/vm/drop_caches")
            live.update(render())
            drop_caches()
            how, where[0] = "restored alone, image on disk (cold)", "from disk"
        else:
            how, where[0] = "restored alone", "from page cache"
        r = restore("restore-one", how)
        ev(f"1 restore: serving in {fmt_ms(r)} {where[0]} ({cold / r:.0f}× "
           "faster than the cold start)")
        live.update(render())
        time.sleep(STAGE_S)  # then the restores at once

        # 4. Many restores at once from the same image (now in the page
        # cache). Fixed-width IDs: runsc rejects an exact ID as "ambiguous"
        # when another ID starts with it (restore-1 vs restore-10), so
        # kill/delete of restore-1 would fail while restore-10 exists. For
        # the same reason the single restore is "restore-one", not "restore".
        w = len(str(args.clones))
        names = [f"restore-{i + 1:0{w}d}" for i in range(args.clones)]
        t0 = time.monotonic()
        with ThreadPoolExecutor(len(names)) as ex:
            futs = [ex.submit(restore, n,
                              f"{i} of {len(names)} restored at once")
                    for i, n in enumerate(names, 1)]
            while not all(f.done() for f in futs):
                live.update(render())
                time.sleep(0.05)
            clone_ms = [f.result() for f in futs]
        wall = (time.monotonic() - t0) * 1000
        ev(f"{len(names)} restores at once: all serving in {fmt_ms(wall)} "
           f"({fmt_spread(clone_ms)})")
        end = time.monotonic() + 6
        while time.monotonic() < end:
            live.update(render())
            time.sleep(0.1)
        finished = True
        FRAME.append(render())
        activity.note("tearing down the scene's sandboxes")
        rs.destroy_all()

    results["cr"] = {"cold_ms": cold, "checkpoint_ms": ck, "image_bytes": size,
                     "restore_ms": r, "restore_from": where[0],
                     "clone_ms": clone_ms,
                     "clone_median_ms": statistics.median(clone_ms),
                     "clone_wall_ms": wall, "working_set_mib": args.cr_mb}
