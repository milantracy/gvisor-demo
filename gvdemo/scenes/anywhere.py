"""Scene 1: gVisor runs on hosts where a microVM cannot."""

import subprocess
import time

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..hostmon import host_facts
from ..runsc import make_bundle
from ..ui import GV, KATA, console, fmt_ms, out, pause, source_line, title


def run(rs, ref, args, results):
    console.clear()
    out(title("1", "Runs where VMs cannot",
              "gVisor needs no hardware virtualization"))
    f = host_facts()
    results["host"] = f
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    t.add_column("Host check")
    t.add_column("Result")
    t.add_row("Machine", f"{f['machine']} · {f['cpus']} vCPU · {f['mem_gib']} GiB")
    t.add_row("/dev/kvm", "[green]present[/]" if f["kvm"] else "[red]absent[/]")
    t.add_row("CPU virtualization (vmx/svm)",
              "[green]exposed[/]" if f["virt_flags"] else
              "[red]not exposed (no nested virtualization)[/]")
    out(t)

    can_vm = f["kvm"] and f["virt_flags"]
    verdict = Table.grid(padding=(0, 2))
    verdict.add_column(justify="right", style="bold")
    verdict.add_column()
    verdict.add_row(Text("Kata Containers", style=KATA),
                    Text("✓ KVM available" if can_vm else
                         "✗ cannot start: a microVM needs /dev/kvm",
                         style="green" if can_vm else "bold red"))
    with console.status("Starting a gVisor sandbox on this host…"):
        b = make_bundle(rs.workdir, "anywhere",
                        "echo \"kernel: $(uname -sr)\"; dmesg | head -n 4")
        t0 = time.monotonic()
        res = subprocess.run(rs.cmd("run", "--bundle", str(b), "anywhere"),
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True)
        dt = (time.monotonic() - t0) * 1000
    ok = res.returncode == 0
    verdict.add_row(Text("gVisor", style=GV),
                    Text(f"✓ sandbox ran and exited in {fmt_ms(dt)}" if ok else
                         f"✗ failed: {res.stderr.strip()[:80]}",
                         style="bold green" if ok else "bold red"))
    out(Panel(verdict, title="Can this host run sandboxes?",
              border_style="white"))
    if ok:
        out(Panel(Text(res.stdout.rstrip(), style="cyan"),
                  title="Output from inside the sandbox "
                        "(gVisor's own kernel answers)",
                  border_style="grey50"))
    note = ref.get("hardware", {}).get("note")
    if note:
        out(Text(note, style="grey50"))
        out(source_line(ref.get("hardware", {})))
    pause(args)
