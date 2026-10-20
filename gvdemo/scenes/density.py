"""Scene 4: pack idle sandboxes onto one host and watch what they cost."""

import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..hostmon import Sampler, host_facts, wait_settled
from ..runsc import make_bundle, time_to_ready
from ..ui import (FRAME, GV, KATA, SPARK_W, bar, console, fmt_ms, fmt_spread,
                  out, pause, pct, percentile, source_line, spark, title)


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
    probes = []  # (sandboxes running, [ms of each probe start])
    state = {"n": 0, "launch_s": 0.0, "phase": "starting"}

    with console.status("Waiting for host memory to settle before the "
                        "baseline…"):
        waited, settled = wait_settled()
    sampler = Sampler(rs.root, args.sample_interval)

    def render():
        series, boxes, interval, scan = sampler.snapshot()
        p = series[-1] if series else {}
        n = p.get("n", 0)
        per = lambda k: p[k] / n if n and p.get(k) is not None else 0
        dirty = per("dirty_mib")
        per_host = per("host_mib")
        el = state["launch_s"]

        stats = Table.grid(padding=(0, 3))
        for _ in range(4):
            stats.add_column(justify="center")
        big = lambda v: Text(v, style=f"bold {GV}")
        small = lambda v: Text(v, style=GV)
        stats.add_row(big(f"{n}"), big(f"{dirty:.1f} MiB"),
                      big(pct(per("cpu_cores"))),
                      big(f"{state['n'] / el:.0f}/s" if el else ""))
        stats.add_row("sandboxes running", "private memory per sandbox",
                      "CPU per sandbox, now",
                      f"ramp rate ({args.parallel} at once)")
        stats.add_row("", "", "", "")
        last = probes[-1][1] if probes else None
        stats.add_row(
            small(f"{per('pss_mib'):.1f} MiB"),
            small(f"{per_host:.1f} MiB"),
            small(f"{per('kernel_mib') * 1024:.0f} KiB"),
            small(f"{fmt_ms(statistics.median(last))} (empty host "
                  f"{fmt_ms(statistics.median(probes[0][1]))})"
                  if last else "…"))
        stats.add_row("PSS per sandbox", "host RAM per sandbox",
                      "kernel memory per sandbox", "start on this busy host")

        # Memory per sandbox, side by side, in the Kata source's metric.
        cmp = Table.grid(padding=(0, 1))
        cmp.add_column(width=16, justify="right")
        cmp.add_column(width=52)
        cmp.add_column(width=24)
        vmax = max(kata_mib or 0, dirty, 1)
        cmp.add_row(Text("gVisor", style=f"bold {GV}"),
                    bar(dirty, vmax, 50, GV),
                    Text(f"{dirty:.1f} MiB (measured)", style=GV))
        if kata_mib:
            cmp.add_row(Text("Kata (ref.)", style=f"bold {KATA}"),
                        bar(kata_mib, vmax, 50, KATA),
                        Text(f"{kata_mib:g} {kref.get('unit', 'MiB')} "
                             "(published)", style=KATA))

        # Time series over the whole scene.
        ncpu = os.cpu_count()
        cpu = [x["cpu_cores"] or 0 for x in series]
        ts = Table.grid(padding=(0, 1))
        ts.add_column(width=16, justify="right", style="grey62")
        ts.add_column(width=SPARK_W)
        ts.add_column()
        ts.add_row("sandboxes", spark([x["n"] for x in series], GV), f"{n}")
        ts.add_row("host RAM used",
                   spark([x["host_mib"] for x in series], GV),
                   f"{p.get('host_mib', 0):.0f} MiB")
        ts.add_row("host CPU",
                   spark(cpu, GV),
                   f"{pct(cpu[-1] / ncpu) if cpu else '…'} now · "
                   f"peak {pct(max(cpu) / ncpu) if cpu else '…'}")
        ts.add_row("start, probed",
                   spark([statistics.median(x[1]) for x in probes], GV),
                   fmt_spread(last) if last else "")

        # Per-sandbox distribution, from the latest sample.
        dist = Table(box=box.SIMPLE, header_style="grey62", pad_edge=False)
        dist.add_column("per sandbox", style="grey62")
        for h in ("min", "p50", "p95", "max"):
            dist.add_column(h, justify="right")
        dist.add_column("heaviest", style="grey62")
        for label, key in (("private dirty", "dirty_kib"), ("PSS", "pss_kib")):
            mem = sorted((v[key] / 1024, c) for c, v in boxes.items())
            if mem:
                m = [x[0] for x in mem]
                dist.add_row(label, *(f"{v:.1f} MiB" for v in (
                    m[0], percentile(m, 50), percentile(m, 95), m[-1])),
                    f"{mem[-1][1]} ({mem[-1][0]:.1f} MiB)")
        cpus = sorted((v["cpu_cores"], c) for c, v in boxes.items()
                      if v["cpu_cores"] is not None)
        if cpus:
            c = [x[0] for x in cpus]
            dist.add_row("CPU", *(pct(v) for v in (
                c[0], percentile(c, 50), percentile(c, 95), c[-1])),
                f"{cpus[-1][1]} ({pct(cpus[-1][0])})")
        if boxes:
            pr = sorted(v["procs"] for v in boxes.values())
            th = sorted(v["threads"] for v in boxes.values())
            dist.add_row("procs / threads", f"{pr[0]} / {th[0]}",
                         f"{percentile(pr, 50):.0f} / {percentile(th, 50):.0f}",
                         f"{percentile(pr, 95):.0f} / {percentile(th, 95):.0f}",
                         f"{pr[-1]} / {th[-1]}", "")

        parts = [Panel(Align.center(stats), border_style="white"),
                 Panel(cmp, title="Private memory of one idle sandbox "
                                  "(Private_Dirty, the Kata source's metric)",
                       border_style="white")]
        if kata_mib:
            parts.append(source_line(kref))
        parts.append(Panel(
            ts, title=f"Over time ({state['phase']})", border_style="grey50",
            subtitle=f"sampled every {interval:.2f} s · "
                     f"one scan takes {scan * 1000:.0f} ms"))
        parts.append(Panel(dist, title=f"Each sandbox measured separately "
                                       f"({len(boxes)} sandboxes)",
                           border_style="grey50"))
        if n and per_host and kata_mib:
            mem_gib = results["host"]["mem_gib"]
            fit_g = int(mem_gib * 1024 * 0.9 / per_host)
            fit_k = int(mem_gib * 1024 * 0.9 / kata_mib)
            parts.append(Text(
                f"Projected on this {mem_gib} GiB host (90% of RAM, memory "
                f"only; gVisor's host RAM vs Kata's private memory): "
                f"gVisor ≈ {fit_g:,} · Kata ≈ {fit_k:,} sandboxes",
                style="bold white"))
        if not settled:
            parts.append(Text("Host memory was still moving when the baseline "
                              "was taken: host RAM figures are less exact.",
                              style="yellow"))
        return Group(*parts)

    def start(cid):
        rs.call("run", "--bundle", str(b), "-detach", cid)

    def probe():
        """Start args.probes sandboxes one at a time, as in scene 2."""
        ms = [time_to_ready(rs, pb, f"probe{len(probes):04d}{i}")
              for i in range(args.probes)]
        ms = [x for x in ms if x]
        if ms:
            probes.append((state["n"], ms))

    results.setdefault("host", host_facts())
    sampler.start()
    try:
        with Live(render(), console=console, refresh_per_second=8) as live:
            ids = iter(range(args.density))
            probe()
            live.update(render())
            with ThreadPoolExecutor(args.parallel) as ex:
                while state["n"] < args.density:
                    batch = [f"d{next(ids):05d}" for _ in
                             range(min(args.step, args.density - state["n"]))]
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
            idle_from = len(sampler.snapshot()[0])
            end = time.monotonic() + args.idle_window
            while time.monotonic() < end:
                live.update(render())
                time.sleep(0.1)
        FRAME.append(render())
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
    with console.status(f"Tearing down {state['n']} sandboxes…"):
        rs.destroy_all()
    pause(args)
