# gVisor live demo: startup, checkpoint/restore, density

A terminal demo for talks and customer meetings. It starts real gVisor
sandboxes on the presenter's machine and shows the results as they happen.
Kata Containers figures are published reference numbers from
`kata_reference.json`, each with its source (shown on screen, except the
short cloud note in scene 1).

## Get started

The initial supported environment is Ubuntu 24.04 on x86_64, with root
access and a writable cgroup v2 hierarchy. Use a development machine or a
disposable VM. The quick profile does not need Docker or nested virtualization.

1. Install the complete gVisor distribution from the
   [installation guide](https://gvisor.dev/docs/user_guide/install/).
   Keep all files in the release together if you move it.
2. Install the demo dependencies for the system Python (sudo does not see
   a user-only Rich installation):

   ```shell
   sudo apt-get update
   sudo apt-get install -y git python3 python3-rich busybox-static
   ```

3. Clone and check requirements, then run:

   ```shell
   git clone https://github.com/milantracy/gvisor-demo.git
   cd gvisor-demo
   sudo python3 demo.py --check
   sudo python3 demo.py
   ```

There is nothing to build. Use a terminal of at least 110×45. If runsc is
outside sudo's search path, add `--runsc /absolute/path/to/runsc` to both
commands. `--check` reports missing prerequisites without creating sandboxes.

## Profiles and scenes

The default `--profile quick` runs `intro anywhere startup cr summary`.
It uses 10 sequential starts, 10 concurrent starts, a 16 MiB app, and one restore followed by two concurrent
restores, capped at two CPUs and 2 GiB. Limits are reduced to available
resources. The Python UI remains outside the sandbox cgroup.

`--profile full` runs every scene with the original presentation settings:
200 sequential starts, 200 concurrent starts, a 128 MiB app, one restore followed by 10 concurrent restores,
and up to 5,000 idle sandboxes, capped at 16 CPUs and 32 GiB.

Explicit scene names replace the profile's sequence. `all` selects all
scenes; workload flags override the selected profile's defaults.

```shell
sudo python3 demo.py startup cr summary --runs 5 --burst 5
sudo python3 demo.py --profile full --check
sudo python3 demo.py --profile full
sudo python3 demo.py --auto 1 --output quick-results.json
sudo python3 demo.py --kiosk
sudo python3 demo.py --svg captures/
```

The systemd scene additionally needs Docker and network access. It builds
and exports a Chromium image on first use (about 620 MB) into a private
root-owned cache under `/var/tmp/gvisor-demo`. Its browser uses the host
network to play a YouTube video. The image currently supports x86_64 only.

On a host without the required cgroup controllers, `--no-limit` explicitly
allows sandbox workloads to use the machine's available resources.
`--cr-cold` runs sync and drops the **whole host's page cache**. Use it only
on a disposable machine. High density and churn settings are explicit workloads,
not part of the quick sequence.

## Controls and output

Before a scene: Enter starts it, `s` skips, `b` goes back, and `q` quits.
After a scene: Enter continues, `r` repeats, `b` goes back, and `q` quits.
Ctrl-C, SIGTERM and SIGHUP stop the run and clean up its resources.

Each run gets its own private directory under `/tmp`, which is removed on
exit. `--workdir` accepts a new path only, below trusted ancestors without
symlinks. Existing directories are refused. Commands through the runsc wrapper and startup readiness waits time out
after 60 seconds; checkpoint application readiness waits use 30 seconds.
Docker image builds and the interactive browser have separate lifetimes. A failed cleanup preserves the directory
and reports the error.

JSON results are saved after scenes and on exit, including failures and
interruptions, without requiring the summary scene. By default the file has
a unique name in the current directory. `--output PATH` chooses a new file;
existing files and symlinks are refused. When using sudo, the file is created
with the invoking user's permissions. Execution metadata records profile,
scene selection, runtime version, platform selection and resource limits.

The activity panel lists runsc commands. Use `--activity off` to hide it
or `--title-size normal` if double-size titles do not render correctly.
See `python3 demo.py --help` for every option.

## Tests

```shell
python3 -m unittest discover -s tests -v
```

Privileged integration tests must run on a disposable Linux host with the
requirements above and runsc installed:

```shell
sudo env GVDEMO_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

Unit CI uses Ubuntu 24.04. Integration CI is manually dispatched on a dedicated,
freshly provisioned runner labeled `gvisor-demo-disposable`. Do not attach a
persistent or production machine to that label. The integration suite runs the
quick profile, concurrent runs, runtime failures, directory rejection and
SIGINT/SIGTERM/SIGHUP cleanup. It does not download browser images or use YouTube.

## Scenes and talking points

The sizes in this presentation table describe the `full` profile. The default
`quick` profile uses the smaller sizes listed above.

| Scene | What happens | Say |
|---|---|---|
| Intro (`intro`) | The gVisor logo in pixel art, on a white card. | "Everything you'll see is measured live on this machine." |
| 1 Runs anywhere | Checks `/dev/kvm` and vmx/svm, then boots a sandbox that prints gVisor's kernel version and `dmesg`. | "This is a normal cloud VM. A microVM can't start here, and gVisor runs as-is." |
| systemd (`systemd`) | Boots Ubuntu's systemd as the sandbox's init, as in the [systemd-in-gVisor blog post](https://gvisor.dev/blog/2026/09/17/systemd-in-gvisor/) (`--in-sandbox-cgroup=v2 --allow-suid`), waits until `systemctl is-system-running` says "running", and shows `dmesg | head -3; systemctl status` as run inside (gVisor's kernel answers `dmesg`; systemd, PID 1, answers `status`). Then, on Enter, systemd starts Chromium as a unit (`systemd-run --pty --unit=chromium`): [Carbonyl](https://github.com/fathyb/carbonyl), a Chromium build that renders in the terminal, plays a YouTube video until you press `q` (for 30 s with `--auto`, 15 s with `--kiosk`). This scene's sandbox uses the host network (`--network=host`; the sentry still handles every socket call) and needs YouTube to be reachable. The image (`images/systemd-chromium`, built on `images/systemd-ubuntu`) is built and exported once, about 620 MB, to `/var/tmp/gvisor-demo/systemd-chromium`; that needs Docker and network the first time. | "A real Linux init system, booted in under a second in a sandbox. And it runs a real browser, playing YouTube, as a systemd service." |
| 2 Startup latency | 200 sandboxes one after another, then 200 at once. Each sandbox is a box: grey = not started, amber = started, green = ready (its app printed its first line), red = failed; a legend says so. Under each set's boxes: the wall time for all of them and a latency histogram (own axis, p50 and p95 marked). The "at once" panel is on screen from the start, all grey, until its turn. Measured from `runsc run` until the sandbox is ready: the app inside is running and has printed its first line. | "About 75 ms until a new sandbox is ready, and 200 at once in about 1.3 s." |
| ⇉ Sandbox churn (`churn`) | At 1, 10, 20, 50 and 100 at once, workers create sandboxes back to back for 5 s each: each sandbox prints, exits and is destroyed, then the worker creates the next. A live count shows the sandboxes created and destroyed so far and the time elapsed. Below it, one bar per level, updated live: sandboxes created and destroyed per second, with p50 ready and destroy times. | "A sandbox is created, ready and gone in about 80 ms; destroying it takes about 3 ms. This 16-CPU node cycles about 145 sandboxes a second, over 8,000 a minute." |
| 3 Checkpoint/restore | A Python app warms up for real: it imports libraries, then builds a 128 MiB in-memory index, then serves. The demo checkpoints it, restores it, then restores 10 more copies at once from the same image. The counter keeps going from where it stopped. | "Pay warm-up once. Every later copy starts warm in about 110 ms (about 400 ms from disk). Kata has no checkpoint command." |
| 4 Density | Starts up to 5,000 idle sandboxes in batches (`--density`), while a sampler measures each one. If memory fills up first, the ramp stops just short of the limit (about 512 MiB of room left), and a sandbox that fails to start also ends it; `--density-mem PCT` sets a lower stop and shows it on screen. | "About 15 MiB of private memory per sandbox, against about 138 MB for a Kata VM measured the same way. Idle sandboxes use no CPU at all." |
| ★ Scorecard | Summary table, with sample sizes and sources. | |

Results from this host (GCE, 32 vCPU, runsc release-20260727, systrap):

| Result | Value |
|---|---|
| startup latency (until sandbox ready) | 78 ms median, p95 88 ms (n=200, 68–93 ms): 200 one after another in 16.2 s; 200 at once (launched within 10 ms) all ready in about 1.7 s, p50 about 1.4 s |
| checkpoint, 128 MiB app | 54 ms (to page cache) |
| restore | 109 ms from page cache; 410 ms from disk (`--cr-cold`) |
| 10 restores at once | 169 ms median (n=10, 152–175 ms); all serving in 208 ms |
| private memory per idle sandbox | 15.2 MiB median (n=300); host RAM 17.9 MiB each; 300 sandboxes started in 15 batches in about 7 s |
| host RAM per idle sandbox | 17.8 MiB (= PSS 15.6 + kernel 2.3) |
| sandbox churn (peak) | about 145 created and destroyed per second with 20 at once at the default 16-CPU limit (ready 124 ms, destroyed +9 ms); one at a time: ready 76 ms, destroyed +3 ms |
| idle CPU per sandbox | 0 millicores now; 0.03 millicores averaged over the idle window (0 context switches in a 10 s check) |

## Caveats for honest use

- **Kata was not measured.** This host has no nested virtualization. The
  startup scene shows no Kata bar until a measured value exists (see TODO).
  The only published comparison found, Li et al., CLOSER 2021, gives gaps
  (Kata-QEMU 0.6 s slower than gVisor on the same host, Kata 1.11) but no
  absolute times in its text, so it can't serve as a fixed reference.
- **Memory is compared in the Kata source's metric.** The paper counts private
  dirty pages, so the comparison bar uses gVisor's `Private_Dirty`. The paper
  writes "MB"; the demo shows its value as published. The density projection
  uses gVisor's host RAM (which includes kernel memory) against Kata's private
  memory, which favours Kata. It counts memory only;
  CPU, PIDs and disk limits are ignored.
- **Restore reads the image from the page cache by default**, because runsc
  writes the checkpoint through the page cache and returns before it reaches
  the disk. `--cr-cold` runs `sync` and drops the host page cache first, so
  the restore reads from disk. That slows the whole host for a moment.
- **The warm-up in scene 3 is real work but chosen by us**: Python imports
  plus building an index, a few seconds of CPU. The "N× faster than cold"
  ratio depends on `--cr-mb`, which sets the index size. The app runs the
  host's `python3` from the host's `/usr`, mounted read-only.
- **The workload is tiny**: busybox (a small Python app in scene 3) with
  `--network=none`. Real images and
  networking add time to both runtimes.
- **These are demo runs, not benchmarks.** Each figure shows n and its range.
  For a publication, use n≥32 with confidence intervals.
- Kata's checkpoint status is quoted from its
  [Limitations.md](https://github.com/kata-containers/kata-containers/blob/main/docs/Limitations.md).
  Pod-level VM snapshots are in progress upstream
  ([#13653](https://github.com/kata-containers/kata-containers/issues/13653)).
  Update the slide when that work ships.

## TODO

- **Measure Kata startup.** Run Kata (QEMU and Cloud Hypervisor) on a KVM
  host with the same busybox bundle, and put the result in
  `kata_reference.json` as `startup.value_ms` with its `source`. The startup
  scene and scorecard then show it as a fixed reference bar. Kata memory
  should be re-measured the same way.

## Density metrics

A background sampler measures every sandbox on its own, every
`--sample-interval` seconds (default 0.5 s), from the first start until
`--idle-window` seconds (default 5 s) after the last one. The screen shows:

- **Sandboxes up and running**: a progress bar of running sandboxes against
  the target, with the ramp rate and host processes/threads.
- **History**: one panel per metric (host RAM, host CPU, private memory per
  sandbox, startup latency), drawn as a thin line (braille dots, from zero),
  each on its own scale, all on the same time axis in seconds. The title
  shows the current value and the footer the peak. ┊ marks the moment all sandboxes
  are idle. Samples are placed by time, so uneven sampling does not distort
  the shape; probe latency draws as steps. Host CPU is whole-host busy time
  from `/proc/stat`, so it includes the `runsc` commands that start
  sandboxes (per-sandbox CPU, in the table, cannot see those).
- **Timeline** (results file only, not on screen): what happened, in words,
  stamped on the charts' clock: the baseline, each batch (sandboxes started
  and how long it took, the running total, host RAM and its increase, the
  probed startup latency), the start of the idle phase, an idle summary
  (host and per-sandbox CPU, private memory vs Kata) and teardown.
- **Each sandbox measured separately**: min / p50 / p95 / max of private
  dirty memory, PSS and CPU used. Probe
  sandboxes are excluded from all sampled figures.

| Metric | How it is measured |
|---|---|
| sandbox membership | the sentry and gofer command lines contain `--root=<state dir>` and end with the container ID; systrap stubs are their children. Newly seen processes are re-checked for 2 s, since one caught mid-exec shows a transient command line |
| private memory | `Private_Dirty` from `smaps_rollup` of each sandbox's processes: memory no other process shares, and the Kata source's metric |
| PSS | `Pss` from `smaps_rollup`: private memory plus a fair share of shared pages |
| host RAM | drop in `MemAvailable` **plus per-CPU free pages** (`count` in `/proc/zoneinfo`) since the baseline, ÷ sandboxes. `MemAvailable` alone leaves out per-CPU free lists: after a burst of starts they held ~750 MiB on this 32-CPU host and then shrank over tens of seconds, so `MemAvailable` drifted by up to 500 MiB with no real change. The sum stays flat (±35 MiB over 30 s with 200 idle sandboxes). The baseline is taken once the sum is steady (≤ 6 MiB change in 2 s; usually 2 s) |
| kernel memory per sandbox | change in `PageTables + Slab + KernelStack` in `/proc/meminfo`. Includes about 13 KiB per sandbox of slab from the sampler's own `/proc` scans |
| CPU | change in `/proc/<pid>/task/*/schedstat` (exact ns) between samples, shown in absolute units: millicores (1/1000 core) per sandbox, cores for the host (out of its vCPUs). Idle sandboxes read 0: in a 10 s check they made 0 context switches. The scorecard shows the mean over the idle window |
| CPU used since start | the same per-thread schedstat counters read as a running total (ms), so it includes each sandbox's boot work, which the per-sample rate cannot see. It leaves out the `runsc` command that starts the sandbox and kernel setup done before the sandbox's processes exist; the host CPU chart shows those. About 104 ms per sandbox here |
| procs / threads | sandbox processes, and the sum of their thread counts |
| startup latency, busy host | median of `--probes` one-at-a-time starts after each batch, measured as in scene 2 |
| ramp rate | sandboxes ÷ time spent in the ramp; batches of `--step` sandboxes (default 100), `--parallel` starting at the same time (default 32) |

Sampler cost: one scan takes about 0.16 s at 200 sandboxes and about 0.8 s
at 1000. When a scan takes longer than the interval, the next one starts at
once; the panel shows the real interval and the scan time. The sampler did
not change start times measurably (1000 sandboxes, ramp p50/p95: 309/563 ms
with it, 306/545 ms without).

**Start time on a busy host** is measured by probes, never by the ramp.
After each batch, `--probes` sandboxes (default 1) start one at a time, timed
as in scene 2 (`run` until the first output line). The ramp starts many
sandboxes at once, and those starts queue on a host-wide kernel lock:
`pivot_root` (runsc calls it twice per start) walks every host thread while
holding `namespace_sem`, and each idle sandbox adds about 35 threads. Timing
the ramp measured that queue, not a start: at 1000 sandboxes with 32 at once,
p50 was about 650 ms.

Probe results: 78–86 ms with 200 running (75–79 ms on an empty host); 104 ms
with 1000 running (72 ms empty). The remaining growth is the real per-start
kernel cost; removing it needs a runsc change (fewer `pivot_root` calls or
fewer threads per sandbox). The same lock caps the ramp rate at about 90/s
with 1000 running.

Sizes can be changed with flags: `--runs`, `--burst`, `--cr-mb`,
`--cr-cold`, `--restores`, `--density`, `--density-mem`, `--step`, `--parallel`, `--probes`,
`--sample-interval`, `--idle-window`, `--churn-levels`, `--churn-seconds`,
`--cpus`, `--memory`, `--kiosk`, `--kiosk-delay`, `--platform`, `--activity`,
`--activity-width`.
