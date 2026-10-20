"""Driving runsc: a thin CLI wrapper and busybox OCI bundles."""

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class Runsc:
    def __init__(self, binary, workdir, extra_flags):
        self.binary = binary
        self.workdir = Path(workdir)
        self.root = self.workdir / "state"
        self.flags = [f"--root={self.root}", "--network=none"] + extra_flags
        self.root.mkdir(parents=True, exist_ok=True)

    def cmd(self, *args):
        return [self.binary, *self.flags, *args]

    def call(self, *args, out=subprocess.DEVNULL, check=True):
        r = subprocess.run(self.cmd(*args), stdin=subprocess.DEVNULL,
                           stdout=out, stderr=out)
        if check and r.returncode != 0:
            raise RuntimeError(f"runsc {' '.join(args)} failed ({r.returncode})")
        return r

    def output(self, *args):
        return subprocess.run(self.cmd(*args), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True).stdout

    def version(self):
        out = subprocess.run([self.binary, "--version"], capture_output=True,
                             text=True).stdout
        return out.splitlines()[0].replace("runsc version ", "") if out else "?"

    def ids(self):
        return self.output("list", "-quiet").split()

    def destroy(self, cid):
        self.call("kill", cid, "KILL", check=False)
        self.call("delete", "-force", cid, check=False)

    def destroy_all(self):
        ids = self.ids()
        with ThreadPoolExecutor(32) as ex:
            list(ex.map(self.destroy, ids))
        return len(ids)

    def cleanup(self):
        self.destroy_all()
        # --network=none leaves an nsfs mount under the root dir.
        with open("/proc/mounts") as f:
            mounts = [l.split()[1] for l in f if str(self.workdir) in l]
        for m in sorted(mounts, reverse=True):
            subprocess.run(["umount", "-l", m], stderr=subprocess.DEVNULL)
        shutil.rmtree(self.workdir, ignore_errors=True)


def make_bundle(workdir, name, script, tmpfs_mb=None):
    """An OCI bundle whose rootfs is a single static busybox."""
    b = Path(workdir) / "bundles" / name
    rootfs = b / "rootfs"
    (rootfs / "bin").mkdir(parents=True, exist_ok=True)
    (rootfs / "tmp").mkdir(exist_ok=True)
    (rootfs / "proc").mkdir(exist_ok=True)
    bb = rootfs / "bin" / "busybox"
    if not bb.exists():
        shutil.copy(shutil.which("busybox"), bb)
        for applet in ("sh", "sleep", "echo", "cat", "head", "uname", "dmesg",
                       "date", "printf"):
            (rootfs / "bin" / applet).symlink_to("busybox")
    tmp_opts = [f"size={tmpfs_mb}m"] if tmpfs_mb else []
    spec = {
        "ociVersion": "1.0.0",
        "process": {"args": ["/bin/sh", "-c", script], "cwd": "/",
                    "env": ["PATH=/bin"], "user": {"uid": 0, "gid": 0}},
        "root": {"path": "rootfs", "readonly": True},
        "hostname": "gvisor-demo",
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs",
             "options": tmp_opts},
        ],
        "linux": {"namespaces": [{"type": t} for t in
                                 ("pid", "mount", "ipc", "uts", "network")]},
    }
    (b / "config.json").write_text(json.dumps(spec))
    return b


def time_to_ready(rs, bundle, cid):
    """Milliseconds from `runsc run` until the app prints its first line.

    The bundle's app must print "ready" first; it then exits by itself.
    """
    t0 = time.monotonic()
    p = subprocess.Popen(rs.cmd("run", "--bundle", str(bundle), cid),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    line = p.stdout.readline()
    dt = (time.monotonic() - t0) * 1000
    p.wait()
    return dt if line.strip() == b"ready" else None
