#!/usr/bin/env python3
"""gVisor live demo: startup latency, checkpoint/restore, and density.

Every gVisor number on screen is measured live on this host with runsc.
Kata Containers numbers are published reference figures, loaded from
kata_reference.json, and are always shown with their source.

Usage: sudo ./demo.py [all|anywhere|startup|churn|cr|density|summary] [options]
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # also works under `python3 -I`

from rich.console import Console  # noqa: E402

from gvdemo.hostmon import limit_node, remove_cgroup  # noqa: E402
from gvdemo.runsc import Runsc  # noqa: E402
from gvdemo.scenes import SCENES  # noqa: E402
from gvdemo.activity import activity  # noqa: E402
from gvdemo.ui import ACTIVITY, FRAME, ask_next, console  # noqa: E402


def parse_size(text):
    """'16G', '512M' or a byte count, to bytes; None stays None."""
    if not text:
        return None
    units = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    t = text.strip().upper().rstrip("IB")
    if t and t[-1] in units:
        return int(float(t[:-1]) * units[t[-1]])
    return int(t)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # No default list: Python 3.12 checks a list default against choices as
    # one value and rejects it. No scenes given means all of them.
    ap.add_argument("scenes", nargs="*", choices=["all", *SCENES])
    ap.add_argument("--runsc", default=shutil.which("runsc") or "runsc")
    ap.add_argument("--platform", default=None, help="runsc --platform")
    ap.add_argument("--workdir", default="/tmp/gvisor-demo/work")
    ap.add_argument("--reference", default=str(HERE / "kata_reference.json"))
    ap.add_argument("--runs", type=int, default=200, help="sequential startups")
    ap.add_argument("--burst", type=int, default=200, help="concurrent startups")
    ap.add_argument("--churn-levels", default="1,5,10,20,50,100",
                    help="sandbox churn: sandboxes starting at once, per level")
    ap.add_argument("--churn-seconds", type=float, default=5,
                    help="sandbox churn: seconds to run each level")
    ap.add_argument("--cr-mb", type=int, default=128, help="app working set")
    ap.add_argument("--cr-warm", type=float, default=3, help="app init seconds")
    ap.add_argument("--cr-cold", action="store_true",
                    help="restore from disk: drop the host page cache first")
    ap.add_argument("--clones", type=int, default=10)
    ap.add_argument("--density", type=int, default=300, help="sandboxes")
    ap.add_argument("--step", type=int, default=20, help="sandboxes per batch")
    ap.add_argument("--parallel", type=int, default=None,
                    help="density starts at the same time (default: --step, "
                         "so each batch starts at once)")
    ap.add_argument("--probes", type=int, default=3,
                    help="one-at-a-time start probes after each batch")
    ap.add_argument("--sample-interval", type=float, default=0.5,
                    help="seconds between density samples")
    ap.add_argument("--idle-window", type=float, default=5,
                    help="seconds to watch the sandboxes idle after the ramp")
    ap.add_argument("--cpus", type=int, default=16, metavar="N",
                    help="limit all sandboxes to N CPUs, like a smaller node "
                         "(default 16)")
    ap.add_argument("--memory", default="64G", metavar="SIZE",
                    help="limit all sandboxes to SIZE of memory (default 64G)")
    ap.add_argument("--no-limit", action="store_true",
                    help="no cgroup limit: sandboxes may use the whole host")
    ap.add_argument("--activity", choices=["auto", "side", "bottom", "off"],
                    default="auto",
                    help="behind-the-scenes panel: beside the scene when the "
                         "terminal is 170+ columns (auto), side, bottom, off")
    ap.add_argument("--activity-width", type=int, metavar="N",
                    help="width of the side panel (default: what is left "
                         "beyond 130 columns, 56 to 90)")
    ap.add_argument("--svg", metavar="DIR",
                    help="save each scene's final screen as DIR/<scene>.svg")
    ap.add_argument("--auto", type=float, default=0,
                    help="advance scenes after N seconds instead of Enter")
    ap.add_argument("--kiosk", action="store_true",
                    help="run the scenes in a loop, unattended, moving on "
                         "after --kiosk-delay seconds; Ctrl-C stops")
    ap.add_argument("--kiosk-delay", type=float, default=5, metavar="S",
                    help="kiosk: seconds to show each finished scene "
                         "(default 5)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("runsc needs root: run with sudo")
    # A closed terminal (SIGHUP) or a kill (SIGTERM) must still clean up
    # sandboxes and the cgroup, like Ctrl-C does; this matters for --kiosk.
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    if args.parallel is None:
        args.parallel = args.step
    if args.kiosk:
        args.auto = args.kiosk_delay
    ACTIVITY["mode"] = args.activity
    ACTIVITY["width"] = args.activity_width
    scenes = args.scenes
    if not scenes or "all" in scenes:
        scenes = list(SCENES)
    ref = json.loads(Path(args.reference).read_text()) \
        if Path(args.reference).exists() else {}
    # Every run starts by deleting all sandboxes under its work dir, so two
    # runs on one work dir would destroy each other's sandboxes. Refuse.
    lock_path = Path(args.workdir).parent / f".{Path(args.workdir).name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        sys.exit(f"another demo run (pid {lock.read().strip() or '?'}) is "
                 f"using {args.workdir}; stop it, or pass a different "
                 "--workdir")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    cg_name = None
    if not args.no_limit:
        # A limit larger than the host means the host: say so, don't fail.
        cpus = min(args.cpus, os.cpu_count()) if args.cpus else None
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        mem = parse_size(args.memory)
        mem = min(mem, total) if mem else None
        if cpus != args.cpus or (mem and mem < parse_size(args.memory)):
            console.print(f"[yellow]This host has {os.cpu_count()} CPUs and "
                          f"{total / 2**30:.0f} GiB; limiting to what it has.")
        # One group per work dir; the run lock makes it ours alone.
        cg_name = "gvisor-demo-" + hashlib.sha1(
            str(Path(args.workdir).resolve()).encode()).hexdigest()[:8]
        limit_node(cg_name, cpus, mem)
    flags = [f"--platform={args.platform}"] if args.platform else []
    rs = Runsc(args.runsc, args.workdir, flags)
    rs.cleanup()
    rs.root.mkdir(parents=True, exist_ok=True)
    results = {}
    loop = 1
    try:
        i = 0
        while i < len(scenes):
            s = scenes[i]
            FRAME.clear()
            activity.clear()
            try:
                SCENES[s](rs, ref, args, results)
            except Exception as e:
                if not args.kiosk:
                    raise
                # Unattended: show the failure, clean up, and keep going.
                console.print(f"\n[bold red]Scene {s} failed:[/] {e}")
                with console.status("Cleaning up after the failure…"):
                    rs.cleanup()
                    rs.root.mkdir(parents=True, exist_ok=True)
            if args.svg:
                rec = Console(record=True, width=console.width,
                              file=io.StringIO(), force_terminal=True)
                for r in FRAME:
                    rec.print(r)
                os.makedirs(args.svg, exist_ok=True)
                rec.save_svg(os.path.join(args.svg, f"{s}.svg"),
                             title=f"gVisor demo · {s}")
            last = i == len(scenes) - 1
            upcoming = scenes[0] if args.kiosk and last else \
                None if last else scenes[i + 1]
            choice = ask_next(args, last=last, upcoming=upcoming,
                              note=f"Kiosk · loop {loop} · " if args.kiosk
                              else "", first=i == 0)
            if choice == "quit":
                break
            if choice in ("again", "back"):
                if choice == "back":
                    i -= 1
                # Clean state: no sandboxes, images, logs or bundles left
                # from the previous attempt.
                with console.status("Cleaning up for a fresh run…"):
                    rs.cleanup()
                    rs.root.mkdir(parents=True, exist_ok=True)
                continue
            i += 1
            if args.kiosk and i == len(scenes):
                # Start the next loop from a clean state.
                with console.status("Cleaning up for the next loop…"):
                    rs.cleanup()
                    rs.root.mkdir(parents=True, exist_ok=True)
                results = {}
                loop += 1
                i = 0
    except KeyboardInterrupt:
        pass
    finally:
        with console.status("Cleaning up sandboxes…"):
            rs.cleanup()
            if cg_name:
                remove_cgroup(cg_name)


if __name__ == "__main__":
    main()
