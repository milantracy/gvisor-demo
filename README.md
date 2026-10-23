# gVisor live demo: startup, checkpoint/restore, density

A terminal demo for talks and customer meetings. It starts real gVisor
sandboxes on the presenter's machine and shows the results as they happen.
Kata Containers figures are published reference numbers from
`kata_reference.json`. Each one appears on screen with its source.

## Run

```
sudo ./demo.py              # all scenes; Enter: next · r: replay scene · q: quit
sudo ./demo.py startup cr   # chosen scenes only
sudo ./demo.py --auto 4     # hands-free: advance every 4 s
sudo ./demo.py --svg out/   # also save each final screen as SVG for slides
sudo ./demo.py cr --cr-cold # restore from disk instead of the page cache
sudo ./demo.py --cpus 8 --memory 16G   # a different limit (default 16 CPUs, 64G)
sudo ./demo.py --no-limit               # sandboxes may use the whole host
sudo ./demo.py --kiosk                  # loop all scenes unattended, 5 s each
```

A **Behind the scenes** panel shows every runsc command as it runs (with its
duration, and a one-line explanation the first time each command appears),
the commands in flight, and background steps such as the density sampler,
start probes, the memory baseline and teardown. It sits beside the scene on
terminals of 170+ columns and below it otherwise; `--activity side|bottom|off`
overrides that. Beside the scene it takes the columns beyond 120 (60 to 100
wide); `--activity-width N` sets it exactly. Command durations there are whole-command times (a `run`
includes the sandbox exiting), so they are longer than the startup latency
figures in the scenes.

**Kiosk mode** (`--kiosk`) runs the scenes in a loop for a booth or a
screen nobody attends: each finished scene stays up for `--kiosk-delay`
seconds (default 5) with a countdown to the next one, and after the
scorecard the loop starts again from a clean state. A scene that fails is
shown, cleaned up and skipped instead of stopping the loop. Ctrl-C, a
closed terminal (SIGHUP) or a kill (SIGTERM) all stop it and clean up
every sandbox and the cgroup.

After each scene the demo asks what to do next: Enter goes on, `r` runs
the same scene again from a clean state (all sandboxes, images, logs and
bundles from the previous attempt are deleted first), and `q` quits.
With `--auto` there is no prompt.

Needs: root, `runsc` on PATH (or `--runsc`), a static `busybox`, and Python 3
with `rich`. Use a terminal of at least 110×45; the density scene with its full timeline needs about 70 lines. The demo cleans up all
sandboxes on exit and on Ctrl-C. Only one run can use a work directory at a
time: a second run on the same `--workdir` refuses to start, because every run
begins by deleting all sandboxes there. Raw numbers go to
`/tmp/gvisor-demo/gvisor-demo-results.json`.

## Limiting the demo to a smaller node

By default the demo behaves like a 16-CPU, 64 GiB node; `--no-limit` lifts
that. A yellow line under every scene title names the limit in force (or
says there is none), so the audience knows what the numbers ran on.

`--cpus N` and `--memory SIZE` put every sandbox in one cgroup v2 group,
pinned to the first N CPUs (`cpuset`, so runsc and the Go runtime inside it
see N CPUs) and capped at SIZE (`memory.max`). runsc places each sandbox in
the cgroup named by its spec's `cgroupsPath` (by default `/<container id>`
at the cgroup root), so with a limit each sandbox gets a small bundle of its
own: a `config.json` that points at the shared rootfs and sets
`cgroupsPath` to `/<group>/<container id>`. The group is removed at exit.

With a limit, CPU figures are the group's own use (`cpu.stat`), out of N
cores, and the density projection uses the memory limit. What the limit covers: every sandbox's processes (sentry, gofer, stubs) in
all scenes, including probes, clones and restores; their memory, including
kernel memory and the page cache they create; and each `runsc` command once
it joins the sandbox's cgroup. Not covered: the first moments of each
`runsc` command, and the demo's own Python process (UI and sampler), which
stays in the caller's cgroup. A limit larger than the host is reduced to
what the host has.
Measured with `--cpus 8`: start throughput peaks at 80/s (172/s on all 32
CPUs), and the group's CPU use tops out at 7.6 cores.

## Layout

```
demo.py                 entry point: options, scene loop, SVG export
kata_reference.json     Kata figures and their sources
gvdemo/runsc.py         runsc CLI wrapper, busybox OCI bundles, startup latency
gvdemo/hostmon.py       host measurements: per-sandbox sampler, meminfo, settling
gvdemo/ui.py            rich helpers: bars, sparklines, formatting, out()/FRAME
gvdemo/activity.py      the "Behind the scenes" panel: commands and background steps
gvdemo/scenes/          one module per scene: anywhere, startup, throughput,
                        checkpoint, density, summary
```

## Scenes and talking points

| Scene | What happens | Say |
|---|---|---|
| 1 Runs anywhere | Checks `/dev/kvm` and vmx/svm, then boots a sandbox that prints gVisor's kernel version and `dmesg`. | "This is a normal cloud VM. A microVM can't start here, and gVisor runs as-is." |
| 2 Startup latency | 200 sandboxes one after another, then 200 at once. Each sandbox is a box: grey = not started, amber = started, green = ready (its app printed its first line), red = failed; a legend says so. Under each set's boxes: the wall time for all of them and a latency histogram (own axis, p50 and p95 marked). The "at once" panel is on screen from the start, all grey, until its turn. Measured from `runsc run` until the sandbox is ready: the app inside is running and has printed its first line. | "About 75 ms until a new sandbox is ready, and 200 at once in about 1.3 s." |
| ⇉ Start throughput | At 1, 5, 10, 20, 50 and 100 at once, workers start sandboxes back to back for 4 s each (each sandbox prints, is counted when ready, then exits). One bar per level: sandboxes per second, with p50/p95 latency and host CPU. | "This 16-CPU node starts about 135 sandboxes a second, over 8,000 a minute. It scales with CPU until the CPUs are busy, then each start just waits longer." |
| 3 Checkpoint/restore | The app loads 128 MiB and initialises for 3 s, then serves. The demo checkpoints it, restores it, then starts 10 clones from one image. The counter keeps going from where it stopped. | "Pay warm-up once. Every later copy starts warm in about 110 ms (about 400 ms from disk). Kata has no checkpoint command." |
| 4 Density | Starts 300 idle sandboxes in batches while a sampler measures each one. | "About 15 MiB of private memory per sandbox, against about 138 MB for a Kata VM measured the same way. Idle sandboxes use no CPU at all." |
| ★ Scorecard | Summary table, with sample sizes and sources. | |

Results from this host (GCE, 32 vCPU, runsc release-20260727, systrap):

| Result | Value |
|---|---|
| startup latency (until sandbox ready) | 78 ms median, p95 88 ms (n=200, 68–93 ms): 200 one after another in 16.2 s; 200 at once (launched within 10 ms) all ready in about 1.7 s, p50 about 1.4 s |
| checkpoint, 128 MiB app | 54 ms (to page cache) |
| restore | 109 ms from page cache; 410 ms from disk (`--cr-cold`) |
| 10 clones at once | 169 ms median (n=10, 152–175 ms); all serving in 208 ms |
| private memory per idle sandbox | 15.2 MiB median (n=300); host RAM 17.9 MiB each; 300 sandboxes started in 15 batches in about 7 s |
| host RAM per idle sandbox | 17.8 MiB (= PSS 15.6 + kernel 2.3) |
| start throughput (peak) | about 136 sandboxes/s with 40 at once at the default 16-CPU limit; 172/s with 50 at once on all 32 CPUs (`--no-limit`, p50 250 ms); about 12/s per worker until the CPUs are busy |
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
- **The cold start in scene 3 is simulated** (random data plus a sleep), so the
  "N× faster than cold" ratio depends on `--cr-mb` and `--cr-warm`.
- **The workload is tiny**: busybox with `--network=none`. Real images and
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
  shows the current value and the footer the peak. ┬ marks each batch start and ┊ the moment all sandboxes
  are idle. Samples are placed by time, so uneven sampling does not distort
  the shape; probe latency draws as steps. Host CPU is whole-host busy time
  from `/proc/stat`, so it includes the `runsc` commands that start
  sandboxes (per-sandbox CPU, in the tile and table, cannot see those).
- **Timeline**: what happened, in words, stamped on the charts' clock: the
  baseline, each batch (sandboxes started and how long it took, the running
  total, host RAM and its increase, the probed startup latency), the start of
  the idle phase, an idle summary (host and per-sandbox CPU, private memory
  vs Kata) and teardown. It grows to at most 20 lines (300 sandboxes in
  batches of 20 make 19 events); the full list is in the results file.
- **Each sandbox measured separately**: min / p50 / p95 / max of private
  dirty memory, PSS, CPU and threads, with the heaviest sandbox named. Probe
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
| ramp rate | sandboxes ÷ time spent in the ramp; each batch of `--step` sandboxes starts at once (`--parallel` overrides) |

Sampler cost: one scan takes about 0.16 s at 200 sandboxes and about 0.8 s
at 1000. When a scan takes longer than the interval, the next one starts at
once; the panel shows the real interval and the scan time. The sampler did
not change start times measurably (1000 sandboxes, ramp p50/p95: 309/563 ms
with it, 306/545 ms without).

**Start time on a busy host** is measured by probes, never by the ramp.
After each batch, `--probes` sandboxes (default 3) start one at a time, timed
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

Sizes can be changed with flags: `--runs`, `--burst`, `--cr-mb`, `--cr-warm`,
`--cr-cold`, `--clones`, `--density`, `--step`, `--parallel`, `--probes`,
`--sample-interval`, `--idle-window`, `--tp-levels`, `--tp-seconds`,
`--cpus`, `--memory`, `--kiosk`, `--kiosk-delay`, `--platform`, `--activity`,
`--activity-width`.
