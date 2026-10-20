"""Scene 3: warm an app once, checkpoint it, then start warm copies."""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..hostmon import drop_caches
from ..runsc import make_bundle
from ..ui import (FRAME, GV, KATA, console, fmt_ms, fmt_spread, out, pause,
                  source_line, title)

# A stand-in for real warm-up: fill memory, then "initialise" for a while.
APP = r"""
echo "booting: loading {mb} MiB working set"
head -c {mb}m /dev/urandom > /tmp/cache
sleep {warm}
echo "warm: ready to serve"
n=0
while true; do n=$((n+1)); echo "requests served: $n"; sleep 0.2; done
"""


def last_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256))
            lines = f.read().decode(errors="replace").splitlines()
            return lines[-1] if lines else ""
    except OSError:
        return ""


def wait_for(path, needle, timeout=120):
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
                    APP.format(mb=args.cr_mb, warm=args.cr_warm),
                    tmpfs_mb=args.cr_mb * 2)
    logs = rs.workdir / "logs"
    logs.mkdir(exist_ok=True)
    img = rs.workdir / "image"
    img.mkdir(exist_ok=True)
    kref = ref.get("checkpoint", {})

    rows = {}  # name -> {"how", "log", "ms", "fast"}
    events = []

    def render():
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
        ev = Text("\n".join(events[-6:]), style="white")
        kata = Text.assemble(("Kata Containers: ", f"bold {KATA}"),
                             (kref.get("status", "no reference loaded"), KATA))
        return Group(t, Panel(ev, title="Timeline", border_style="grey50"),
                     kata, source_line(kref))

    def restore(name, how):
        lg = logs / f"{name}.log"
        rows[name] = {"how": how, "log": lg, "fast": True}
        t0 = time.monotonic()
        with open(lg, "w") as f:
            rs.call("restore", f"--image-path={img}", "--bundle", str(b),
                    "-detach", name, out=f)
        wait_for(lg, "requests served")
        rows[name]["ms"] = (time.monotonic() - t0) * 1000
        return rows[name]["ms"]

    with Live(render(), console=console, refresh_per_second=10) as live:
        # 1. Cold start: the app pays its full warm-up.
        log = logs / "original.log"
        rows["original"] = {"how": "cold start", "log": log}
        t0 = time.monotonic()
        with open(log, "w") as f:
            rs.call("run", "--bundle", str(b), "-detach", "original", out=f)
        wait_for(log, "requests served")
        cold = (time.monotonic() - t0) * 1000
        rows["original"]["ms"] = cold
        events.append(f"original  cold start → serving in {fmt_ms(cold)} "
                      f"(simulated warm-up: {args.cr_mb} MiB of data + "
                      f"{args.cr_warm:g} s init)")
        live.update(render())
        time.sleep(2)

        # 2. Checkpoint, then stop the original. runsc writes the image
        # through the page cache and returns before it reaches the disk.
        t0 = time.monotonic()
        rs.call("checkpoint", f"--image-path={img}", "original")
        ck = (time.monotonic() - t0) * 1000
        size = sum(p.stat().st_size for p in img.rglob("*") if p.is_file())
        events.append(f"checkpoint taken in {fmt_ms(ck)} (image {size / 2**20:.0f} "
                      f"MiB, written to page cache) · count frozen at "
                      f"'{last_line(log)}'")
        rows["original"]["how"] = "cold start → checkpointed"
        rs.destroy("original")
        live.update(render())
        time.sleep(1.5)

        # 3. Restore one copy. By default the image is still in the page
        # cache; --cr-cold flushes it so the restore reads from disk.
        if args.cr_cold:
            events.append("flushing the image to disk and dropping the host "
                          "page cache…")
            live.update(render())
            drop_caches()
            how, where = "restore, image on disk (cold)", "from disk"
        else:
            how, where = "restore, image in page cache", "from page cache"
        r = restore("restored", how)
        events.append(f"restored  → serving in {fmt_ms(r)} {where}, counter "
                      f"continues where it stopped ({cold / r:.0f}× faster "
                      "than the simulated cold start)")
        live.update(render())
        time.sleep(2)

        # 4. A burst of clones from the same image (now in the page cache).
        names = [f"clone-{i + 1}" for i in range(args.clones)]
        t0 = time.monotonic()
        with ThreadPoolExecutor(len(names)) as ex:
            futs = [ex.submit(restore, n, "clone, image in page cache")
                    for n in names]
            while not all(f.done() for f in futs):
                live.update(render())
                time.sleep(0.05)
            clone_ms = [f.result() for f in futs]
        wall = (time.monotonic() - t0) * 1000
        events.append(f"{len(names)} clones at once → all serving in "
                      f"{fmt_ms(wall)} (each {fmt_spread(clone_ms)}), each "
                      "already warm, each an isolated sandbox")
        end = time.monotonic() + 4
        while time.monotonic() < end:
            live.update(render())
            time.sleep(0.1)

    FRAME.append(render())
    results["cr"] = {"cold_ms": cold, "checkpoint_ms": ck, "image_bytes": size,
                     "restore_ms": r, "restore_from": where,
                     "clone_ms": clone_ms,
                     "clone_median_ms": statistics.median(clone_ms),
                     "clone_wall_ms": wall, "working_set_mib": args.cr_mb}
    rs.destroy_all()
    pause(args)
