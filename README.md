# gVisor live demo: startup, checkpoint/restore, density

A terminal demo for talks and customer meetings. It starts real gVisor
sandboxes on the presenter's machine and shows the results as they happen.
Kata Containers figures are published reference numbers from
`kata_reference.json`. Each one appears on screen with its source.

## Run

```
sudo ./demo.py              # all scenes; press Enter to advance
sudo ./demo.py startup cr   # chosen scenes only
sudo ./demo.py --auto 4     # hands-free: advance every 4 s
sudo ./demo.py --svg out/   # also save each final screen as SVG for slides
sudo ./demo.py cr --cr-cold # restore from disk instead of the page cache
```

Needs: root, `runsc` on PATH (or `--runsc`), a static `busybox`, and Python 3
with `rich`. Use a terminal of at least 110×45. The demo cleans up all
sandboxes on exit and on Ctrl-C. Raw numbers go to
`/tmp/gvisor-demo/gvisor-demo-results.json`.

## Layout

```
demo.py                 entry point: options, scene loop, SVG export
kata_reference.json     Kata figures and their sources
gvdemo/runsc.py         runsc CLI wrapper, busybox OCI bundles, time-to-first-output
gvdemo/hostmon.py       host measurements: per-sandbox sampler, meminfo, settling
gvdemo/ui.py            rich helpers: bars, sparklines, formatting, out()/FRAME
gvdemo/scenes/          one module per scene: anywhere, startup, checkpoint,
                        density, summary
```

## Scenes and talking points

| Scene | What happens | Say |
|---|---|---|
| 1 Runs anywhere | Checks `/dev/kvm` and vmx/svm, then boots a sandbox that prints gVisor's kernel version and `dmesg`. | "This is a normal cloud VM. A microVM can't start here, and gVisor runs as-is." |
| 2 Startup | 10 sandboxes one after another, then 50 at once. Time = `runsc run` until the app's first line. | "About 75 ms to a running app. Kata's published extra startup time alone is about 8× that." |
| 3 Checkpoint/restore | The app loads 128 MiB and initialises for 3 s, then serves. The demo checkpoints it, restores it, then starts 5 clones from one image. The counter keeps going from where it stopped. | "Pay warm-up once. Every later copy starts warm in about 110 ms (about 400 ms from disk). Kata has no checkpoint command." |
| 4 Density | Starts 200 idle sandboxes in batches while a sampler measures each one. | "About 15 MiB of private memory per sandbox, against about 138 MB for a Kata VM measured the same way. Idle sandboxes use no CPU at all." |
| ★ Scorecard | Summary table, with sample sizes and sources. | |

Results from this host (GCE, 32 vCPU, runsc release-20260727, systrap):

| Result | Value |
|---|---|
| startup | 76 ms median (n=10, 68–88 ms); 50 at once all ready in 327 ms |
| checkpoint, 128 MiB app | 54 ms (to page cache) |
| restore | 109 ms from page cache; 410 ms from disk (`--cr-cold`) |
| 5 clones at once | 138 ms median (n=5, 130–149 ms) |
| private memory per idle sandbox | 15.1 MiB median (n=200, 14.3–16.5) |
| host RAM per idle sandbox | 17.8 MiB (= PSS 15.6 + kernel 2.3) |
| idle CPU | 0% (0 context switches in a 10 s check) |

## Caveats for honest use

- **Kata was not measured.** This host has no nested virtualization. The Kata
  startup bar is *live gVisor median + 0.6 s*, which is the published Kata-QEMU
  vs gVisor gap from Li et al., CLOSER 2021 (Kata 1.11, gVisor 2020). See TODO.
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
  `kata_reference.json` as `startup.value_ms` (it replaces `delta_ms`). Until
  then the Kata startup bar and the "8×" banner in scene 2 rest on the 2021
  gap. Kata memory should be re-measured the same way.

## Density metrics

A background sampler measures every sandbox on its own, every
`--sample-interval` seconds (default 0.5 s), from the first start until
`--idle-window` seconds (default 5 s) after the last one. The screen shows:

- **Over time**: sparklines of sandbox count, host RAM, host CPU, and the
  probed start time. They cover the whole scene; long runs are squeezed by
  taking each bucket's maximum.
- **Each sandbox measured separately**: min / p50 / p95 / max of private
  dirty memory, PSS, CPU and processes/threads, with the heaviest sandbox named.

| Metric | How it is measured |
|---|---|
| sandbox membership | the sentry and gofer command lines contain `--root=<state dir>` and end with the container ID; systrap stubs are their children |
| private memory | `Private_Dirty` from `smaps_rollup` of each sandbox's processes: memory no other process shares, and the Kata source's metric |
| PSS | `Pss` from `smaps_rollup`: private memory plus a fair share of shared pages |
| host RAM | drop in `MemAvailable` **plus per-CPU free pages** (`count` in `/proc/zoneinfo`) since the baseline, ÷ sandboxes. `MemAvailable` alone leaves out per-CPU free lists: after a burst of starts they held ~750 MiB on this 32-CPU host and then shrank over tens of seconds, so `MemAvailable` drifted by up to 500 MiB with no real change. The sum stays flat (±35 MiB over 30 s with 200 idle sandboxes). The baseline is taken once the sum is steady (≤ 6 MiB change in 2 s; usually 2 s) |
| kernel memory per sandbox | change in `PageTables + Slab + KernelStack` in `/proc/meminfo`. Includes about 13 KiB per sandbox of slab from the sampler's own `/proc` scans |
| CPU | change in `/proc/<pid>/task/*/schedstat` (exact ns) between samples. Idle sandboxes read 0%: in a 10 s check they made 0 context switches. The scorecard shows the mean over the idle window |
| procs / threads | sandbox processes, and the sum of their thread counts |
| start on busy host | median of `--probes` one-at-a-time starts after each batch, timed to first output |
| ramp rate | sandboxes ÷ time spent in the ramp, with `--parallel` starts at once |

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
`--sample-interval`, `--idle-window`, `--platform`.
