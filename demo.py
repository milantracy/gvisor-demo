#!/usr/bin/env python3
"""gVisor live demo: startup latency, checkpoint/restore, and density.

Every gVisor number on screen is measured live on this host with runsc.
Kata Containers numbers are published reference figures, loaded from
kata_reference.json, and are always shown with their source.

Usage: sudo ./demo.py [all|intro|anywhere|systemd|startup|churn|cr|density|summary] [options]
"""

import argparse
import io
import json
import os
import shutil
import signal
import platform
import termios
import uuid
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # also works under `python3 -I`

from gvdemo.options import configure, parse_size, prerequisites  # noqa: E402
from gvdemo.session import ResultFile, cleanup_session, create_workdir  # noqa: E402

SCENE_NAMES = ('intro', 'anywhere', 'systemd', 'startup', 'churn', 'cr', 'density', 'summary')


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenes", nargs="*", metavar="SCENE")
    ap.add_argument("--profile", choices=["quick", "full"], default="quick",
                    help="quick (default): small local demo; full: all presentation scenes")
    ap.add_argument("--check", action="store_true", help="check requirements without creating sandboxes")
    ap.add_argument("--output", metavar="PATH", help="new results JSON file (never overwrites a file)")
    ap.add_argument("--runsc", default=shutil.which("runsc") or "runsc")
    ap.add_argument("--platform", default=None, help="runsc --platform")
    ap.add_argument("--workdir", default=None, help="new private work directory; defaults to a unique directory in /tmp")
    ap.add_argument("--reference", default=str(HERE / "kata_reference.json"))
    ap.add_argument("--runs", type=int, default=None, help="sequential startups")
    ap.add_argument("--burst", type=int, default=None, help="concurrent startups")
    ap.add_argument("--churn-levels", default="1,10,20,50,100",
                    help="sandbox churn: sandboxes starting at once, per level")
    ap.add_argument("--churn-seconds", type=float, default=5,
                    help="sandbox churn: seconds to run each level")
    ap.add_argument("--cr-mb", type=int, default=None,
                    help="checkpoint/restore: size of the app's in-memory "
                         "index, MiB (sets its warm-up time)")
    ap.add_argument("--cr-cold", action="store_true",
                    help="restore from disk: drop the host page cache first")
    ap.add_argument("--restores", "--clones", dest="clones", type=int,
                    default=None, help="checkpoint/restore: copies restored "
                                     "at once from one image")
    ap.add_argument("--density", type=int, default=5000,
                    help="density: sandboxes to start")
    ap.add_argument("--density-mem", type=float, default=None, metavar="PCT",
                    help="density: stop once memory use reaches PCT%% of "
                         "the memory limit (or of host RAM with --no-limit), "
                         "and say so on screen; without it the ramp fills "
                         "the memory, stopping just short of the limit")
    # Measured at 1,500 sandboxes: batches of 100 with 1 probe ramp in 34 s,
    # batches of 20 with 3 probes in 46 s; 32 workers instead of 20 changed
    # little, as starts queue on a host-wide kernel lock (pivot_root).
    ap.add_argument("--step", type=int, default=100,
                    help="density: sandboxes per batch")
    ap.add_argument("--parallel", type=int, default=32,
                    help="density: sandboxes starting at the same time")
    ap.add_argument("--probes", type=int, default=1,
                    help="density: one-at-a-time start probes after each "
                         "batch")
    ap.add_argument("--sample-interval", type=float, default=0.5,
                    help="seconds between density samples")
    ap.add_argument("--idle-window", type=float, default=5,
                    help="seconds to watch the sandboxes idle after the ramp")
    ap.add_argument("--cpus", type=int, default=None, metavar="N",
                    help="limit all sandboxes to N CPUs, like a smaller node "
                         "(profile default)")
    ap.add_argument("--memory", default=None, metavar="SIZE",
                    help="limit all sandboxes to SIZE of memory (profile default)")
    ap.add_argument("--no-limit", action="store_true",
                    help="no cgroup limit: sandboxes may use the whole host")
    ap.add_argument("--title-size", choices=["large", "normal"],
                    default="large",
                    help="large: double-size scene titles (macOS Terminal.app, "
                         "xterm, Windows Terminal, Konsole); use normal in "
                         "terminals without them, such as iTerm2")
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
    ap.add_argument("--kiosk-delay", type=float, default=7, metavar="S",
                    help="kiosk: seconds to show each finished scene "
                         "(default 7)")
    return configure(ap, ap.parse_args(argv), SCENE_NAMES)


def main(argv=None):
    args = parse_args(argv)
    errors = prerequisites(args)
    if errors:
        sys.exit("Requirements not met:\n- " + "\n- ".join(errors))
    if args.check:
        print(f"Requirements satisfied for {args.profile}: {', '.join(args.scenes)}")
        return

    # Import rendering only after reporting missing Python dependencies.
    from rich.console import Console
    from gvdemo.activity import activity
    from gvdemo.hostmon import limit_node, remove_cgroup
    from gvdemo.runsc import Runsc
    from gvdemo.scenes import SCENES
    from gvdemo.ui import (ACTIVITY, FRAME, HEADER, START, TITLE, SceneNav,
                           ask_next, console, watch_resize)

    watch_resize()  # redraw cleanly when the terminal is resized or zoomed
    if args.kiosk:
        args.auto = args.kiosk_delay
    START["wait"] = not args.auto
    ACTIVITY["mode"] = args.activity
    TITLE["large"] = args.title_size == "large"
    ACTIVITY["width"] = args.activity_width
    scenes = args.scenes
    ref = json.loads(Path(args.reference).read_text()) \
        if Path(args.reference).exists() else {}
    output = ResultFile(args.output)
    args.output = str(output.path)
    rs = None
    workdir = None
    cg_name = None
    terminal = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None
    results = {"execution": {"profile": args.profile, "scenes": scenes,
                             "machine": platform.machine(), "status": "running",
                             "platform": args.platform or "default"}}
    loop = 1

    def stop(signum, frame):
        if rs is not None:
            rs.cancel_pending()
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, stop) for sig in
                (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        workdir = create_workdir(args.workdir)
        args.workdir = str(workdir)
        if not args.no_limit:
            cpus = min(args.cpus, len(os.sched_getaffinity(0)))
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            mem = min(parse_size(args.memory), total)
            cg_name = "gvisor-demo-" + uuid.uuid4().hex
            limit_node(cg_name, cpus, mem)
            results["execution"].update(cpus=cpus, memory_bytes=mem, cgroup=cg_name)
        flags = [f"--platform={args.platform}"] if args.platform else []
        rs = Runsc(args.runsc, args.workdir, flags)
        results["execution"]["runsc_version"] = rs.version()
        output.save(results)
        i = 0
        while i < len(scenes):
            s = scenes[i]
            FRAME.clear()
            HEADER.clear()
            activity.clear()
            nav = None  # set when a scene is left at its start prompt
            try:
                SCENES[s](rs, ref, args, results)
                output.save(results)
            except SceneNav as e:
                nav = e.choice
            except Exception as e:
                if not args.kiosk:
                    raise
                # Unattended: show the failure, clean up, and keep going.
                console.print(f"\n[bold red]Scene {s} failed:[/] {e}")
                metadata = results["execution"]
                metadata["scene_error_count"] = metadata.get("scene_error_count", 0) + 1
                metadata["scene_errors"] = (metadata.get("scene_errors", []) +
                                            [{"scene": s, "error": str(e)}])[-100:]
                output.save(results)
                with console.status("Cleaning up after the failure…"):
                    rs.cleanup()
                    rs.root.mkdir(parents=True, exist_ok=True)
            if args.svg and nav is None:
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
            if nav is None:
                choice = ask_next(args, last=last, upcoming=upcoming,
                                  note=f"Kiosk · loop {loop} · " if args.kiosk
                                  else "", first=i == 0)
            else:
                # The first scene has no previous one: show it again.
                choice = "again" if nav == "back" and i == 0 else nav
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
                output.save(results)
                results = {"execution": dict(results["execution"], loop=loop + 1)}
                loop += 1
                i = 0
        results["execution"]["status"] = ("failed" if results["execution"].get("scene_error_count")
                                             else "completed")
    except KeyboardInterrupt:
        results["execution"]["status"] = "interrupted"
    except BaseException as error:
        results["execution"].update(status="failed", error=str(error))
        raise
    finally:
        # Do not let a second termination signal interrupt resource teardown.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            cleanup_session(rs, cg_name, workdir, remove_cgroup)
        except Exception as error:
            results["execution"].update(status="failed", cleanup_error=str(error))
            raise
        finally:
            try:
                output.save(results)
                print(f"Raw results: {output.path}")
            finally:
                output.close()
                if terminal is not None:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, terminal)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    main()
