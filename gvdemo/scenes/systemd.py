"""Scene: systemd boots as the init process of a gVisor sandbox, then starts
Chromium, which plays a YouTube video in the terminal.

As in https://gvisor.dev/blog/2026/09/17/systemd-in-gvisor/: the blog's
Ubuntu image (images/systemd-ubuntu), runsc with --in-sandbox-cgroup=v2 and
--allow-suid, and the mounts Docker's --privileged and --tmpfs options give.
The image adds Carbonyl, a Chromium build that renders in the terminal
(images/systemd-chromium).
"""

import fcntl
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import struct
import termios
import threading
import time
import tty
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.live_render import LiveRender
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..activity import activity
from ..hostmon import proc_sample, sandbox_procs
from ..ui import (FRAME, GV, START, Large, box_title, console, fmt_ms, out,
                  read_answer, scene_live, title, wait_to_start,
                  with_activity)

IMAGES = Path(__file__).resolve().parents[2] / "images"
# Built in this order: the second is FROM the first.
BUILD = (("gvisor-demo/systemd-ubuntu", IMAGES / "systemd-ubuntu"),
         ("gvisor-demo/systemd-chromium", IMAGES / "systemd-chromium"))
IMAGE = BUILD[-1][0]
# The exported image, kept across runs (the work directory is wiped between
# scenes). Sandboxes do not change its files: runsc's default overlay keeps
# their changes in a backing file it creates in that directory, named after
# the sandbox (.gvisor.filestore.<id>).
ROOTFS = Path("/var/tmp/gvisor-demo/systemd-chromium/rootfs")
# Unique per run: two runs at once (two demos) would share the overlay's
# backing file, and the second sandbox would fail to start.
CID = f"systemd-{os.getpid()}"
HOSTNAME = "systemd-in-gvisor"
COMMAND = "dmesg | head -3; systemctl status"
TERM_LINES = 24  # of the command's output shown: up to its services
# The sandbox uses the host's network (gVisor's sentry still handles every
# socket call), so Chromium can reach YouTube; the other scenes have none.
FLAGS = ("--in-sandbox-cgroup=v2", "--allow-suid", "--network=host")
VIDEO = "https://www.youtube.com/watch?v=Aq5WXmQQooo"
CHROMIUM_S = 30  # Chromium has the terminal this long
INTRO_S = 3  # the card before it, with --auto or --kiosk (else: Enter)
BAR_ROWS = 2  # the status bar under it
# Close Chromium early: q or Ctrl-C (not passed on to it).
CLOSE_KEYS = (b"q", b"\x03")
HOLD_S = 4  # the result stays on screen this long before the prompt

# Every capability: Docker's --privileged. In gVisor these are capabilities
# of the sandbox's own kernel, not of the host.
CAPS = [f"CAP_{c}" for c in (
    "CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID "
    "SETPCAP LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN NET_RAW "
    "IPC_LOCK IPC_OWNER SYS_MODULE SYS_RAWIO SYS_CHROOT SYS_PTRACE SYS_PACCT "
    "SYS_ADMIN SYS_BOOT SYS_NICE SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG MKNOD "
    "LEASE AUDIT_WRITE AUDIT_CONTROL SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG "
    "WAKE_ALARM BLOCK_SUSPEND AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE"
).split()]


class Escapes:
    """Where a stream of terminal output is: inside an escape sequence or a
    UTF-8 character, or between them (idle), where other output can go in
    without breaking it."""

    def __init__(self):
        self.state, self.utf8 = "text", 0

    def feed(self, data):
        """Track data; return the end of its last idle point (0 if none),
        where other output can go in."""
        cut = 0
        for i, c in enumerate(data):
            self.step(c)
            if self.idle():
                cut = i + 1
        return cut

    def step(self, c):
        if self.utf8:
            self.utf8 -= 1
        elif self.state == "text":
            if c == 0x1b:
                self.state = "esc"
            elif c >= 0xf0:
                self.utf8 = 3
            elif c >= 0xe0:
                self.utf8 = 2
            elif c >= 0xc0:
                self.utf8 = 1
        elif self.state == "esc":
            self.state = {0x5b: "csi", 0x5d: "osc", 0x50: "osc"}.get(
                c, "text")
        elif self.state == "csi":
            if 0x40 <= c <= 0x7e:
                self.state = "text"
        elif self.state == "osc":  # ends with BEL or ESC \
            if c == 0x07:
                self.state = "text"
            elif c == 0x1b:
                self.state = "osc-esc"
        elif self.state == "osc-esc":
            self.state = "text"

    def idle(self):
        return self.state == "text" and not self.utf8


def status_bar(rows, cols, left, kernel):
    """The bottom BAR_ROWS rows while Chromium runs, drawn without moving its
    cursor: DECSC/DECRC save and restore the position and colours."""
    def row(text):
        return text[:cols].ljust(cols)
    top = row(f" ▶ INSIDE A gVisor SANDBOX     kernel: Linux {kernel}, "
              "gVisor's own kernel, not the host's ")
    low = row(f" Chromium (Carbonyl), started by systemd as chromium.service "
              f"· every system call goes to gVisor · q: close · "
              f"{max(0, left):.0f} s left ")
    return (f"\x1b7\x1b[{rows - 1};1H\x1b[0;1;30;102m{top}"
            f"\x1b[{rows};1H\x1b[0;1;97;48;5;22m{low}\x1b8").encode()


def intro_card(kernel):
    """Shown for INTRO_S before Chromium starts: what runs on what."""
    stack = Table.grid(padding=(0, 3))
    stack.add_column(no_wrap=True)
    stack.add_column(no_wrap=True)
    layers = (
        ("Chromium (Carbonyl)", "the app: chromium.service, a systemd unit",
         "system calls"),
        ("systemd, PID 1", "Ubuntu's init, inside the sandbox too", ""),
        ("gVisor's kernel (the Sentry)",
         f"Linux {kernel}: handles every system call", "a few, filtered"),
        ("host Linux", "Chromium never reaches it directly", None),
    )
    for name, what, down in layers:
        stack.add_row(Text(name, style=f"bold {GV}" if "gVisor" in name
                           else "bold bright_white"),
                      Text(what, style="bright_white"))
        if down is not None:
            stack.add_row(Text(f"      │ {down}", style="grey62"), "")
    return Group(Text(""), Large(Text("Chromium inside gVisor",
                                      style="bold bright_white")),
                 Text(""), Align.center(stack))


def ensure_rootfs():
    """Build the images and export the last one's root filesystem, once."""
    if (ROOTFS / "sbin" / "init").exists():
        return
    for image, context in BUILD:
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0:
            continue
        with console.status(f"Building {image} (first run only; needs "
                            "network)…"):
            subprocess.run(["docker", "build", "-q", "-t", image,
                            str(context)], check=True,
                           stdout=subprocess.DEVNULL)
    with console.status("Exporting the systemd image (first run only)…"):
        ROOTFS.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=ROOTFS.parent))
        # It becomes the sandbox's "/": mkdtemp's 0700 would keep every
        # service that runs as a user (dbus, ...) out of it.
        tmp.chmod(0o755)
        cid = subprocess.run(["docker", "create", IMAGE], check=True,
                             capture_output=True, text=True).stdout.strip()
        try:
            export = subprocess.Popen(["docker", "export", cid],
                                      stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", str(tmp)], stdin=export.stdout,
                           check=True)
            export.wait()
        finally:
            subprocess.run(["docker", "rm", cid], capture_output=True)
        shutil.rmtree(ROOTFS, ignore_errors=True)
        tmp.rename(ROOTFS)


def make_systemd_bundle(workdir):
    """An OCI bundle that boots /sbin/init from the exported image.

    The mounts are the ones Docker's default spec has (with --tmpfs /run and
    --tmpfs /home:exec, as in the blog). systemd mounts what is missing
    itself, with options gVisor does not take (a cgroup2 tree with
    nsdelegate, a tmpfs with usrquota), and stops if that fails. Chromium
    needs a larger /dev/shm than Docker's 64 MiB, and the host's DNS setup.
    There is no network namespace: the sandbox runs with --network=host.
    """
    b = Path(workdir) / "bundles" / "systemd"
    b.mkdir(parents=True, exist_ok=True)
    spec = {
        "ociVersion": "1.0.0",
        "process": {
            "args": ["/sbin/init"], "cwd": "/",
            "env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                    "/sbin:/bin", "container=docker"],
            "user": {"uid": 0, "gid": 0},
            "capabilities": {k: CAPS for k in ("bounding", "effective",
                                               "inheritable", "permitted",
                                               "ambient")},
        },
        "root": {"path": str(ROOTFS), "readonly": False},
        "hostname": HOSTNAME,
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {"destination": "/sys", "type": "sysfs", "source": "sysfs"},
            {"destination": "/sys/fs/cgroup", "type": "cgroup",
             "source": "cgroup", "options": ["nosuid", "noexec", "nodev"]},
            {"destination": "/dev/pts", "type": "devpts", "source": "devpts",
             "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666",
                         "mode=0620"]},
            {"destination": "/dev/shm", "type": "tmpfs", "source": "shm",
             "options": ["nosuid", "noexec", "nodev", "mode=1777",
                         "size=1073741824"]},
            {"destination": "/dev/mqueue", "type": "mqueue",
             "source": "mqueue", "options": ["nosuid", "noexec", "nodev"]},
            {"destination": "/run", "type": "tmpfs", "source": "tmpfs"},
            {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs"},
            {"destination": "/home", "type": "tmpfs", "source": "tmpfs",
             "options": ["exec"]},
            {"destination": "/etc/resolv.conf", "type": "bind",
             "source": "/etc/resolv.conf", "options": ["rbind", "ro"]},
        ],
        "linux": {"namespaces": [{"type": t} for t in
                                 ("pid", "mount", "ipc", "uts")]},
    }
    (b / "config.json").write_text(json.dumps(spec))
    return b


def run(rs, ref, args, results):
    console.clear()
    out(title("systemd in gVisor",
              "A full Linux init system boots inside the sandbox, with its "
              "services"))
    ensure_rootfs()
    b = make_systemd_bundle(rs.workdir)
    sd = rs.with_flags(*FLAGS)
    header = activity.header
    activity.set_header(f"runsc {' '.join(sd.flags)} <command>")
    state = {"phase": "ready", "status": {}, "terminal": [], "t0": None}

    def exec_out(*cmd):
        return sd.output("exec", CID, *cmd, log=False)

    def body():
        boot = Text()
        if state["phase"] == "ready":
            boot.append("Ready to boot /sbin/init", style="bright_white")
        elif "boot_ms" not in state:
            boot.append("Booting… ", style="bold bright_white")
            boot.append(fmt_ms((time.monotonic() - state["t0"]) * 1000),
                        style="bold cyan")
        else:
            boot.append("systemd is running ", style="bold bright_white")
            boot.append(fmt_ms(state["boot_ms"]), style="bold cyan")
            boot.append(" after runsc run", style="bright_white")
        boot.append("   ·   systemctl is-system-running polled until it "
                    "answers \"running\"", style="grey62")

        # The command run inside the sandbox, and its output as is, like a
        # terminal: gVisor's kernel answers dmesg, systemd answers status.
        term = Text(no_wrap=True, overflow="ellipsis")
        term.append(f"root@{HOSTNAME}:/# ", style=f"bold {GV}")
        term.append(COMMAND + "\n", style="bold bright_white")
        lines = state["terminal"][:TERM_LINES]
        for k, line in enumerate(lines):
            style = "cyan" if k < 3 else "bright_white"
            if line.lstrip().startswith(("State:", "Failed:")):
                style = "bold bright_white"
            term.append(line + ("\n" if k < len(lines) - 1 else ""),
                        style=style)
        if not lines:
            term.append("…", style="grey62")
        top = Panel(term, border_style="grey30", title_align="left",
                    title=box_title("Inside the sandbox"))

        parts = [boot, top]
        if "mem" in state:
            mem = Text.assemble(
                ("The whole system, systemd and its services: ",
                 "bright_white"),
                (f"{state['mem']:.1f} MiB", "bold cyan"),
                (" of private memory on the host", "bright_white"))
            if "chromium" in state:
                secs, peak = state["chromium"]
                mem.append_text(Text.assemble(
                    ("   ·   with Chromium playing YouTube for ",
                     "bright_white"), (f"{secs:.0f} s", "bold cyan"),
                    (": up to ", "bright_white"), (f"{peak:.0f} MiB",
                                                   "bold cyan")))
            parts.append(mem)
        return Group(*parts)

    def render():
        return with_activity(body())

    def private_mib():
        procs = sandbox_procs(sd.root, {}).get(CID, [])
        return sum(s[1] for s in map(proc_sample, procs) if s) / 1024

    def sample_memory():
        mib = private_mib()
        if mib:
            state["mem"] = mib

    def ask_chromium():
        """Show the card that says what Chromium runs on, and whether to
        play: Enter plays, s skips, q quits. With --auto or --kiosk the card
        shows for INTRO_S, then it plays."""
        state["kernel"] = exec_out("uname", "-r").strip()
        console.clear()
        console.print(intro_card(state["kernel"]))
        if not START["wait"]:
            time.sleep(INTRO_S)
            return True
        answer = read_answer(
            "\n[grey50]Enter: start Chromium · [bold]s[/bold]: skip · "
            "[bold]q[/bold]: quit[/] ")
        if answer == "q":
            raise KeyboardInterrupt
        return answer != "s"

    def play_chromium():
        """Hand the screen to Chromium for CHROMIUM_S, or until q or Ctrl-C:
        systemd-run starts it as a unit (chromium.service). It runs on a
        pseudo-terminal of ours, BAR_ROWS shorter than the window; its output
        and the keyboard are relayed, and a bar below says where it runs.
        Returns the seconds it played and the sandbox's peak private
        memory."""
        cols, rows = shutil.get_terminal_size()
        kernel = state["kernel"]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows - BAR_ROWS, cols, 0, 0))
        tty_in = sys.stdin.isatty()
        saved = termios.tcgetattr(sys.stdin) if tty_in else None
        peak, done = [0.0], threading.Event()

        def watch():
            while not done.wait(1):
                peak[0] = max(peak[0], private_mib())
        threading.Thread(target=watch, daemon=True).start()
        # Chromium waits for a click before it plays video with sound;
        # the switch lifts that, so the video plays without one.
        cmd = ("exec", CID, "systemd-run", "--pty", "--quiet", "--wait",
               "--unit=chromium", "carbonyl", "--no-sandbox",
               "--autoplay-policy=no-user-gesture-required", VIDEO)
        token = activity.begin(list(cmd))
        stdout = sys.stdout.fileno()
        hello = "Starting Chromium inside the gVisor sandbox…"
        # Scroll only the rows above the bar, as Chromium's terminal does,
        # so a line it ends on its last row never moves into the bar.
        os.write(stdout, (f"\x1b[2J\x1b[1;{rows - BAR_ROWS}r"
                          f"\x1b[{rows // 2};"
                          f"{max(1, (cols - len(hello)) // 2)}H\x1b[1;97m"
                          f"{hello}\x1b[0m").encode())
        t0 = time.monotonic()
        p = subprocess.Popen(sd.cmd(*cmd), stdin=slave, stdout=slave,
                             stderr=slave, start_new_session=True)
        os.close(slave)
        esc, drawn = Escapes(), 0.0
        try:
            if tty_in:
                tty.setraw(sys.stdin)
            while p.poll() is None:
                left = CHROMIUM_S - (time.monotonic() - t0)
                if left <= 0:
                    break
                ready, _, _ = select.select(
                    [master] + ([sys.stdin] if tty_in else []), [], [], 0.1)
                due = time.monotonic() - drawn >= 0.5
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:  # Chromium's side closed
                        break
                    cut = esc.feed(data)
                    if due and cut:  # the bar goes in at a safe point
                        data = (data[:cut] +
                                status_bar(rows, cols, left, kernel) +
                                data[cut:])
                        drawn = time.monotonic()
                    os.write(stdout, data)
                elif due and esc.idle():
                    os.write(stdout, status_bar(rows, cols, left, kernel))
                    drawn = time.monotonic()
                if tty_in and sys.stdin in ready:
                    keys = os.read(sys.stdin.fileno(), 1024)
                    if any(k in keys for k in CLOSE_KEYS):  # close it now
                        break
                    os.write(master, keys)
        finally:
            # Mouse reporting off at once: Chromium turned on any-motion
            # tracking, and nothing reads the keyboard during the slow stop
            # below, so every mouse move would queue a report there.
            os.write(stdout, b"\x1b[?1000l\x1b[?1002l\x1b[?1003l"
                     b"\x1b[?1006l")
            if saved:
                # Then drop input still waiting (reports sent before the
                # terminal saw that, late answers to Chromium's queries),
                # which the next prompt would read as typed. Keys pressed
                # during the stop are kept.
                time.sleep(0.2)
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            sd.call("exec", CID, "systemctl", "stop", "chromium",
                    check=False, log=False)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            os.close(master)
            done.set()
            activity.end(token, 0 if p.returncode in (0, None) else
                         p.returncode, fail_note="stopped after "
                         f"{CHROMIUM_S} s")
            if saved:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
            # Leave Carbonyl's alternate screen and mouse reporting, and
            # show the cursor again.
            os.write(stdout, b"\x1b[r\x1b[?1049l\x1b[?1000l\x1b[?1002l"
                     b"\x1b[?1003l\x1b[?1006l\x1b[?25h\x1b[0m")
        return time.monotonic() - t0, peak[0]

    def parse_status(text):
        for line in text.splitlines():
            k, sep, v = line.strip().partition(": ")
            if sep and k in ("State", "Units", "Failed", "systemd",
                             "Tainted"):
                state["status"][k] = v.strip()

    wait_to_start(render)
    try:
        with scene_live(render, refresh_per_second=8):
            state["phase"] = "booting"
            state["t0"] = time.monotonic()
            sd.call("run", "--bundle", str(b), "-detach", CID)
            for _ in range(600):
                answer = exec_out("systemctl", "is-system-running").strip()
                if answer in ("running", "degraded"):
                    break
                time.sleep(0.05)
            state["boot_ms"] = (time.monotonic() - state["t0"]) * 1000
            state["phase"] = "running"
            # Exactly as shown: with no terminal, systemctl uses no pager.
            out_text = sd.output("exec", CID, "sh", "-c", COMMAND)
            state["terminal"] = out_text.rstrip("\n").splitlines()
            parse_status(out_text)
            sample_memory()
            time.sleep(HOLD_S)
            FRAME.append(render())
            results["systemd"] = {
                "boot_ms": state["boot_ms"],
                "units": state["status"].get("Units"),
                "failed": state["status"].get("Failed"),
                "private_mib": state.get("mem"),
            }
        if ask_chromium():
            state["chromium"] = play_chromium()
            results["systemd"]["chromium_s"] = state["chromium"][0]
            results["systemd"]["chromium_peak_mib"] = state["chromium"][1]
            console.clear()
            FRAME[-1] = render()
            console.print(LiveRender(FRAME[-1], vertical_overflow="ellipsis"))
    finally:
        activity.note("tearing down the systemd sandbox")
        sd.destroy(CID)
        activity.set_header(header)
