"""Scene 4: pack idle sandboxes onto one host and watch what they cost."""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..colors import BOX, HIST
from ..hostmon import (LIMIT, Sampler, available_kib, cpu_capacity,
                       host_facts, memory_use, wait_settled)
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, bar, box_title, console, fmt_cpu, fmt_ms,
                  line_chart, main_width, millicores, out, percentile,
                  scene_live, title, wait_to_start, with_activity)


PHASES = {"starting": "Starting sandboxes", "all idle": "All idle",
          "tearing down": "Tearing down", "done": "Finished"}


def run(rs, ref, args, results):
    console.clear()
    out(title("Density",
              "Pack sandboxes onto one host and watch what they cost"))
    b = make_bundle(rs.workdir, "density", "exec sleep 1000000")
    kref = ref.get("density", {})
    kata_mib = kref.get("value_mib")
    # Start latency is measured by probes, one at a time, never by the ramp:
    # concurrent starts queue on a host-wide kernel lock (pivot_root walks
    # every host thread under namespace_sem), which says more about the
    # ramp's own concurrency than about starting one sandbox on a busy host.
    pb = make_bundle(rs.workdir, "probe", "echo ready")
    probes = []  # (sandboxes running, [ms of each probe start], t)
    # batches: start time of each batch, in sampler time (seconds).
    state = {"n": 0, "launch_s": 0.0, "phase": "starting", "batches": [],
             "idle_t": None}

    with console.status("Waiting for host memory to settle before the "
                        "baseline…"):
        waited, settled = wait_settled()
    activity.note(f"baseline: host memory steady after {waited:.1f} s"
                  if settled else "baseline: host memory still moving after "
                  f"{waited:.0f} s")
    # Probe sandboxes come and go between batches; they are not part of
    # the measured population.
    sampler = Sampler(rs.root, args.sample_interval,
                      include=lambda cid: not cid.startswith("probe"))

    def body():
        series, boxes, _, scan = sampler.snapshot()
        p = series[-1] if series else {}
        n = p.get("n", 0)
        per = lambda k: p[k] / n if n and p.get(k) is not None else 0
        dirty = per("dirty_mib")
        el = state["launch_s"]
        rate = f"{state['n'] / el:.0f}/s" if el else "…"
        W = main_width()
        ncpu = cpu_capacity()

        # 1. Sandboxes up and running: one small square per sandbox launched
        # so far, coloured as in startup latency, so the grid grows live.
        # Nothing shows where it will end: that is not known in advance, the
        # ramp just stops when memory use reaches the mark.
        launched = [x for x in boxes_state if x != "waiting"]
        cols = max(10, W - 4)
        grid = Text(no_wrap=True)
        for i, st in enumerate(launched):
            if i and i % cols == 0:
                grid.append("\n")
            grid.append("▪", style=BOX[st])
        legend = Text()
        for key, word in (("starting", "Starting"), ("ready", "Running"),
                          ("failed", "Failed")):
            legend.append("▪ ", style=BOX[key])
            legend.append(f"{word}   ", style="bright_white")
        legend.append(f"   ·   ramp {rate}, {args.parallel} sandbox starts at "
                      "once", style="bright_white")
        legend.no_wrap, legend.overflow = True, "ellipsis"
        head = Table.grid(expand=True)
        head.add_column()
        head.add_column(justify="right", no_wrap=True)
        used, cap = state.get("mem_now") or memory_use()
        head.add_row(Text(PHASES[state["phase"]], style="bold white"),
                     Text.assemble(
                         (f"{n:,} running", "bold white"),
                         (f"   ·   cgroup memory {used / 2**30:.1f} of "
                          f"{cap / 2**30:g} GiB, stops at "
                          f"{args.density_mem:g}%", "bright_white")))
        head = Group(head, grid, legend)
        progress = Panel(head, border_style="grey30", title_align="left",
                         title=box_title(
                             f"Sandboxes up and running: up to "
                             f"{args.density:,}, or until memory use reaches "
                             f"{args.density_mem:g}% of the "
                             f"{'limit' if LIMIT['mem_bytes'] else 'host'}"))

        # 3. The headline: memory per sandbox against the Kata reference.
        # It shares a row with the per-sandbox table.
        left_w = (W - 1) * 3 // 5
        bw = max(10, left_w - 12 - 26 - 8)
        cmp = Table.grid(padding=(0, 2))
        cmp.add_column(width=12, justify="right", no_wrap=True)
        cmp.add_column(width=bw)
        cmp.add_column(no_wrap=True)
        vmax = max(kata_mib or 0, dirty, 1)
        cmp.add_row(Text("gVisor", style="bold white"), bar(dirty, vmax, bw, GV),
                    Text.assemble((f"{dirty:.1f} MiB", "bold white"),
                                  ("  measured live", "bright_white")))
        lines = [cmp]
        if kata_mib:
            cmp.add_row(Text("Kata", style="bold white"),
                        bar(kata_mib, vmax, bw, KATA),
                        Text.assemble((f"{kata_mib:g} {kref.get('unit', 'MiB')}",
                                       "bold white"),
                                      ("  published", "bright_white")))
            if dirty:
                lines.insert(0, Text(f"{kata_mib / dirty:.0f}× less private "
                                     "memory per sandbox", style="bold gold1"))
            # How many fit at 90% of the memory allowed, from what each
            # sandbox has added to the same count the ramp stops on (the
            # cgroup's memory.current), so the two agree and other host
            # activity can't skew it. Kept from the last reading with
            # sandboxes running, since the final frame comes after teardown.
            if n and "mem0" in state and state["phase"] in ("starting",
                                                           "all idle"):
                used, cap = memory_use()
                each = (used - state["mem0"]) / n / 2**20
                if each > 0:
                    state["fits"] = (cap, int(cap / 2**20 * 0.9 / each))
                    state["each_cg"] = each
            if "fits" in state:
                cap, fits = state["fits"]
                lines.append(Text.assemble(
                    (f"This {cap / 2**30:g} GiB "
                     f"{'node' if LIMIT['mem_bytes'] else 'host'} fits ≈ ",
                     "white"),
                    (f"{fits:,} gVisor", f"bold {GV}"), (" vs ≈ ", "white"),
                    (f"{int(cap / 2**20 * 0.9 / kata_mib):,} Kata",
                     f"bold {KATA}"),
                    ("  at 90% of its memory", "bright_white")))
        # The same cost three ways, said openly: private memory (the Kata
        # source's metric); what the cgroup counts, which adds the kernel
        # memory billed to it (page tables, kernel stacks, slab); and what
        # the host lost, which adds kernel memory billed to no cgroup.
        host_each = p["host_mib"] / n if n and p.get("host_mib") else 0
        if dirty and "each_cg" in state and host_each:
            say = "bright_white"
            lines.append(Text.assemble(
                ("Each sandbox: ", say),
                (f"{dirty:.1f} MiB", "bold white"), (" private · ", say),
                (f"{state['each_cg']:.1f} MiB", "bold white"),
                (" with kernel memory (what the limit counts) · ", say),
                (f"{host_each:.1f} MiB", "bold white"),
                (" of host RAM (some kernel memory is outside the cgroup)",
                 say)))
        headline = Panel(Group(*lines), border_style="grey30",
                         title=box_title("Memory per sandbox"),
                         title_align="left")

        # 4. History: one panel per metric, all on the same time axis.
        t_max = max([series[-1]["t"] if series else 0] +
                    [x[2] for x in probes] + [1])
        metrics = [
            ("Host RAM used (whole host)",
             [(x["t"], x["host_mib"]) for x in series],
             lambda v: f"{v:,.0f} MiB"),
            (f"{'Node CPU, limited to' if LIMIT['cgroup'] else 'Host CPU, of'} "
             f"{ncpu} cores", [(x["t"], x["host_cores"]) for x in series],
             fmt_cpu),
            ("Private memory per sandbox", [(x["t"], x["dirty_mib"] / x["n"])
                                            for x in series if x["n"]],
             lambda v: f"{v:.1f} MiB"),
            ("Startup latency (probes)", [(x[2], statistics.median(x[1]))
                                          for x in probes], fmt_ms),
        ]
        pw = (W - 1) // 2

        def chart(name, pts, fmt):
            ylab_w = 10
            cw = max(10, pw - 4 - ylab_w - 1)
            now = pts[-1][1] if pts else None
            peak = max(v for _, v in pts) if pts else None
            # No batch ticks: with dozens of batches they merge into a comb.
            # Only the "all idle" mark (┊) is drawn.
            rows = line_chart(pts, t_max, cw, 3, HIST, fmt,
                              idle_t=state["idle_t"], ylab_w=ylab_w)
            head = Text.assemble((name, "bright_white"), ("   ", ""),
                                 (fmt(now) if pts else "…",
                                  "bold bright_white"))
            sub = Text(f"peak {fmt(peak)}" if pts else "",
                       style="bright_white")
            return Panel(Group(*rows), border_style="grey30", title=head,
                         title_align="left", subtitle=sub,
                         subtitle_align="right", width=pw)

        charts = Table.grid(padding=(0, 1))
        charts.add_column(width=pw)
        charts.add_column(width=pw)
        panels = [chart(*m) for m in metrics]
        charts.add_row(panels[0], panels[1])
        charts.add_row(panels[2], panels[3])

        dist = Table(box=box.SIMPLE_HEAD, header_style="bright_white",
                     pad_edge=False, expand=True, show_edge=False)
        dist.add_column("", style="bright_white", no_wrap=True)
        for h in ("min", "p50", "p95", "max"):
            dist.add_column(h, justify="right", style="white", no_wrap=True)
        for label, key in (("private, MiB", "dirty_kib"),
                           ("PSS, MiB", "pss_kib")):
            m = sorted(v[key] / 1024 for v in boxes.values())
            if m:
                dist.add_row(label, *(f"{v:.1f}" for v in (
                    m[0], percentile(m, 50), percentile(m, 95), m[-1])))
        used = sorted(v["cpu_ms"] for v in boxes.values())
        if used:
            dist.add_row("CPU used, ms", *(f"{v:.0f}" for v in (
                used[0], percentile(used, 50), percentile(used, 95),
                used[-1])))
        each = Panel(dist, border_style="grey30", title_align="left",
                     title=box_title(f"Each of {len(boxes):,} sandboxes, "
                                     "measured alone"))

        # The two boxes side by side, as tall as the taller one.
        tall = max(len(console.render_lines(p, console.options.update_width(w),
                                            pad=False))
                   for p, w in ((headline, left_w), (each, W - left_w - 1)))
        headline.height = each.height = tall
        middle = Table.grid(expand=True, padding=(0, 1))
        middle.add_column(width=left_w)
        middle.add_column(ratio=1)
        middle.add_row(headline, each)

        parts = [middle, charts, progress]
        if not settled:
            parts.append(Text("Host memory was still moving when the baseline "
                              "was taken: host RAM figures are less exact.",
                              style="yellow"))

        return Group(*parts)

    def render():
        # One square per sandbox needs the rows the panel would take below.
        return with_activity(body(), below=False)

    boxes_state = []  # per sandbox, by index: waiting/starting/ready/failed

    def start(cid):
        i = int(cid[1:])
        boxes_state[i] = "starting"
        try:
            rs.call("run", "--bundle", str(b), "-detach", cid)
        except Exception:
            boxes_state[i] = "failed"
            raise
        boxes_state[i] = "ready"

    events = []  # (seconds on the charts' clock, message), for the results

    def ev(msg):
        events.append((time.monotonic() - sampler.t0, msg))

    def host_mib():
        """Host RAM used now, read directly (the sampler may lag a tick)."""
        return (sampler.base_avail - available_kib()) / 1024

    def probe():
        """Start args.probes sandboxes one at a time, as in scene 2."""
        activity.note(f"probe: {args.probes} single starts, timed, with "
                      f"{state['n']} sandboxes running")
        # Fixed-width IDs, so no ID is a prefix of another (see checkpoint).
        ms = [time_to_ready(rs, pb, f"probe{len(probes):04d}-{i:03d}")
              for i in range(args.probes)]
        ms = [x for x in ms if x]
        if ms:
            probes.append((state["n"], ms, time.monotonic() - sampler.t0))

    results.setdefault("host", host_facts())
    # Bundles made, memory settled: start on Enter. The sampler starts after
    # it, so the charts' clock starts with the scene.
    wait_to_start(render)
    sampler.start()
    try:
        with scene_live(render, refresh_per_second=8) as live:
            ids = iter(range(args.density))
            boxes_state.extend(["waiting"] * args.density)
            probe()
            ev(f"baseline: host memory steady after {waited:.1f} s; startup "
               f"latency on the empty host "
               f"{fmt_ms(statistics.median(probes[-1][1]))}"
               if probes else f"baseline after {waited:.1f} s")
            ram = host_mib()
            live.update(render())
            # Stop at --density sandboxes, or earlier at --density-mem
            # percent of the memory allowed, whichever comes first. Before
            # each batch, the memory each sandbox has taken so far predicts
            # where the batch would end; a batch that would pass the mark is
            # cut short, so the ramp stops just under it, never near an OOM.
            state["mem0"] = used0 = memory_use()[0]
            with ThreadPoolExecutor(args.parallel) as ex:
                while state["n"] < args.density:
                    used, cap = memory_use()
                    size = min(args.step, args.density - state["n"])
                    if state["n"]:
                        each = max(1, (used - used0) / state["n"])
                        room = int((cap * args.density_mem / 100 - used) / each)
                        size = min(size, room)
                    if size < 1:
                        ev(f"memory use {used / 2**30:.1f} GiB, "
                           f"{used / cap:.0%} of {cap / 2**30:g} GiB: "
                           f"stopping at {state['n']:,} sandboxes")
                        break
                    batch = [f"d{next(ids):05d}" for _ in range(size)]
                    state["batches"].append(time.monotonic() - sampler.t0)
                    activity.note(f"batch: start {len(batch)} sandboxes, "
                                  f"{args.parallel} at a time")
                    t0 = time.monotonic()
                    futs = [ex.submit(start, c) for c in batch]
                    while not all(f.done() for f in futs):
                        live.update(render())
                        time.sleep(0.1)
                    for f in futs:
                        f.result()
                    took = time.monotonic() - t0
                    state["launch_s"] += took
                    state["n"] += len(batch)
                    probe()
                    now = host_mib()
                    ev(f"batch {len(state['batches'])}: +{len(batch)} "
                       f"sandboxes in {fmt_ms(took * 1000)} → {state['n']} "
                       f"running · host RAM {now:,.0f} MiB "
                       f"(+{now - ram:,.0f}) · startup "
                       f"{fmt_ms(statistics.median(probes[-1][1]))}")
                    ram = now
            state["phase"] = "all idle"
            state["idle_t"] = time.monotonic() - sampler.t0
            activity.note(f"all idle: watching for {args.idle_window:g} s")
            ev(f"all {state['n']} started; idle from here, watching for "
               f"{args.idle_window:g} s")
            idle_from = len(sampler.snapshot()[0])
            end = time.monotonic() + args.idle_window
            while time.monotonic() < end:
                live.update(render())
                time.sleep(0.1)
            sr = sampler.snapshot()[0]
            idle_pts = [x for x in sr[idle_from:] if x["cpu_cores"] is not None]
            n_now = sr[-1]["n"] if sr else 0
            dirty_now = sr[-1]["dirty_mib"] / n_now if n_now else 0
            host_cpu = statistics.mean(x["host_cores"] for x in idle_pts) \
                if idle_pts else 0
            box_cpu = statistics.mean(x["cpu_cores"] for x in idle_pts) / \
                max(1, n_now) if idle_pts else 0
            msg = (f"idle: host CPU {fmt_cpu(host_cpu)}, each sandbox "
                   f"{millicores(box_cpu)} millicores · {dirty_now:.1f} MiB "
                   "private each")
            if kata_mib and dirty_now:
                msg += f", {kata_mib / dirty_now:.0f}× less than Kata"
            ev(msg)
            # Stop sampling first, so the results describe the full host.
            sampler.stop()
            state["mem_now"] = memory_use()  # the final frame comes later
            state["phase"] = "tearing down"
            activity.note(f"tearing down {state['n']} sandboxes")
            t0 = time.monotonic()
            rs.destroy_all()
            ev(f"tore down {state['n']} sandboxes in "
               f"{fmt_ms((time.monotonic() - t0) * 1000)}")
            state["phase"] = "done"
            FRAME.append(render())
    finally:
        sampler.stop()

    series, boxes, interval, _ = sampler.snapshot()
    idle = [x["cpu_cores"] for x in series[idle_from:]
            if x["cpu_cores"] is not None] or [0.0]
    dirty = sorted(v["dirty_kib"] / 1024 for v in boxes.values())
    used = sorted(v["cpu_ms"] for v in boxes.values())
    results["density"] = {**series[-1], "cpu_cores": statistics.mean(idle),
                          "dirty_p50_mib": percentile(dirty, 50),
                          "dirty_min_mib": dirty[0], "dirty_max_mib": dirty[-1],
                          "cpu_used_ms": used,
                          "baseline_wait_s": waited,
                          "baseline_settled": settled,
                          "launch_s": state["launch_s"], "probes_ms": probes,
                          "timeline": events,
                          "sample_interval_s": interval, "series": series,
                          "per_sandbox": boxes}
