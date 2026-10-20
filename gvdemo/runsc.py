"""Driving runsc: a thin CLI wrapper and busybox OCI bundles."""

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .activity import activity
from .hostmon import LIMIT


class Runsc:
    def __init__(self, binary, workdir, extra_flags):
        self.binary = binary
        self.workdir = Path(workdir)
        self.root = self.workdir / "state"
        self.flags = [f"--root={self.root}", "--network=none"] + extra_flags
        self.root.mkdir(parents=True, exist_ok=True)
        activity.set_header(f"runsc {' '.join(self.flags)} <command>")

    def cmd(self, *args):
        return [self.binary, *self.flags, *self.place(args)]

    def place(self, args):
        """With a node limit, run each sandbox inside the limited cgroup.

        runsc puts a sandbox in the cgroup named by its spec's cgroupsPath
        (by default /<container id>, at the cgroup root). Bundles are shared
        by many sandboxes, so each sandbox gets its own small bundle: a
        config.json pointing at the shared rootfs, with cgroupsPath
        /<limit group>/<container id>.
        """
        args = list(args)
        if not LIMIT["cgroup"] or "--bundle" not in args:
            return args
        i = args.index("--bundle")
        args[i + 1] = str(sandbox_bundle(args[i + 1], args[-1],
                                         LIMIT["cgroup"]))
        return args

    def _run(self, args, log=True, fail_note=None, **kw):
        """subprocess.run of runsc, logged in the activity panel.

        fail_note marks a failure as expected and names its reason.
        """
        if not log:
            return subprocess.run(self.cmd(*args), stdin=subprocess.DEVNULL, **kw)
        token = activity.begin(list(args))
        rc = -1
        try:
            r = subprocess.run(self.cmd(*args), stdin=subprocess.DEVNULL, **kw)
            rc = r.returncode
            return r
        finally:
            activity.end(token, rc, fail_note)

    def call(self, *args, out=subprocess.DEVNULL, check=True, log=True,
             fail_note=None):
        r = self._run(args, log=log, fail_note=fail_note, stdout=out, stderr=out)
        if check and r.returncode != 0:
            raise RuntimeError(f"runsc {' '.join(args)} failed ({r.returncode})")
        return r

    def output(self, *args, log=True):
        return self._run(args, log=log, capture_output=True, text=True).stdout

    def capture(self, *args):
        return self._run(args, capture_output=True, text=True)

    def version(self):
        out = subprocess.run([self.binary, "--version"], capture_output=True,
                             text=True).stdout
        return out.splitlines()[0].replace("runsc version ", "") if out else "?"

    def ids(self, log=True):
        return self.output("list", "-quiet", log=log).split()

    def destroy(self, cid, log=True):
        # kill fails harmlessly when the app has already exited.
        self.call("kill", cid, "KILL", check=False, log=log,
                  fail_note="already stopped")
        self.call("delete", "-force", cid, check=False, log=log)

    def destroy_all(self):
        """Kill and delete every sandbox, logged as a single step."""
        ids = self.ids(log=False)
        if not ids:
            return 0
        token = activity.begin(["kill + delete", f"{len(ids)} sandboxes"])
        with ThreadPoolExecutor(32) as ex:
            list(ex.map(lambda c: self.destroy(c, log=False), ids))
        activity.end(token, 0)
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


def sandbox_bundle(base, cid, cgroup):
    """A per-sandbox bundle that shares base's rootfs and sets its cgroup."""
    base = Path(base)
    d = base / "ids" / cid
    d.mkdir(parents=True, exist_ok=True)
    spec = json.loads((base / "config.json").read_text())
    spec["root"]["path"] = str(base / "rootfs")
    spec["linux"]["cgroupsPath"] = f"/{cgroup}/{cid}"
    (d / "config.json").write_text(json.dumps(spec))
    return d


def time_to_ready(rs, bundle, cid):
    """Milliseconds from `runsc run` until the app prints its first line.

    The bundle's app must print "ready" first; it then exits by itself.
    """
    args = ["run", "--bundle", str(bundle), cid]
    token = activity.begin(args)
    t0 = time.monotonic()
    p = subprocess.Popen(rs.cmd(*args),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    line = p.stdout.readline()
    dt = (time.monotonic() - t0) * 1000
    activity.end(token, p.wait())
    return dt if line.strip() == b"ready" else None
