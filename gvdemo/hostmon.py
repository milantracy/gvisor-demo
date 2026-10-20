"""Host-side measurements: memory, CPU and processes of each sandbox."""

import os
import re
import threading
import time
from pathlib import Path


def meminfo_kib():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0])
    return out


_PCP_COUNT = re.compile(rb"^\s+count:\s+(\d+)", re.M)


def available_kib():
    """MemAvailable plus the pages parked in per-CPU free lists.

    MemAvailable leaves out per-CPU free pages. After a burst of process
    starts and exits those lists grow (to ~750 MiB on a 32-CPU host), and the
    kernel then shrinks them over tens of seconds, so MemAvailable alone
    drifts by hundreds of MiB with no change in real usage. The sum stays
    flat.
    """
    with open("/proc/zoneinfo", "rb") as f:
        pcp = sum(int(c) for c in _PCP_COUNT.findall(f.read()))
    return meminfo_kib()["MemAvailable"] + pcp * os.sysconf("SC_PAGE_SIZE") // 1024


def kernel_kib(mi):
    """Kernel memory that grows with process count."""
    return mi["PageTables"] + mi["Slab"] + mi["KernelStack"]


def wait_settled(tol_mib=6, window_s=2.0, timeout_s=20.0):
    """Wait until available memory stops moving, so it can serve as a baseline.

    Processes that have just exited (for example the previous scene's
    sandboxes) can take a moment to release their memory. On an idle host
    available_kib() moves about 2 MiB in 2 s (4 MiB at p90).
    Returns (seconds waited, settled).
    """
    t0 = time.monotonic()
    hist = []
    while True:
        now = time.monotonic()
        hist.append((now, available_kib()))
        # Keep just enough history to cover the whole window.
        while len(hist) > 1 and now - hist[1][0] >= window_s:
            hist.pop(0)
        vals = [h[1] for h in hist]
        if now - hist[0][0] >= window_s and \
                (max(vals) - min(vals)) / 1024 <= tol_mib:
            return now - t0, True
        if now - t0 >= timeout_s:
            return now - t0, False
        time.sleep(0.25)


def read_proc(path):
    """Read a small /proc file. Much faster than open() for thousands of files."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.read(fd, 4096)
    finally:
        os.close(fd)


def sandbox_procs(root, cache):
    """Map each sandbox's container ID to its host processes.

    The sentry and gofer name the root dir on their command line and end it
    with the container ID. Their descendants (systrap stubs) belong to the
    same sandbox. cache maps pid -> (ppid, cid or None) across calls, so only
    new processes are read.
    """
    marker = f"--root={root}".encode()
    live = {int(d) for d in os.listdir("/proc") if d.isdigit()}
    for pid in list(cache):
        if pid not in live:
            del cache[pid]
    for pid in live - cache.keys():
        try:
            ppid = int(read_proc(f"/proc/{pid}/stat").rsplit(b")", 1)[1].split()[1])
            argv = read_proc(f"/proc/{pid}/cmdline").rstrip(b"\0").split(b"\0")
            cache[pid] = (ppid, argv[-1].decode() if marker in argv else None)
        except (OSError, IndexError, ValueError):
            continue
    children = {}
    for pid, (ppid, _) in cache.items():
        children.setdefault(ppid, []).append(pid)
    procs = {}
    for pid, (_, cid) in cache.items():
        if cid is None:
            continue
        stack = [pid]
        while stack:
            p = stack.pop()
            procs.setdefault(cid, []).append(p)
            stack.extend(c for c in children.get(p, []) if cache[c][1] is None)
    return procs


def _kib_field(rollup, name):
    i = rollup.index(b"\n" + name + b":")
    return int(rollup[i + len(name) + 2:rollup.index(b"kB", i)])


def proc_sample(pid):
    """Memory, threads and CPU of one process, or None if it is gone.

    Returns (PSS KiB, Private_Dirty KiB, threads, CPU ns). CPU time comes
    from per-thread schedstat, which is exact; utime/stime in /proc/pid/stat
    round to 10 ms ticks and read as zero when idle.
    """
    try:
        d = read_proc(f"/proc/{pid}/smaps_rollup")
        pss = _kib_field(d, b"Pss")
        dirty = _kib_field(d, b"Private_Dirty")
        tids = os.listdir(f"/proc/{pid}/task")
    except (OSError, ValueError):
        return None
    cpu = 0
    for t in tids:
        try:
            cpu += int(read_proc(f"/proc/{pid}/task/{t}/schedstat").split(None, 1)[0])
        except (OSError, IndexError, ValueError):
            pass
    return pss, dirty, len(tids), cpu


class Sampler(threading.Thread):
    """Samples every sandbox under root on a fixed interval, in the background.

    Keeps a time series of host-wide totals and the latest per-sandbox
    values. If a scan takes longer than the interval, the next one starts
    straight away; `interval_s` holds the real spacing.
    """

    def __init__(self, root, interval, history=600):
        super().__init__(daemon=True)
        self.root, self.interval, self.history = root, interval, history
        self.base = meminfo_kib()
        self.base_avail = available_kib()
        self.t0 = time.monotonic()
        self.series = []     # dicts: t, n, host_mib, pss_mib, dirty_mib, ...
        self.per_box = {}    # cid -> {"pss_kib", "dirty_kib", "cpu_cores", ...}
        self.interval_s = self.scan_s = 0.0
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self._prev_cpu, self._prev_t = {}, None
        self._pids = {}

    def run(self):
        while not self.stop_ev.is_set():
            t = time.monotonic()
            self.sample()
            self.stop_ev.wait(max(0.0, self.interval - (time.monotonic() - t)))

    def stop(self):
        self.stop_ev.set()
        self.join()

    def sample(self):
        start = time.monotonic()
        boxes = {}
        cpu_now = {}
        for cid, pids in sandbox_procs(self.root, self._pids).items():
            pss = dirty = threads = cpu = n = 0
            for p in pids:
                s = proc_sample(p)
                if s:
                    pss += s[0]
                    dirty += s[1]
                    threads += s[2]
                    cpu += s[3]
                    n += 1
            boxes[cid] = {"pss_kib": pss, "dirty_kib": dirty,
                          "threads": threads, "procs": n}
            cpu_now[cid] = cpu
        t = time.monotonic()
        dt = t - self._prev_t if self._prev_t else None
        total_cores = 0.0
        for cid, b in boxes.items():
            prev = self._prev_cpu.get(cid)
            # A new sandbox has no previous sample: count it from next tick.
            b["cpu_cores"] = (cpu_now[cid] - prev) / 1e9 / dt \
                if dt and prev is not None else None
            total_cores += b["cpu_cores"] or 0
        mi = meminfo_kib()
        point = {
            "t": t - self.t0, "n": len(boxes),
            "host_mib": (self.base_avail - available_kib()) / 1024,
            "pss_mib": sum(b["pss_kib"] for b in boxes.values()) / 1024,
            "dirty_mib": sum(b["dirty_kib"] for b in boxes.values()) / 1024,
            "kernel_mib": (kernel_kib(mi) - kernel_kib(self.base)) / 1024,
            "cpu_cores": total_cores if dt else None,
            "procs": sum(b["procs"] for b in boxes.values()),
            "threads": sum(b["threads"] for b in boxes.values()),
        }
        with self.lock:
            self.series.append(point)
            del self.series[:-self.history]
            self.per_box = boxes
            self.interval_s = dt or 0.0
            self.scan_s = t - start
        self._prev_cpu, self._prev_t = cpu_now, t

    def snapshot(self):
        with self.lock:
            return list(self.series), dict(self.per_box), self.interval_s, \
                self.scan_s


def drop_caches():
    """Write dirty pages to disk, then drop the host page cache."""
    os.sync()
    Path("/proc/sys/vm/drop_caches").write_text("3\n")


def host_facts():
    facts = {}
    try:
        facts["machine"] = Path("/sys/class/dmi/id/product_name").read_text().strip()
    except OSError:
        facts["machine"] = "unknown"
    facts["kvm"] = os.path.exists("/dev/kvm")
    with open("/proc/cpuinfo") as f:
        flags = next((l for l in f if l.startswith("flags")), "")
    facts["virt_flags"] = " vmx " in flags or " svm " in flags
    facts["cpus"] = os.cpu_count()
    facts["mem_gib"] = round(os.sysconf("SC_PAGE_SIZE") *
                             os.sysconf("SC_PHYS_PAGES") / 2**30)
    facts["kernel"] = os.uname().release
    return facts
