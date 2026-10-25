"""Scorecard: gVisor results from this run next to the Kata references."""

import json
import statistics

from rich import box
from rich.table import Table
from rich.text import Text

from ..hostmon import host_facts
from ..ui import (GV, KATA, console, fmt_cpu, fmt_ms, fmt_spread, out,
                  percentile, title)
from .startup import kata_startup_ms
from ..colors import MUTED


def run(rs, ref, args, results):
    console.clear()
    out(title("Scorecard",
              "gVisor measured live on this host · "
              "Kata from published figures"))
    t = Table(box=box.ROUNDED, header_style="bold", expand=True)
    t.add_column("")
    t.add_column("gVisor (live)", style=GV, justify="center")
    t.add_column("Kata Containers (reference)", style=KATA, justify="center")
    h = results.get("host", host_facts())
    t.add_row("Runs on this VM (no nested virt)", "✓ yes",
              "✓ yes" if h["kvm"] else "✗ needs /dev/kvm")

    s = results.get("startup")
    if s and s["seq_ms"]:
        med = statistics.median(s["seq_ms"])
        k = kata_startup_ms(ref.get("startup", {}))
        t.add_row("Startup latency (until sandbox ready)",
                  f"{fmt_ms(med)} median, p95 "
                  f"{fmt_ms(percentile(s['seq_ms'], 95))} "
                  f"({fmt_spread(s['seq_ms'])})",
                  fmt_ms(k) if k else "not measured yet")

    tp = results.get("churn")
    if tp and tp["levels"]:
        r = max(tp["levels"], key=lambda r: r["rate"])
        t.add_row("Sandbox churn (peak)",
                  f"{r['rate']:.0f} created and destroyed per second with "
                  f"{r['level']} at once (ready {fmt_ms(r['start'])}, "
                  f"destroyed +{fmt_ms(r['destroy'])}, p50)", "—")

    c = results.get("cr")
    if c:
        short = ref.get("checkpoint", {}).get("short", "—")
        t.add_row(f"Cold start of the scene 3 app ({c['working_set_mib']} MiB)",
                  f"{fmt_ms(c['cold_ms'])} (n=1)", "—")
        t.add_row("Checkpoint a running app",
                  f"{fmt_ms(c['checkpoint_ms'])} (n=1, to page cache)", short)
        t.add_row("Start warm from snapshot",
                  f"{fmt_ms(c['restore_ms'])} (n=1, {c['restore_from']}; "
                  f"{c['cold_ms'] / c['restore_ms']:.0f}× faster than cold)",
                  short)
        t.add_row(f"{len(c['clone_ms'])} clones at once",
                  f"{fmt_ms(c['clone_median_ms'])} median "
                  f"({fmt_spread(c['clone_ms'])})", short)

    d = results.get("density")
    if d:
        kd = ref.get("density", {})
        t.add_row("Private memory per idle sandbox",
                  f"{d['dirty_p50_mib']:.1f} MiB median (n={d['n']}, "
                  f"{d['dirty_min_mib']:.1f}–{d['dirty_max_mib']:.1f})",
                  f"{kd['value_mib']:g} {kd.get('unit', 'MiB')}"
                  if "value_mib" in kd else "—")
        t.add_row("Host RAM per idle sandbox (incl. kernel)",
                  f"{d['host_mib'] / d['n']:.1f} MiB", "—")
        t.add_row("Idle CPU per sandbox",
                  fmt_cpu(d["cpu_cores"] / d["n"]), "—")
        if d.get("cpu_used_ms"):
            u = d["cpu_used_ms"]
            t.add_row("CPU used since start, per sandbox",
                      f"{statistics.median(u):.0f} ms median "
                      f"(n={len(u)}, {u[0]:.0f}–{u[-1]:.0f} ms)", "—")
    out(t)
    limits = " · ".join(x for x in (
        f"{h['limit_cpus']} CPUs" if h.get("limit_cpus") else "",
        f"{h['limit_mem_gib']:g} GiB" if h.get("limit_mem_gib") else "") if x)
    out(Text(f"runsc {rs.version()} · {h['machine']} · "
             f"{h['cpus']} vCPU · kernel {h['kernel']} · "
             + (f"sandboxes limited to {limits} · " if limits else "") +
             "network=none · busybox workload", style=MUTED))
    for k in ("startup", "checkpoint", "density"):
        if "source" in ref.get(k, {}):
            out(Text(f"Kata {k}: {ref[k].get('source', '')}", style=MUTED))
    path = rs.workdir.parent / "gvisor-demo-results.json"
    path.write_text(json.dumps(results, indent=2))
    out(Text(f"Raw results: {path}", style=MUTED))
