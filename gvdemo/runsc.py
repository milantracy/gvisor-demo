"""Driving runsc: a thin CLI wrapper and busybox OCI bundles."""

import json
import os
import re
import select
import signal
import threading
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .activity import activity
from .hostmon import LIMIT


class Runsc:
    def __init__(self, binary, workdir, extra_flags):
        self._processes = set()
        self._mu = threading.RLock()
        self._cancelled = threading.Event()
        self.binary = binary
        self.workdir = Path(workdir)
        self.root = self.workdir / "state"
        self.flags = [f"--root={self.root}", "--network=none"] + extra_flags
        self.root.mkdir(parents=True, exist_ok=True)
        activity.set_header(f"runsc {' '.join(self.flags)} <command>")

    def with_flags(self, *extra):
        """The same runsc (same root and sandboxes) with extra global flags;
        a flag given again (--network=host) replaces the one set before."""
        names = {f.split("=", 1)[0] for f in extra}
        other = Runsc.__new__(Runsc)
        other.__dict__.update(self.__dict__)
        other.flags = [f for f in self.flags
                       if f.split("=", 1)[0] not in names] + list(extra)
        return other

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

    def start(self, args, **kw):
        """Start and track a runsc command in its own process group."""
        with self._mu:
            if self._cancelled.is_set() and args[0] not in ('kill', 'delete', 'list'):
                raise RuntimeError('demo execution was cancelled')
            process = subprocess.Popen(self.cmd(*args), start_new_session=True, **kw)
            self._processes.add(process)
        return process

    def finish(self, process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        with self._mu:
            self._processes.discard(process)

    def cancel_pending(self):
        self._cancelled.set()
        with self._mu:
            for process in self._processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def _run(self, args, log=True, fail_note=None, **kw):
        token = activity.begin(list(args)) if log else None
        rc = -1
        process = None
        capture = kw.pop('capture_output', False)
        if capture:
            kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process = self.start(args, stdin=subprocess.DEVNULL, **kw)
            stdout, stderr = process.communicate(timeout=60)
            rc = process.returncode
            return subprocess.CompletedProcess(process.args, rc, stdout, stderr)
        finally:
            if process is not None:
                self.finish(process)
            if token is not None:
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
                             text=True, check=True, timeout=10).stdout
        return out.splitlines()[0].replace("runsc version ", "") if out else "?"

    def ids(self, log=True):
        result = self._run(('list', '-quiet'), log=log, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'cannot list sandboxes: {result.stderr.strip()}')
        return result.stdout.split()

    def destroy(self, cid, log=True):
        # kill fails harmlessly when the app has already exited.
        self.call("kill", cid, "KILL", check=False, log=log,
                  fail_note="already stopped")
        self.call("delete", "-force", cid, log=log)

    def destroy_all(self):
        """Kill and delete every sandbox, logged as a single step."""
        ids = self.ids(log=False)
        if not ids:
            return 0
        token = activity.begin(["kill + delete", f"{len(ids)} sandboxes"])
        with ThreadPoolExecutor(32) as ex:
            list(ex.map(lambda c: self.destroy(c, log=False), ids))
        remaining = self.ids(log=False)
        if remaining:
            raise RuntimeError(f'sandboxes remain after deletion: {remaining}')
        activity.end(token, 0)
        return len(ids)

    def cleanup(self, preserve=False):
        if self.root.exists():
            self.destroy_all()
        mounts = owned_mounts(self.workdir)
        for mount in mounts:
            subprocess.run(['umount', '-l', mount], check=True, timeout=10,
                           stderr=subprocess.DEVNULL)
        if owned_mounts(self.workdir):
            raise RuntimeError(f'mounts remain under {self.workdir}; preserving work directory')
        if preserve:
            return
        # Keep the private parent for scene retries and kiosk loops.
        for path in self.workdir.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def owned_mounts(workdir, mountinfo='/proc/self/mountinfo'):
    """Mounts at or below the private run directory, deepest first."""
    root = Path(workdir)
    mounts = []
    with open(mountinfo) as file:
        for line in file:
            encoded = line.split()[4]
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), encoded))
            if mount == root or root in mount.parents:
                mounts.append(str(mount))
    return sorted(mounts, key=lambda name: len(Path(name).parts), reverse=True)


def make_bundle(workdir, name, script, tmpfs_mb=None, python=False):
    """An OCI bundle whose rootfs is a single static busybox.

    With python=True, script is a Python program instead, run by the host's
    python3 from the host's /usr, mounted read-only.
    """
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
    args = ["/bin/sh", "-c", script]
    env = ["PATH=/bin"]
    mounts = []
    if python:
        (rootfs / "app.py").write_text(script)
        for d in ("usr", "lib", "lib64"):
            if not (rootfs / d).exists():
                (rootfs / d).mkdir() if d == "usr" else \
                    (rootfs / d).symlink_to(f"usr/{d}")
        args = ["/usr/bin/python3", "-u", "/app.py"]
        env = ["PATH=/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1"]
        mounts = [{"destination": "/usr", "type": "bind", "source": "/usr",
                   "options": ["rbind", "ro"]}]
    spec = {
        "ociVersion": "1.0.0",
        "process": {"args": args, "cwd": "/", "env": env,
                    "user": {"uid": 0, "gid": 0}},
        "root": {"path": "rootfs", "readonly": True},
        "hostname": "gvisor-demo",
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs",
             "options": tmp_opts},
            *mounts,
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
    if not Path(spec["root"]["path"]).is_absolute():
        spec["root"]["path"] = str(base / spec["root"]["path"])
    spec["linux"]["cgroupsPath"] = f"/{cgroup}/{cid}"
    (d / "config.json").write_text(json.dumps(spec))
    return d


def time_to_ready(rs, bundle, cid, on_ready=None):
    """Milliseconds from `runsc run` until the app prints its first line.

    The bundle's app must print "ready" first; it then exits by itself.
    on_ready, if given, is called the moment that line arrives.
    """
    args = ["run", "--bundle", str(bundle), cid]
    token = activity.begin(args)
    t0 = time.monotonic()
    process = None
    rc = -1
    try:
        process = rs.start(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        line = bytearray()
        while b'\n' not in line:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise subprocess.TimeoutExpired(process.args, 60)
            chunk = os.read(process.stdout.fileno(), 1024)
            if not chunk:
                break
            line.extend(chunk)
            if len(line) > 4096:
                raise RuntimeError('sandbox readiness line exceeds 4096 bytes')
        dt = (time.monotonic() - t0) * 1000
        if on_ready:
            on_ready()
        rc = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return dt if rc == 0 and line.split(b'\n', 1)[0].strip() == b'ready' else None
    finally:
        if process is not None:
            rs.finish(process)
            process.stdout.close()
        activity.end(token, rc)
