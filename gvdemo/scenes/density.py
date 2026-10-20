"""Scene 4: pack idle sandboxes onto one host and watch what they cost."""

import os
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
from ..hostmon import Sampler, host_facts, wait_settled
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, bar, console, fmt_cpu, fmt_ms,
                  line_chart, main_width, meter, millicores, out, pause,
                  percentile, source_line, tile, title,
                  with_activity)


PHASES = {"starting": "Starting sandboxes", "all idle": "All idle",
          "tearing down": "Tearing down", "done": "Finished"}


def run(rs, ref, args, results):
    console.clear()
    out(title("4", "Density",
              "Pack idle sandboxes onto one host and watch what they cost"))
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
        series, boxes, interval, scan = sampler.snapshot()
        p = series[-1] if series else {}
        n = p.get("n", 0)
        per = lambda k: p[k] / n if n and p.get(k) is not None else 0
        dirty = per("dirty_mib")
        per_host = per("host_mib")
        el = state["launch_s"]
        rate = f"{state['n'] / el:.0f}/s" if el else "…"
        last = probes[-1][1] if probes else None
        W = main_width()
        ncpu = os.cpu_count()

        # 1. Sandboxes up and running, as progress toward the target.
        procs, threads = p.get("procs", 0), p.get("threads", 0)
        head = Table.grid(padding=(0, 2), expand=True)
        head.add_column(no_wrap=True)
        head.add_column(ratio=1)
        head.add_column(no_wrap=True)
        head.add_row(Text(PHASES[state["phase"]], style="bold white"),
                     meter(n / args.density, max(10, W - 50), GV),
                     Text(f"{n:,} / {args.density:,} running", style="bold white"))
        head.add_row("", Text(
            f"ramp {rate}, {args.parallel} at once · {procs:,} host processes, "
            f"{threads:,} threads" + (f" ({procs / n:.0f} and {threads / n:.0f} "
                                      "per sandbox)" if n else ""),
            style="grey50", no_wrap=True, overflow="ellipsis"), "")
        progress = Panel(head, border_style="grey30", title_align="left",
                         title="Sandboxes up and running")

        # 2. KPI row. Values in ink; colour only marks the latest point.
        cols = 3 if W >= 90 else 1
        tiles = [
            tile("Private memory, each", f"{dirty:.1f} MiB",
                 f"PSS {per('pss_mib'):.1f} · host {per_host:.1f} MiB"),
            tile("CPU per sandbox, now", fmt_cpu(per("cpu_cores")),
                 f"host peak {fmt_cpu(max(x['host_cpu'] for x in series) * ncpu)}"
                 f" of {ncpu}" if series else ""),
            tile("Startup latency",
                 fmt_ms(statistics.median(last)) if last else "…",
                 f"empty host: {fmt_ms(statistics.median(probes[0][1]))}"
                 if probes else ""),
        ]
        kpi = Table.grid(expand=True, padding=(0, 1))
        for _ in range(cols):
            kpi.add_column(ratio=1)
        for i in range(0, len(tiles), cols):
            kpi.add_row(*tiles[i:i + cols])

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
                                  ("  measured live", "grey50")))
        lines = [cmp]
        if kata_mib:
            cmp.add_row(Text("Kata", style="bold white"),
                        bar(kata_mib, vmax, bw, KATA),
                        Text.assemble((f"{kata_mib:g} {kref.get('unit', 'MiB')}",
                                       "bold white"), ("  published", "grey50")))
            if dirty:
                lines.insert(0, Text(f"{kata_mib / dirty:.0f}× less private "
                                     "memory per sandbox", style="bold white"))
            if per_host:
                mem_gib = results["host"]["mem_gib"]
                lines.append(Text.assemble(
                    (f"This {mem_gib} GiB host fits ≈ "
                     f"{int(mem_gib * 1024 * 0.9 / per_host):,} gVisor vs ≈ "
                     f"{int(mem_gib * 1024 * 0.9 / kata_mib):,} Kata", "white"),
                    ("  (memory only)", "grey50")))
            src = source_line(kref)
            src.no_wrap, src.overflow = True, "ellipsis"  # full in scorecard
            lines.append(src)
        headline = Panel(Group(*lines), border_style="grey30",
                         title="Memory per idle sandbox (Private_Dirty, the "
                               "Kata source's metric)", title_align="left")

        # 4. History: one panel per metric, all on the same time axis.
        t_max = max([series[-1]["t"] if series else 0] +
                    [x[2] for x in probes] + [1])
        metrics = [
            ("Host RAM used", [(x["t"], x["host_mib"]) for x in series],
             lambda v: f"{v:,.0f} MiB"),
            (f"Host CPU (cores, of {ncpu})", [(x["t"], x["host_cpu"] * ncpu)
                                              for x in series],
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
            rows = line_chart(pts, t_max, cw, 4, GV, fmt, state["batches"],
                              state["idle_t"], ylab_w=ylab_w)
            head = Text.assemble((name, "white"), ("   ", ""),
                                 (fmt(now) if pts else "…", "bold white"))
            sub = Text(f"peak {fmt(peak)}" if pts else "", style="grey50")
            return Panel(Group(*rows), border_style="grey30", title=head,
                         title_align="left", subtitle=sub,
                         subtitle_align="right", width=pw)

        charts = Table.grid(padding=(0, 1))
        charts.add_column(width=pw)
        charts.add_column(width=pw)
        panels = [chart(*m) for m in metrics]
        charts.add_row(panels[0], panels[1])
        charts.add_row(panels[2], panels[3])
        legend = Text(f"┬ batch start   ┊ all idle   sampled every "
                      f"{interval:.2f} s", style="grey50")
        over_time = Group(charts, legend)

        dist = Table(box=box.SIMPLE_HEAD, header_style="grey62",
                     pad_edge=False, expand=True, show_edge=False)
        dist.add_column("", style="grey62", no_wrap=True)
        for h in ("min", "p50", "p95", "max"):
            dist.add_column(h, justify="right", style="white", no_wrap=True)
        heaviest = ""
        for label, key in (("private", "dirty_kib"), ("PSS", "pss_kib")):
            mem = sorted((v[key] / 1024, c) for c, v in boxes.items())
            if mem:
                m = [x[0] for x in mem]
                dist.add_row(label, *(f"{v:.1f}" for v in (
                    m[0], percentile(m, 50), percentile(m, 95), m[-1])))
                if key == "dirty_kib":
                    heaviest = f"heaviest: {mem[-1][1]} ({mem[-1][0]:.1f} MiB)"
        c = sorted(v["cpu_cores"] for v in boxes.values()
                   if v["cpu_cores"] is not None)
        if c:
            dist.add_row("CPU, mcores", *(millicores(v) for v in (
                c[0], percentile(c, 50), percentile(c, 95), c[-1])))
        if boxes:
            th = sorted(v["threads"] for v in boxes.values())
            dist.add_row("threads", *(f"{v:.0f}" for v in (
                th[0], percentile(th, 50), percentile(th, 95), th[-1])))
        each = Panel(Group(dist, Text(heaviest + "   memory in MiB",
                                      style="grey50")),
                     border_style="grey30", title_align="left",
                     title=f"Each of {len(boxes)} sandboxes, measured alone")

        middle = Table.grid(expand=True, padding=(0, 1))
        middle.add_column(width=left_w)
        middle.add_column(ratio=1)
        middle.add_row(headline, each)

        parts = [progress, kpi, middle, over_time]
        if not settled:
            parts.append(Text("Host memory was still moving when the baseline "
                              "was taken: host RAM figures are less exact.",
                              style="yellow"))
        return Group(*parts)

    def render():
        return with_activity(body())

    def start(cid):
        rs.call("run", "--bundle", str(b), "-detach", cid)

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
    sampler.start()
    try:
        with Live(console=console, refresh_per_second=8,
                  get_renderable=render) as live:
            ids = iter(range(args.density))
            probe()
            live.update(render())
            with ThreadPoolExecutor(args.parallel) as ex:
                while state["n"] < args.density:
                    batch = [f"d{next(ids):05d}" for _ in
                             range(min(args.step, args.density - state["n"]))]
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
                    state["launch_s"] += time.monotonic() - t0
                    state["n"] += len(batch)
                    probe()
            state["phase"] = "all idle"
            state["idle_t"] = time.monotonic() - sampler.t0
            activity.note(f"all idle: watching for {args.idle_window:g} s")
            idle_from = len(sampler.snapshot()[0])
            end = time.monotonic() + args.idle_window
            while time.monotonic() < end:
                live.update(render())
                time.sleep(0.1)
            FRAME.append(render())
            # Stop sampling first, so the results describe the full host.
            sampler.stop()
            state["phase"] = "tearing down"
            activity.note(f"tearing down {state['n']} sandboxes")
            rs.destroy_all()
            state["phase"] = "done"
    finally:
        sampler.stop()

    series, boxes, interval, _ = sampler.snapshot()
    idle = [x["cpu_cores"] for x in series[idle_from:]
            if x["cpu_cores"] is not None] or [0.0]
    dirty = sorted(v["dirty_kib"] / 1024 for v in boxes.values())
    results["density"] = {**series[-1], "cpu_cores": statistics.mean(idle),
                          "dirty_p50_mib": percentile(dirty, 50),
                          "dirty_min_mib": dirty[0], "dirty_max_mib": dirty[-1],
                          "baseline_wait_s": waited,
                          "baseline_settled": settled,
                          "launch_s": state["launch_s"], "probes_ms": probes,
                          "sample_interval_s": interval, "series": series,
                          "per_sandbox": boxes}
    pause(args)
