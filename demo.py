#!/usr/bin/env python3
"""gVisor live demo: startup latency, checkpoint/restore, and density.

Every gVisor number on screen is measured live on this host with runsc.
Kata Containers numbers are published reference figures, loaded from
kata_reference.json, and are always shown with their source.

Usage: sudo ./demo.py [all|anywhere|startup|cr|density|summary] [options]
"""

import argparse
import fcntl
import io
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # also works under `python3 -I`

from rich.console import Console  # noqa: E402

from gvdemo.runsc import Runsc  # noqa: E402
from gvdemo.scenes import SCENES  # noqa: E402
from gvdemo.activity import activity  # noqa: E402
from gvdemo.ui import ACTIVITY, FRAME, console  # noqa: E402


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
    ap.add_argument("--runs", type=int, default=50, help="sequential startups")
    ap.add_argument("--burst", type=int, default=200, help="concurrent startups")
    ap.add_argument("--cr-mb", type=int, default=128, help="app working set")
    ap.add_argument("--cr-warm", type=float, default=3, help="app init seconds")
    ap.add_argument("--cr-cold", action="store_true",
                    help="restore from disk: drop the host page cache first")
    ap.add_argument("--clones", type=int, default=10)
    ap.add_argument("--density", type=int, default=200, help="sandboxes")
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
    ap.add_argument("--activity", choices=["auto", "side", "bottom", "off"],
                    default="auto",
                    help="behind-the-scenes panel: beside the scene when the "
                         "terminal is 170+ columns (auto), side, bottom, off")
    ap.add_argument("--activity-width", type=int, metavar="N",
                    help="width of the side panel (default: what is left "
                         "beyond 120 columns, 60 to 100)")
    ap.add_argument("--svg", metavar="DIR",
                    help="save each scene's final screen as DIR/<scene>.svg")
    ap.add_argument("--auto", type=float, default=0,
                    help="advance scenes after N seconds instead of Enter")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("runsc needs root: run with sudo")
    if args.parallel is None:
        args.parallel = args.step
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
    flags = [f"--platform={args.platform}"] if args.platform else []
    rs = Runsc(args.runsc, args.workdir, flags)
    rs.cleanup()
    rs.root.mkdir(parents=True, exist_ok=True)
    results = {}
    try:
        for s in scenes:
            FRAME.clear()
            activity.clear()
            SCENES[s](rs, ref, args, results)
            if args.svg:
                rec = Console(record=True, width=console.width,
                              file=io.StringIO(), force_terminal=True)
                for r in FRAME:
                    rec.print(r)
                os.makedirs(args.svg, exist_ok=True)
                rec.save_svg(os.path.join(args.svg, f"{s}.svg"),
                             title=f"gVisor demo · {s}")
    except KeyboardInterrupt:
        pass
    finally:
        with console.status("Cleaning up sandboxes…"):
            rs.cleanup()


if __name__ == "__main__":
    main()
