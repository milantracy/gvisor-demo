"""Scene 1: gVisor runs on hosts where a microVM cannot."""

import time

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..colors import MUTED
from ..hostmon import host_facts
from ..runsc import make_bundle
from ..ui import (FRAME, GV, KATA, console, fmt_ms, out, scene_live,
                  title, with_activity)


def run(rs, ref, args, results):
    console.clear()
    out(title("gVisor runs anywhere",
              "No hardware virtualization needed · "
              "gVisor runs on x86 and ARM"))
    f = host_facts()
    results["host"] = f
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    t.add_column("Host check")
    t.add_column("Result")
    t.add_row("Machine", f"{f['machine']} · {f['cpus']} vCPU · {f['mem_gib']} GiB")
    if f["limit_cpus"] or f["limit_mem_gib"]:
        t.add_row("Sandboxes limited to", " · ".join(
            x for x in (f"{f['limit_cpus']} CPUs" if f["limit_cpus"] else "",
                        f"{f['limit_mem_gib']:g} GiB" if f["limit_mem_gib"]
                        else "") if x))
    t.add_row("/dev/kvm", "[green]present[/]" if f["kvm"] else "[red]absent[/]")
    t.add_row("CPU virtualization (vmx/svm)",
              "[green]exposed[/]" if f["virt_flags"] else
              "[red]not exposed (no nested virtualization)[/]")

    can_vm = f["kvm"] and f["virt_flags"]
    verdict = Table.grid(padding=(0, 2))
    verdict.add_column(justify="right", style="bold")
    verdict.add_column()
    verdict.add_row(Text("Kata Containers", style=KATA),
                    Text("✓ KVM available" if can_vm else
                         "✗ cannot start: a microVM needs /dev/kvm",
                         style="green" if can_vm else "bold red"))
    state = {}

    def body():
        parts = [t, Panel(verdict,
                          title="[bright_white]Can this host run "
                                "sandboxes?[/]",
                          title_align="left", border_style="grey30")]
        if "res" not in state:
            parts.append(Text("Starting a gVisor sandbox on this host…",
                              style=MUTED))
        elif state["res"].returncode == 0:
            parts.append(Panel(Text(state["res"].stdout.rstrip(), style="cyan"),
                               title="[bright_white]Output from inside "
                                     "the sandbox[/]",
                               title_align="left", border_style="grey30"))
        note = ref.get("hardware", {}).get("note")
        if note:  # sources are in kata_reference.json
            parts.append(Text(f"• {note}", style="bright_white"))
        return Group(*parts)

    def render():
        return with_activity(body())

    with scene_live(render, refresh_per_second=8):
        b = make_bundle(rs.workdir, "anywhere",
                        "echo \"kernel: $(uname -sr)\"; dmesg | head -n 4")
        t0 = time.monotonic()
        res = rs.capture("run", "--bundle", str(b), "anywhere")
        dt = (time.monotonic() - t0) * 1000
        ok = res.returncode == 0
        verdict.add_row(Text("gVisor", style=GV),
                        Text(f"✓ sandbox ran and exited in {fmt_ms(dt)}" if ok
                             else f"✗ failed: {res.stderr.strip()[:80]}",
                             style="bold green" if ok else "bold red"))
        state["res"] = res
        time.sleep(0.3)
    FRAME.append(render())
