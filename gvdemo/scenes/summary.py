"""Scorecard: gVisor results from this run next to the Kata references."""

import json
import statistics

from rich import box
from rich.table import Table
from rich.text import Text

from ..hostmon import host_facts
from ..ui import GV, KATA, console, fmt_ms, fmt_spread, out, pct, title
from .startup import kata_startup_ms


def run(rs, ref, args, results):
    console.clear()
    out(title("★", "Scorecard",
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
        k = kata_startup_ms(ref.get("startup", {}), med)
        t.add_row("Startup to first output",
                  f"{fmt_ms(med)} median ({fmt_spread(s['seq_ms'])})",
                  fmt_ms(k) if k else "—")

    c = results.get("cr")
    if c:
        short = ref.get("checkpoint", {}).get("short", "—")
        t.add_row("Checkpoint a running app",
                  f"{fmt_ms(c['checkpoint_ms'])} (n=1, to page cache)", short)
        t.add_row("Start warm from snapshot",
                  f"{fmt_ms(c['restore_ms'])} (n=1, {c['restore_from']})", short)
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
                  f"{pct(d['cpu_cores'] / d['n'])} of a core", "—")
    out(t)
    out(Text(f"runsc {rs.version()} · {h['machine']} · "
             f"{h['cpus']} vCPU · kernel {h['kernel']} · "
             "network=none · busybox workload", style="grey50"))
    for k in ("startup", "checkpoint", "density"):
        if k in ref:
            out(Text(f"Kata {k}: {ref[k].get('source', '')}", style="grey50"))
    path = rs.workdir.parent / "gvisor-demo-results.json"
    path.write_text(json.dumps(results, indent=2))
    out(Text(f"Raw results: {path}", style="grey50"))
