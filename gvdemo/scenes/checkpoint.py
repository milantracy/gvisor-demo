"""Scene 3: warm an app once, checkpoint it, then start warm copies."""

import re
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..hostmon import drop_caches
from ..runsc import make_bundle
from ..ui import (FRAME, GV, KATA, bar, console, fmt_ms, fmt_spread, out,
                  pause, source_line, title, with_activity)

TICK_S = 0.2  # the app counts one "request" per tick

# A stand-in for real warm-up: fill memory, then "initialise" for a while.
# The counter n lives only in the app's memory, so a restored copy that
# continues the count proves its memory came back.
APP = r"""
echo "booting: loading {mb} MiB working set"
head -c {mb}m /dev/urandom > /tmp/cache
sleep {warm}
echo "warm: ready to serve"
n=0
while true; do n=$((n+1)); echo "requests served: $n"; sleep {tick}; done
"""
COUNT = re.compile(r"requests served: (\d+)")


def last_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256))
            lines = f.read().decode(errors="replace").splitlines()
            return lines[-1] if lines else ""
    except OSError:
        return ""


def first_count(path):
    """The first counter value a sandbox printed, or None."""
    try:
        m = COUNT.search(path.read_text(errors="replace"))
    except OSError:
        return None
    return int(m.group(1)) if m else None


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
    out(title("3", "Checkpoint & restore",
              "Warm an app once, snapshot it, then start warm copies"))
    b = make_bundle(rs.workdir, "cr",
                    APP.format(mb=args.cr_mb, warm=args.cr_warm, tick=TICK_S),
                    tmpfs_mb=args.cr_mb * 2)
    logs = rs.workdir / "logs"
    logs.mkdir(exist_ok=True)
    img = rs.workdir / "image"
    img.mkdir(exist_ok=True)
    kref = ref.get("checkpoint", {})

    rows = {}  # name -> {"how", "log", "ms", "fast"}
    events = []
    where = ["from page cache"]  # set before the restore; read by compare()

    def body():
        t = Table(box=box.SIMPLE_HEAVY, header_style="bold", expand=True)
        t.add_column("Sandbox", width=12)
        t.add_column("How it started", width=34)
        t.add_column("Time to serving", justify="right", width=16)
        t.add_column("Live output", ratio=1)
        for name, r in list(rows.items()):
            t.add_row(Text(name, style="bold"), r["how"],
                      Text(fmt_ms(r["ms"]) if r.get("ms") else "…",
                           style=f"bold {GV}" if r.get("fast") else "bold"),
                      Text(last_line(r["log"]), style="cyan"))
        ev = Text("\n".join(events[-8:]), style="white")
        kata = Text.assemble(("Kata Containers: ", f"bold {KATA}"),
                             (kref.get("status", "no reference loaded"), KATA))
        parts = [t]
        cmp = compare()
        if cmp:
            parts.append(cmp)
        parts += [Panel(ev, title="Timeline", border_style="grey50"),
                  kata, source_line(kref)]
        return Group(*parts)

    def compare():
        """Bars of this run's own numbers: cold start vs restore vs clones."""
        cold = rows.get("original", {}).get("ms")
        warm = rows.get("restored", {}).get("ms")
        if not (cold and warm):
            return None
        clones = [r["ms"] for n, r in rows.items()
                  if n.startswith("clone-") and r.get("ms")]
        empty = results.get("startup", {}).get("seq_ms")
        g = Table.grid(padding=(0, 1))
        g.add_column(width=38, justify="right")
        g.add_column(width=42)
        g.add_column()
        g.add_row(Text("this app, fresh start", style="bold"),
                  bar(cold, cold, 40, "grey62"),
                  Text(fmt_ms(cold), style="bold"))
        g.add_row(Text(f"this app, restored ({where[0]})", style=f"bold {GV}"),
                  bar(warm, cold, 40, GV),
                  Text(f"{fmt_ms(warm)}  ·  {cold / warm:.0f}× faster",
                       style=f"bold {GV}"))
        if clones:
            g.add_row(Text(f"this app, {len(clones)} clones at once", style=GV),
                      bar(statistics.median(clones), cold, 40, GV),
                      Text(f"{fmt_ms(statistics.median(clones))} median "
                           f"({fmt_spread(clones)})", style=GV))
        if empty:
            g.add_row(Text("empty sandbox, startup (scene 2)", style="grey62"),
                      bar(statistics.median(empty), cold, 40, "grey50"),
                      Text(f"{fmt_ms(statistics.median(empty))} median, "
                           "no image to load", style="grey62"))
        return Panel(g, title="Same app, two ways to start (this run)",
                     border_style="white")

    def render():
        return with_activity(body())

    def restore(name, how):
        lg = logs / f"{name}.log"
        rows[name] = {"how": how, "log": lg, "fast": True}
        t0 = time.monotonic()
        with open(lg, "w") as f:
            rs.call("restore", f"--image-path={img}", "--bundle", str(b),
                    "-detach", name, out=f)
        if not wait_for(lg, "requests served"):
            rows[name]["how"] += " (no output within 30 s)"
            rows[name]["fast"] = False
        rows[name]["t_ready"] = time.monotonic()
        rows[name]["ms"] = (rows[name]["t_ready"] - t0) * 1000
        return rows[name]["ms"]

    with Live(console=console, refresh_per_second=10,
              get_renderable=render) as live:
        t_scene = time.monotonic()

        def ev(msg):
            """Add a timeline event, stamped with seconds since the scene began."""
            events.append(f"{time.monotonic() - t_scene:5.1f} s  {msg}")

        # 1. Cold start: the app pays its full warm-up.
        log = logs / "original.log"
        rows["original"] = {"how": "cold start", "log": log}
        activity.note(f"app warms up: writes {args.cr_mb} MiB to its tmpfs, "
                      f"then sleeps {args.cr_warm:g} s")
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
        ev(f"original: cold start → serving in {fmt_ms(cold)} (simulated "
           f"warm-up: {args.cr_mb} MiB of data + {args.cr_warm:g} s init)")
        live.update(render())
        time.sleep(2)

        # 2. Checkpoint, then stop the original. runsc writes the image
        # through the page cache and returns before it reaches the disk.
        t0 = time.monotonic()
        rs.call("checkpoint", f"--image-path={img}", "original")
        ck = (time.monotonic() - t0) * 1000
        size = sum(p.stat().st_size for p in img.rglob("*") if p.is_file())
        m = COUNT.search(last_line(log))
        frozen = int(m.group(1)) if m else None
        ev(f"checkpoint taken in {fmt_ms(ck)} (image {size / 2**20:.0f} MiB, "
           f"to page cache); the counter is frozen at {frozen}")
        rows["original"]["how"] = "cold start → checkpointed"
        rs.destroy("original")
        live.update(render())
        time.sleep(1.5)

        # 3. Restore one copy. By default the image is still in the page
        # cache; --cr-cold flushes it so the restore reads from disk.
        if args.cr_cold:
            ev("flushing the image to disk and dropping the host page cache")
            activity.note("sync; echo 3 > /proc/sys/vm/drop_caches")
            live.update(render())
            drop_caches()
            how, where[0] = "restore, image on disk (cold)", "from disk"
        else:
            how, where[0] = "restore, image in page cache", "from page cache"
        r = restore("restored", how)
        ev(f"restored: serving in {fmt_ms(r)} {where[0]}, resumed at "
           f"{first_count(rows['restored']['log'])} ({cold / r:.0f}× faster "
           "than the cold start)")
        live.update(render())
        time.sleep(2)

        # 4. A burst of clones from the same image (now in the page cache).
        # Fixed-width IDs: runsc rejects an exact ID as "ambiguous" when
        # another ID starts with it (clone-1 vs clone-10), so kill/delete
        # of clone-1 would fail while clone-10 exists.
        w = len(str(args.clones))
        names = [f"clone-{i + 1:0{w}d}" for i in range(args.clones)]
        t0 = time.monotonic()
        with ThreadPoolExecutor(len(names)) as ex:
            futs = [ex.submit(restore, n, "clone, image in page cache")
                    for n in names]
            while not all(f.done() for f in futs):
                live.update(render())
                time.sleep(0.05)
            clone_ms = [f.result() for f in futs]
        wall = (time.monotonic() - t0) * 1000
        resumed = sorted({first_count(rows[n]["log"]) for n in names} - {None})
        gap = statistics.median(rows[n]["t_ready"] for n in names) - \
            rows["restored"]["t_ready"]
        ev(f"{len(names)} clones at once: all serving in {fmt_ms(wall)} "
           f"({fmt_spread(clone_ms)}), each resumed at "
           f"{'/'.join(map(str, resumed))} too")
        ev(f"the clones started {gap:.1f} s after 'restored', so 'restored' "
           f"is about {gap / TICK_S:.0f} counts ahead; each copy counts on "
           "its own")
        end = time.monotonic() + 4
        while time.monotonic() < end:
            live.update(render())
            time.sleep(0.1)
        FRAME.append(render())
        activity.note("tearing down the scene's sandboxes")
        rs.destroy_all()

    results["cr"] = {"cold_ms": cold, "checkpoint_ms": ck, "image_bytes": size,
                     "restore_ms": r, "restore_from": where[0],
                     "clone_ms": clone_ms,
                     "clone_median_ms": statistics.median(clone_ms),
                     "clone_wall_ms": wall, "working_set_mib": args.cr_mb}
    pause(args)
