# Admission: start jobs only where they fit

Status: spec for TASK-003 (written 2026-11-09 12:05 ET). Nothing here is built yet.

## Why

Several agents share a few machines: servers that people also use directly, and rented machines that come and
go. Someone has to decide where each big job runs and whether it fits next to what is already there. When a
person or an agent does that by hand, it costs time on every job and goes wrong when two callers pick the same
free GPU.

Admission moves that decision into `job`. Each host runs one loop that measures what is in use, including
processes `job` did not start, counts the room that is left, and starts queued jobs only when they fit. Job
sizes come from measured pilot runs, not from guesses, and no rule uses a caller's estimate of run time.

A host without a settings file keeps today's behavior exactly: `job run` queues and starts as before.

## What a caller does

```bash
job room                                              # free room on every admission host
job run auto T-12 --pilot --data /path/to/input -- 'python -u run.py --n 50'
job run auto T-12 --like gpu1#41 -- 'python -u run.py' # full run, sized from pilot gpu1#41
job run gpu2 T-12 --cpus 4 --ram 20G --gpu-mem 6G -- '...'   # a named host, an explicit size
job history --task T-12                               # records of every admitted job for a task
job hold gpu2 --gpu 0 --mem 18G --name model-server --while-pid 4242
job release gpu2 <hold-id>
```

- `auto` instead of a host name lets `job` choose the host (see "Placement").
- Every job submitted to an admission host has a size. It comes from one of:
  - `--pilot`: a first run whose peaks are measured. It is counted as taking the free GPU memory on its card (if
    it asks for a GPU with `--gpus 1`) plus the pilot CPU and RAM defaults from the settings file, and it has a
    time limit (`pilot_max_minutes`, or `--max-minutes`). Its memory limit is that counted size.
  - `--like HOST#ID`: the measured peaks of an earlier job times the margins in the settings file. The new job
    goes to the same host as that job unless `--any-host` is given, and the record notes the worker count the
    earlier job ran with.
  - Explicit sizes: `--cpus N`, `--ram SIZE`, `--gpus N`, `--gpu-mem SIZE` (per GPU), `--gpu-share PCT`.
  - Nothing given: the default size from the settings file (a small job). A job that outgrows it stops with a
    message saying to run `--pilot`.
- Constraints: `--host NAME` (repeatable), `--gpu-model TEXT` (a substring of the GPU name), `--needs-path PATH`
  (only hosts where the path exists), `--data PATH` (prefer the host that has it, and name the disk it reads),
  `--not-after HH:MM` (a window the job must not run into), `--hold-gpu-free SIZE` (room kept free on the card
  for a later step), `--restartable` (may run on hosts marked `preemptible`).
- `job run auto` and `job run <host>` wake the caller exactly as `job run` does today, with the same verdicts.
  Two are added: `ALERT` can now also come from the admission loop (the message says why; the job keeps
  running), and a submit that can never fit on any host, even with every admitted job ended, is refused at once
  with `WRAPPER_ERROR` and the reason.

### Progress

The runner sets `JOB_PROGRESS` to a file path. A job that writes lines `done total` there (for example `120
6264`) gets a projected end time, computed from its own rate since it started. `--progress-from 'CMD'` runs a
command on the host every minute instead, for jobs that already keep completion records; the command prints
`done total`. A job without progress has no projected end. Projections are shown by `job ls`, `job room` and
`job history`, and deployments can act on them (for example to warn before a rented machine's budget runs out).

## Host settings file

`~/.config/agent-pueue/admission.json`. Its presence makes a host an admission host. JSON, because the tool is
standard-library Python 3.8. Example with the field names; the numbers are the deployment's own:

```json
{
  "cpu_busy_threads_max": 18,
  "placed_cores_max": 30,
  "ram_headroom_gb": 16,
  "memory_pressure_max_pct": 5,
  "gpu_headroom_mb": 1536,
  "gpu_busy_max_pct": 95,
  "disk_busy_max_pct": 20,
  "disks": {"/data": "nvme0n1", "/scratch": "sdb"},
  "outside_window_s": 600,
  "sample_interval_s": 15,
  "fairness_wait_min": 30,
  "replace_after_min": 10,
  "default_size": {"cpus": 1, "ram_gb": 4},
  "pilot": {"cpus": 4, "ram_gb": 16, "max_minutes": 30},
  "margins": {"cpu": 1.2, "ram": 1.2, "gpu_mem": 1.1},
  "preemptible": false,
  "scope_slice": "",
  "gpu_env": {"TF_FORCE_GPU_ALLOW_GROWTH": "true", "XLA_PYTHON_CLIENT_PREALLOCATE": "false"}
}
```

An unreadable or invalid file stops admission on that host and every submit there is refused with the reason;
it never falls back to admitting without rules.

## The admission loop

`job _admit` runs on each admission host, one instance (it holds an exclusive lock on
`$STATE/admission.lock`). On hosts with a systemd user manager it runs as a user service; on hosts without one
(some containers) it runs as a pueue task in its own group `admission` with a parallel limit of 1.

Every `sample_interval_s` it:

1. **Measures** (below).
2. **Counts room** for every host resource and every GPU (below).
3. **Releases** held jobs that fit, oldest first, with the fairness rule.
4. **Writes** samples, records and alerts.

### Holding and releasing

On an admission host, `host_submit` adds the job to the pueue group `sched` with `pueue add --stashed`, and
writes its size and constraints to `$STATE/admission/<token>.json`. The loop creates `sched` if it is missing,
with a parallel limit far above what can ever fit (256), so a released job starts at once. Nothing else uses
`sched`. If the group is found paused, the loop releases nothing there and sends ALERT to each job held in it;
it never resumes the group itself.

Release is `pueue enqueue <id>`. From that moment the job counts against room until its scope is gone,
whatever pueue reports.

Jobs in other pueue groups, and anything started outside `job`, are not admitted jobs: their use is measured as
outside use.

### Per-job scope

The runner of an admitted job becomes:

```text
systemd-run --user --scope --unit=job-<token> -p MemoryMax=<ram> -p MemorySwapMax=0 -p OOMPolicy=kill
    [--slice=<scope_slice>] -- nice -n 19 ionice -c 3 bash <script>
```

- `--scope`, not a service: `systemd-run` places itself in the scope and then runs the command, so the job
  stays pueue's child (pueue sees its exit code), keeps its environment (`JOB_TOKEN`, so stall detection works
  unchanged), and stops with `job kill`.
- `MemoryMax` is the job's RAM size. `MemorySwapMax=0` keeps an over-size job from spilling into swap.
  `OOMPolicy=kill` stops the whole job when one of its processes is killed for memory, instead of leaving it
  running without a worker.
- No CPU quota: a quota would throttle jobs that size their worker pools from the core count. Priority stays
  `nice -n 19 ionice -c 3` (or normal with `--normal-priority`). To rank jobs below other users across cgroups,
  a deployment can set `scope_slice` to a slice it has given a low CPU weight.
- The GPU is assigned with `CUDA_VISIBLE_DEVICES=<GPU UUID>` (and `CUDA_DEVICE_ORDER=PCI_BUS_ID`), so CUDA's own
  device order doesn't matter. The `gpu_env` settings are added unless the job's `--env` sets them.
- Where `systemd-run --user` is not available, the job runs without a scope: no hard RAM limit, and its use is
  measured through its token and descendants as today. `job room` marks such a host `no limits`.

### Measurement

Per admitted job, from its scope: `memory.current`, anonymous plus kernel memory from `memory.stat` (the size
basis; `memory.peak` also counts file cache, so it overstates what a job needs), and `cpu.stat` usage. GPU
memory per process from `nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory`, mapped to a job through
`/proc/<pid>/cgroup`. GPU time share per process from `nvidia-smi pmon`. Bytes read and written from
`io.stat` where the io controller is delegated, otherwise from `/proc/<pid>/io` summed over the scope.

Per host: busy threads from `/proc/stat` deltas; available RAM from `MemAvailable`; memory pressure from
`/proc/pressure/memory`; per-disk busy time from `/proc/diskstats` deltas for the devices in `disks`; per GPU,
`memory.used`, `memory.total`, `utilization.gpu`, name and UUID.

Outside use of a GPU, per sample: `memory.used` minus the admitted jobs' measured GPU memory on it minus the
holds on it, never below zero. The value used for room is the highest over the last `outside_window_s`, so a
short dip by someone else's process is not counted as room.

### Room

- **GPU memory, per card:** `memory.total − gpu_headroom − outside use − Σ over admitted jobs on the card of
  max(size, current use) − Σ holds − Σ hold-gpu-free requests`.
- **RAM, per host:** lowest `MemAvailable` over `outside_window_s − ram_headroom − Σ over admitted jobs of
  max(0, size − current use)`. `MemAvailable` already excludes what jobs are using, so only the part of each size
  not yet in use is subtracted. Nothing is released while memory pressure (`full avg10`) is over
  `memory_pressure_max_pct`.
- **CPU, per host:** a job fits when the 10-minute average of busy threads plus its CPU size is at most
  `cpu_busy_threads_max`, and the CPU sizes of admitted jobs including it total at most `placed_cores_max`.
- **GPU time, per card:** closed to new jobs when its `utilization.gpu` is over `gpu_busy_max_pct`, or when the
  GPU shares of its admitted jobs (from their pilots) add up to more than that. These are fractions of time with
  any kernel running, not exact shares, so this is a soft rule.
- **Disk:** a job whose pilot read or wrote heavily (above a settings threshold) is not released while the disk
  of its `--data` path is busier than `disk_busy_max_pct`.
- **Window:** a job with `--not-after` is released only if its projected end (from its pilot's rate) is before
  the window. If it is still running when the window starts, it is stopped with `job kill` and its record says
  why; the caller asked for this.

Room a job is sized for but does not use is not given to others: whoever got it would fail when the first job
grows back to its size. A finished job's record keeps its peaks, so the next `--like` uses the measured number.

### Fairness

Release is oldest first. Once the oldest held job has waited `fairness_wait_min`, a younger job is released
only if it takes nothing from a resource on which the oldest job is short (a younger CPU-only job may still
start while the oldest waits for GPU memory). If the oldest job could not fit even with every admitted job on
the host ended, because outside use alone leaves too little, it blocks nobody, and its caller gets ALERT
saying what it is waiting for.

### Records

The loop writes `$STATE/admission.sqlite` (standard-library `sqlite3`), one writer:

- `jobs`: token, task, session, host, submitted, released, started, ended, exit, size and where it came from
  (pilot, like, explicit, default), constraints, GPU UUID, peaks (CPU, RAM, GPU memory, GPU share, bytes read
  and written), progress and projected end, worker count noted by `--like`.
- `samples`: per-resource values every `sample_interval_s`, kept 7 days.
- `holds`: id, name, card, size, start, end, and how it ends (`--while-pid`, `--ttl`, or `job release`).

Peaks are kept for good. `job history` reads every host's records over the same ssh channel as `job ls`.
`$STATE` is `$XDG_STATE_HOME/agent-pueue`, so a deployment chooses the disk by setting `XDG_STATE_HOME` for the
loop and for `job`.

### Alerts

The loop appends to `$STATE/alerts/<token>.jsonl`. `host_probe` returns alerts newer than the client's last one,
and the client's wait reports `ALERT` with the message (exit 201; the job keeps running). The loop sends ALERT
when a held job's group is paused; when the oldest job can't fit because of outside use; when a GPU job uses
more GPU memory than its size and no cap holds it to that size (no new job goes on that card until it is back
under); and when a deployment hook asks for it.

### Holds

A service that uses a GPU outside `job` (a model server, for example) registers a hold before it loads:
`job hold <host> --gpu <index|uuid> --mem SIZE --name NAME` with `--while-pid PID` (the hold ends when that
process exits) or `--ttl MIN` (it ends unless renewed with `job hold --renew <id>`). A hold is granted only if
the card has the room now; otherwise it is refused at once, and the service decides what to do. Holds and jobs
are first come: neither displaces the other.

## Placement (client side)

`job run auto` asks every admission host for its room (a new host operation, `room`) and filters by the
constraints. It submits to a host where the job fits now; if none, to the host that holds its `--data`, then to
the host with the fewest held jobs. Hosts that don't answer within the timeout are skipped and named in the
output.

While its wait runs, the client checks every `replace_after_min` whether its job is still held. If another host
has room for it now, the client removes the held entry (`pueue remove`, which pueue refuses for a job that has
started, so a job never runs twice) and submits it there with the same token, then waits there. If the new
submit fails, it submits back to the first host. If the caller's session has ended, the job stays held where it
is and starts there when room opens.

`job room` prints, per host: busy threads, RAM room, memory pressure, each disk's busy time, and per GPU its
name, memory room, outside use, busy time and the jobs and holds on it, plus the held queue. `--json` gives the
same for scripts.

Rented machines appear as hosts when a deployment's tooling writes a file for each into
`~/.config/agent-pueue/hosts.d/` (same format as the hosts file). Files there are read after the hosts file.

## Protocol and rollout

- Requests with admission fields, and the new operations (`room`, `hold`, `release`, `history`), use protocol
  5. Hosts accept 3, 4 and 5. A host that predates admission refuses 5, so an admission request is never
  silently run without admission.
- An older client sending protocol 3 to an admission host gets admission with the default size.
- Rollout one host at a time: install the new `bin/job`, add the settings file, start the loop, check it, then
  the next host. No pueued restart is needed (a restart kills running jobs). Jobs already running in other
  groups keep running and count as outside use.

## What admission does not do

- It doesn't stop a caller from bypassing it: a process started with `pueue` or a shell directly shows up as
  outside use and counts as taken.
- It doesn't authenticate callers: the session name comes from the caller's environment, and records show it.
- It never kills a job because of a measurement. A job fails by itself when it goes over a hard limit it was
  given (the kernel for RAM); the loop's only stops are the pilot time limit and `--not-after`, both asked for
  by the caller.
- It doesn't cap GPU memory by default. Consumer GPUs have no MIG; TASK-003.05 tests MPS and HAMi-core caps.
  Without a cap, sharing relies on measured sizes plus `gpu_headroom_mb`.
- It doesn't rent machines. Deployments do, and register them through `hosts.d`.

## Tests

As for the rest of `job`: unit tests with a throwaway pueued in a `mktemp -d` folder, with measurement inputs
faked (a fake `/proc`, `nvidia-smi` and cgroup tree under the test folder), covering: fit and no fit for each
resource, outside use, a finished job freeing its room, fairness, holds, re-placement never running a job
twice, a paused `sched` group, an invalid settings file, and protocol 3/4/5 mixes. The remote test runs
against a real host, with a real scope and a job that goes over its memory limit.
