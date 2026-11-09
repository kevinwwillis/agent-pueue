# Admission: start jobs only where they fit

Status: spec for TASK-003 (written 2026-11-09 12:05 ET, revised 12:30 ET after a full review). Nothing here is
built yet.

## Why

Several agents share a few machines: servers that people also use directly, and rented machines that come and
go. Someone has to decide where each big job runs and whether it fits next to what is already there. When a
person or an agent does that by hand, it costs time on every job and goes wrong when two callers pick the same
free GPU.

Admission moves that decision into `job`. Each host runs one loop that measures what is in use, including
processes `job` did not start, counts the room that is left, and starts queued jobs only when they fit. Job
sizes come from measured runs, not guesses, and no rule uses a caller's estimate of run time.

A host without a settings file keeps today's behavior exactly.

## What a caller does

```bash
job room                                              # free room on every admission host
job run auto T-12 --pilot --data /path/to/input -- 'python -u run.py --n 50'
job run auto T-12 --like 5b94c0de -- 'python -u run.py'   # full run, sized from that pilot (token)
job run host2 T-12 --cpus 4 --ram 20G --gpus 1 --gpu-mem 6G -- '...'   # a named host, an explicit size
job history --task T-12                               # records of every admitted job for a task
job hold host2 --gpu 0 --mem 18G --name model-server --while-pid 4242
job release host2 <hold-id>
```

- `auto` in place of a host name lets `job` choose the host (see "Placement"). A held job stays on the host it
  was sent to.
- Every job on an admission host has a size, from one of:
  - **A measured run** (`--pilot`, or no size given). It is counted at the `measured_run` size from the
    settings file plus, if it asks for a GPU (`--gpus 1`), all free memory on the card it gets. Its RAM limit is
    the host's RAM room at release, so it can grow past the counted size and the growth is measured. Its peaks
    are recorded. `--pilot` adds a time limit (`measured_run.max_minutes`, or `--max-minutes`); a job with no
    size has none.
  - **`--like TOKEN`** (or `HOST#ID`, resolved through the records to the newest job with that id on that host,
    and printed): that job's measured peaks times the margins, each capped at the host's or card's maximum when
    the peak itself fits. It must come from a run that exited 0 and did not hit a limit; otherwise `--like` is
    refused, because its peaks are only a lower bound. The new job goes to the same host unless `--any-host` is
    given. Sizes assume memory per worker does not grow with the amount of input, which holds when the pilot
    runs the same code and worker count on fewer items; jobs where it grows give explicit sizes.
  - **Explicit sizes:** `--cpus N`, `--ram SIZE`, `--gpus N`, `--gpu-mem SIZE` (per GPU), `--gpu-share PCT`.
- Constraints: `--host NAME` (repeatable), `--gpu-model TEXT` (a substring of the GPU name), `--needs-path PATH`
  (only hosts where the path exists), `--data PATH` (prefer the host that has it; names the disk it reads),
  `--stop-at TIME` (ISO 8601 with offset; the job is stopped then, for example before a backup window),
  `--in-containers` (see "Jobs that run containers"), `--restartable` (may run on hosts marked
  `preemptible`).
- `--group G` keeps its meaning: a concurrency limit. On an admission host the loop releases a job only while
  group G has a free slot, so release still means start. Without `--group`, admitted jobs go to the group
  `sched`.
- `--env CUDA_VISIBLE_DEVICES=...` is refused on an admission host: admission assigns the GPU.
- `job run` and `job submit` with `auto` or a host wake the caller with the same verdicts as today, plus:
  - `ALERT` can also come from the admission loop (the message says why; the job keeps running). Each kind of
    alert fires once per job.
  - While a job is held, the wait prints why it is held (for example `held: needs 18.0 GB on GPU 0, 9.4 GB
    free`), and `job ls` shows `Held` with the reason.
  - A submit that cannot fit on any host even when that host is empty (its capacity minus headroom) is refused
    at once with `WRAPPER_ERROR` and the reason. A job that fits an empty host but not the current use waits,
    and gets ALERT if it has waited more than `alert_held_min`.

### Progress

The runner sets `JOB_PROGRESS` to a file path. A job that writes lines `done total` there (for example
`120 6264`) gets a projected end time, from its own rate since it started. A job without progress has no
projected end. Projections are shown by `job ls`, `job room` and `job history`. Admission itself does not use
them; deployments may (for example, to warn before a rented machine's budget runs out).

## Host settings file

`~/.config/agent-pueue/admission.json`. Its presence makes a host an admission host. JSON, because the tool is
standard-library Python 3.8. An example for a 32-thread host with 256 GB RAM (illustrative numbers):

```json
{
  "state_dir": "/var/tmp/agent-pueue-admission",
  "cpu_busy_threads_max": 28,
  "placed_cores_max": 48,
  "ram_headroom_gb": 24,
  "memory_pressure_max_pct": 10,
  "gpu_headroom_mb": 1024,
  "gpu_busy_max_pct": 90,
  "disk_busy_max_pct": 30,
  "disk_heavy_mb_s": 50,
  "disks": {"/data": "nvme0n1", "/scratch": "sdb"},
  "outside_window_s": 600,
  "sample_interval_s": 15,
  "pilot_sample_interval_s": 1,
  "fairness_wait_min": 30,
  "alert_held_min": 60,
  "measured_run": {"cpus": 4, "ram_gb": 16, "max_minutes": 30},
  "margins": {"cpu": 1.2, "ram": 1.25, "gpu_mem": 1.15},
  "preemptible": false,
  "scope_slice": "",
  "oom_score_adj": 500,
  "gpu_env": {"TF_FORCE_GPU_ALLOW_GROWTH": "true", "XLA_PYTHON_CLIENT_PREALLOCATE": "false"}
}
```

- `state_dir` holds the queue files, records and lock. It must be a local disk (not a network filesystem), and
  the loop and every `job _host` call on the host read it from here, so their environments can't disagree.
- An unreadable or invalid file stops admission on that host: submits there are refused with the reason. It
  never falls back to admitting without rules.

## The admission loop

`job _admit` runs on each admission host, one instance (an exclusive lock on `state_dir/admission.lock`). On
hosts with a systemd user manager it runs as a user service with `Restart=always`. On hosts without one (some
containers), it runs as a pueue task in its own group `admission` with a parallel limit of 1.

Every `sample_interval_s` (every `pilot_sample_interval_s` while a measured run is going) it measures, counts
room, releases held jobs that fit, and writes samples, records, alerts and a heartbeat (`state_dir/heartbeat`).

If the heartbeat is older than three intervals, the host is not admitting: `job room` shows it as stale and
`auto` skips it, and every held job's wait gets ALERT (`host_probe` reads the heartbeat).

### Holding and releasing

`host_submit` on an admission host first writes the job's size and constraints to
`state_dir/queue/<token>.json`, then adds the task with `pueue add --stashed` into its group. The loop ignores a
stashed task without a queue file and a queue file without a task. The loop creates `sched` if it is missing,
with a parallel limit far above what can fit (256). If a job's group is paused, the loop releases nothing there
and sends ALERT; it never resumes a group.

Release is `pueue enqueue <id>`, and only when the group has a free slot. A job is in flight, and counts against
room, from release while its pueue task is Queued or Running, or while its scope's cgroup exists.

Jobs in other groups on non-admission hosts, and anything started outside `job`, are not admitted jobs: their
use is outside use.

### Per-job scope

The wrapper script (`job` writes it today) stays outside the scope and runs the job's command inside one:

```text
systemd-run --user --scope --quiet --unit=job-<token> -p MemoryMax=<ram> -p MemorySwapMax=0
    -p OOMPolicy=kill [--slice=<scope_slice>] -- nice -n 19 ionice -c 3 bash <command file>
```

- The wrapper is pueue's child, so pueue sees the exit code, and it survives a group out-of-memory kill of the
  scope, so it still writes the exit record and reads the scope's `memory.peak`, `memory.stat` and `cpu.stat`
  just before the scope ends (the final values, which sampling can miss).
- `XDG_RUNTIME_DIR=/run/user/<uid>` is set explicitly; without it `systemd-run --user` fails with exit 1, which
  would look like the job's own failure. If `systemd-run` itself fails, the wrapper writes that as a wrapper
  error, not as the job's exit.
- `MemoryMax` is the job's RAM size (for a measured run, the RAM room at release). `MemorySwapMax=0` keeps an
  over-size job from spilling into swap. `OOMPolicy=kill` stops the whole job when one process is killed for
  memory. The wrapper records `oom` from the scope's `memory.events` and the verdict says the job went over its
  RAM limit of N GB.
- The command's processes get `oom_score_adj` from the settings (raising it needs no privilege), so the kernel's
  global out-of-memory killer picks jobs before other users' processes.
- No CPU quota: a quota throttles jobs that size their worker pools from the core count. Priority stays
  `nice -n 19 ionice -c 3` (or normal with `--normal-priority`). nice ranks processes only inside one cgroup; to
  rank jobs below other users' cgroups, a deployment sets `scope_slice` to a slice it has given a low CPU weight.
- The GPU is assigned with `CUDA_VISIBLE_DEVICES=<GPU UUID>` and `CUDA_DEVICE_ORDER=PCI_BUS_ID`. Among cards that
  fit, the one with the least room that still fits is chosen, to keep large room free on others. `--gpus 2`
  takes two cards on one host. The `gpu_env` settings are added unless the job's `--env` sets them.
- **Probe at loop start:** the loop runs a test scope with a small `MemoryMax` and reads `memory.max` from its
  cgroup. If the limit is not in effect (no delegation, or no user manager), the host runs jobs without scopes:
  no hard RAM limit, use measured through the token and descendants as today, and `job room` marks the host
  `no limits`.

### Ending, killing and clean-up

- `job kill` kills the pueue task and also stops the scope (`systemctl --user stop job-<token>.scope`), so
  detached descendants end too.
- After reading a failed scope's result and peak, the loop resets it (`systemctl --user reset-failed`), so its
  name can't collide with a later job.
- Each cycle the loop stops any job scope whose pueue task is no longer Queued or Running (left behind by a
  `setsid` child or a pueued crash) and records it. pueue's verdict decides whether a job is running; nothing
  of a finished or killed job keeps running or keeps its room.

### Jobs that run containers

Processes started by a container engine run in the engine's cgroups, outside the job's scope: the scope's
limits, nice and per-job measurement don't reach them, and their use shows as outside use. A job that runs its
work in containers says `--in-containers`; it is counted at its size until its containers' use has had
`outside_window_s` to appear as outside use, then at zero, so its use is not counted twice. Deployments that
want limits on container work set them in the container engine.

### Measurement

GPU values come from NVML through `ctypes` (standard library), in one pass per sample: per card memory used and
total, utilization, name and UUID, and per process GPU memory and utilization. Where NVML can't be loaded,
`nvidia-smi` is used, and the outside-use value of a sample is the lower of two consecutive passes, so a job
allocating between two queries doesn't create phantom outside use. GPU utilization for the busy rule is the
average over the last minute.

Per admitted job, from its scope's cgroup: `memory.current`, `memory.stat` and `cpu.stat`. The RAM size basis is
`memory.current − active_file − inactive_file` (anonymous, shared and kernel memory; file cache excluded,
shared memory such as `/dev/shm` included). Per-process GPU values map to a job through `/proc/<pid>/cgroup`.
Bytes read and written come from `io.stat` where the io controller is delegated, otherwise from
`/proc/<pid>/io` summed over the scope's processes (a soft value: short-lived children are missed).

Per host: busy threads from `/proc/stat` deltas; `MemAvailable`; memory pressure (`full avg10`) from
`/proc/pressure/memory`; per-disk busy time from `/proc/diskstats` deltas for the devices in `disks`.

### Room

- **GPU memory, per card:** `memory.total − gpu_headroom − outside − Σ over admitted jobs on the card of
  max(size, current use) − Σ over holds on the card of max(size, own use)`, where `outside` is the highest over
  `outside_window_s` of `max(0, memory.used − jobs' use − holds' own use)`. A hold's own use is its process tree's
  GPU memory when it names a process (`--while-pid`); otherwise it is taken as zero, so the hold's real use is
  counted once as outside use and its size again (safe, but it wastes room).
- **RAM, per host:** the lowest over `outside_window_s` of `MemAvailable + Σ admitted jobs' current use` (what
  would be free with no admitted jobs), minus `ram_headroom`, minus `Σ over admitted jobs of max(size, current
  use)`. A job that ends frees its room at once. Nothing is released while memory pressure is over
  `memory_pressure_max_pct`.
- **CPU, per host:** a job fits when `busy threads (10-minute average) + Σ over jobs released within the last
  10 minutes of max(0, size − their measured use) + its CPU size ≤ cpu_busy_threads_max`, and the CPU sizes of
  admitted jobs including it total at most `placed_cores_max`.
- **GPU time, per card:** closed to new jobs when its average utilization over the last minute is over
  `gpu_busy_max_pct`, or the recorded GPU shares of its admitted jobs add up to more than that. These are
  fractions of time with any kernel running, not exact shares, so this is a soft rule.
- **Disk:** a job whose earlier run read or wrote faster than `disk_heavy_mb_s` is not released while the disk
  of its `--data` path is busier than `disk_busy_max_pct`.

Room a job is sized for but doesn't use is not given to others: whoever got it would fail when the first job
grows back to its size. The record keeps the measured peaks, so the next `--like` uses the measured number.

### Fairness

Release is oldest first. Once the oldest held job has waited `fairness_wait_min`, a younger job is released only
if it takes nothing from a resource on which the oldest job is short (a younger CPU-only job may still start
while the oldest waits for GPU memory). An oldest job that could not fit even with every admitted job on the
host ended, because outside use alone leaves too little, blocks nobody.

### Records

`state_dir/admission.sqlite` (standard-library `sqlite3`), written only by the loop:

- `jobs`: token, task, session, host, pueue id, group, submitted, released, started, ended, exit, how it ended
  (exit, killed, oom, stop-at, time limit, wrapper error), size and where it came from (measured run, like,
  explicit), whether its peaks are complete or a lower bound, constraints, GPU UUIDs, peaks (CPU, RAM basis,
  `memory.peak`, GPU memory, GPU share, bytes read and written), progress and projected end, and the earlier
  job a `--like` size came from.
- `samples`: per-resource values, kept 7 days.
- `holds`: id, name, card, size, start, end, and how it ended (`--while-pid` exit, `--ttl` lapse, or
  `job release`).

Peaks are kept for good. `job history` reads every host's records over the same ssh channel as `job ls`.

### Alerts

The loop appends to `state_dir/alerts/<token>.jsonl`. `host_probe` returns alerts after the client's cursor; a
new wait starts its cursor at the current end, so reattaching doesn't replay old alerts. The loop alerts once
per job for each of: its group is paused; it has been held longer than `alert_held_min` (with the reason); the
loop's heartbeat is stale; a GPU job uses more GPU memory than its size and no cap holds it there (no new job
goes on that card until it is back under).

### Holds

A service that uses a GPU outside `job` (a model server, for example) registers a hold before it loads:
`job hold <host> --gpu <index|uuid> --mem SIZE --name NAME` with `--while-pid PID` (the hold ends when that
process exits) or `--ttl MIN` (it ends unless renewed with `job hold --renew <id>`). A hold is granted only if
the card has the room now; otherwise it is refused at once and the service decides what to do. A caller that
wants room kept for a later step registers a hold the same way. Holds and jobs are first come: neither displaces
the other.

## Placement (client side)

`job run auto` asks every admission host for its room (a new host operation, `room`, which returns the loop's
snapshot and its age) and filters by the constraints. It submits to a host where the job fits now; if none, to
the host that holds its `--data`, then to the host with the fewest held jobs. Hosts that don't answer within
the timeout, or whose snapshot is stale, are skipped and named in the output. The job then stays on that host.

`job room` prints, per host: snapshot age, busy threads, RAM room, memory pressure, each disk's busy time, and
per GPU its name, memory room, outside use, busy time and the jobs and holds on it, plus the held queue with
each reason. `--json` gives the same for scripts.

Rented machines appear as hosts when a deployment's tooling writes a file for each into
`~/.config/agent-pueue/hosts.d/` (same format as the hosts file), read after the hosts file.

## Protocol and rollout

- Admission submits and the new operations (`room`, `hold`, `release`, `history`) use protocol 5, a superset of
  4 (normal priority may be combined with admission fields). Probes and other requests stay at protocol 3
  unless they concern an admission job, so a new client keeps working with hosts not yet updated.
- A host that predates admission refuses a protocol 5 submit. For the new operations it fails with a usage
  error (it has no such operation); the client maps that to "host predates admission".
- An admission host refuses protocol 3 and 4 submits ("update job on the submitting machine"), so an old
  client's job is never run under limits it didn't ask for.
- Rollout: install the new `bin/job` on every machine first. No host has a settings file yet, so nothing
  changes. Then turn admission on one host at a time: add the settings file, start the loop, check it, then the
  next. No pueued restart is needed (a restart kills running jobs). Jobs already running in other groups keep
  running and count as outside use.

## What admission does not do

- It doesn't stop a caller from bypassing it: a process started with `pueue` or a shell directly shows up as
  outside use and counts as taken.
- It doesn't authenticate callers: the session name comes from the caller's environment, and records show it.
- It never kills a job because of a measurement. A job fails by itself when it goes over a hard limit it was
  given (the kernel enforces the RAM limit); the loop's only stops are the ones the caller asked for
  (`--pilot` time limit, `--stop-at`) and clean-up of scopes whose task pueue reports ended.
- It doesn't move a held job to another host.
- It doesn't cap GPU memory by default. Consumer GPUs have no MIG; TASK-003.05 tests MPS and HAMi-core caps.
  Jobs whose GPU memory a cap or MPS server moves to another process are measured as outside use until that is
  settled.
- It doesn't rent machines. Deployments do, and register them through `hosts.d`.

## Tests

As for the rest of `job`: unit tests with a throwaway pueued in a `mktemp -d` folder and faked measurement
inputs (a fake `/proc`, NVML results and cgroup tree under the test folder), covering: fit and no fit for each
resource; outside use, including a hold and a student process on the same card; a finished job freeing its room
at once; fairness; holds by pid and by ttl; group limits and a paused group; a stale heartbeat; an invalid
settings file; a scope left behind after the task ended; `--like` refusing a lower-bound pilot; and protocol
3/4/5 mixes in both directions. The remote test runs against a real host with a real scope: a job over its
memory limit (exit record written, verdict says oom), a job using `/dev/shm` (counted in its size), and
`job kill` of a job with a detached child.
