# Admission: start jobs only where they fit

Status: spec for TASK-003 (written 2026-11-09 12:05 ET; revised 12:20 and 12:50 ET after two full reviews;
"Per-job scope" revised 13:10 ET after a code review). Built so far: the per-job scope (job 0.6.0, protocol 5,
used only by a submit that carries a scope), then the loop, held submits, records, the client commands
(`--cpus/--ram/...`, `job run auto`, `job room`), holds and the loop's service command (all in job 0.6.0, not yet
released), progress files with projected ends and the RAM-trend alert, and `job history`. Not built yet:
rented machines as hosts (`hosts.d`).

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
job run host2 T-12 --small -- 'sha256sum big.tar'     # the settings file's small size
job history --task T-12                               # records of every admitted job for a task
job hold host2 --gpu 0 --mem 18G --name model-server --unit model-server.service
job release host2 <hold-id>
```

- `auto` in place of a host name lets `job` choose the host (see "Placement"). A held job stays on the host it
  was sent to.
- Every job submitted to an admission host needs a size, from one of:
  - **`--pilot`**: a first run to measure the job. It is counted at, and limited to, all the RAM room at
    release, and, with `--gpus N`, counted at all the free memory on the cards it gets (GPU memory is not
    capped), so nothing else can be admitted into room it may grow into. It has a time limit (`pilot.max_minutes`, or `--max-minutes`), so it never holds the
    room for long; the limit counts from its start. It takes `--cpus` and `--gpus`, and a GPU pilot needs at
    least `pilot.gpu_min_gb` free on each card. Its use is sampled every `pilot_sample_interval_s`.
  - **`--like TOKEN`** (or `HOST#ID`, the newest job with that id on that host): that job's measured peaks
    times the margins, each capped at the host's or card's maximum when the peak itself fits, and its peak disk
    rate. The size is printed (`sized like <token>: ...`). Accepted from a run that exited 0, or a pilot that
    reached its time limit, and measured at least 3 times while it ran; refused from a run that was killed for
    memory, crashed or was killed, because its peaks are only a lower bound. The new job runs on the host that
    holds the record: `HOST#ID` is sent only to HOST, and `auto` with a token tries the hosts in turn, the
    others refusing for want of the record. Sampled peaks are a lower bound even for a clean run; the margins
    and `ram_hard_factor` cover the gap between samples.
  - **Explicit sizes:** `--ram SIZE` (required: it is the job's RAM limit), with `--cpus N` (default: the small
    size's CPUs), `--gpus N`, `--gpu-mem SIZE` (per GPU), `--gpu-share PCT`; or `--small`, the settings file's
    small size.
  - A submit with none of these is refused, with a message naming the options.
- Constraints: `--host NAME` (repeatable), `--gpu-model TEXT` (a substring of the GPU name), `--needs-path PATH`
  (repeatable; a host without the path refuses the job, so `auto` tries the next), `--data PATH` (prefer the
  host that has it; names the disk it reads), `--stop-at TIME` (`YYYY-MM-DDTHH:MM±HH:MM`, the offset required;
  the job is stopped then, and if it is still held then, it is dropped), `--in-containers` (see "Jobs that run
  containers"), `--restartable` (may run on hosts marked `preemptible`). `--max-minutes` is for pilots only.
- `--group G` keeps its meaning, a concurrency limit: the loop releases a job only while group G has a free slot,
  so release still means start. Without `--group`, admitted jobs go to the group `sched`, so on an admission host
  they run as soon as they fit, not one at a time as in pueue's `default` group.
- GPUs are assigned by admission: `--env CUDA_VISIBLE_DEVICES=...` is refused, and a job without `--gpus` gets
  `CUDA_VISIBLE_DEVICES=` (no GPU visible). A job that uses a GPU must ask for one.
- `job run` and `job submit` wake the caller with the same verdicts as today. Changes:
  - `ALERT` can also come from the admission loop (the message says why; the job keeps running). Each kind of
    alert fires once per job.
  - While a job is held, its wait prints a line saying why (for example `held: needs 18.0 GB on GPU 0, 9.4 GB
    free`) every 10 minutes, and `job ls` shows `Held` with the reason. Being held is not a verdict and does not
    end a multi-job wait.
  - A submit that cannot fit on any host even when that host is empty (its capacity minus headroom) is refused
    at once with `WRAPPER_ERROR` and the reason.
  - When a job's command ends, any processes it left running (for example a `nohup ... &`) are stopped within one
    cycle. A job that needs a service to outlive it starts the service some other way.

`docs/usage.md` gets a section listing these changes for admission hosts.

### Progress

The runner sets `JOB_PROGRESS` to a file path for every job. A job that appends lines `done total` there (for
example `120 6264`) gets a projected end time on an admission host: the loop notes the time of each new line
and projects from the rate over the last 20 lines; `job ls` and `job room` show it. A job without progress has
no projected end. A new `total` starts a new series. The loop also records the RAM size basis at each progress
line: once there are 3 lines covering at least 5% of `total`, if RAM grows with `done` and the trend reaches the
job's RAM limit before `total`, the owner gets ALERT early, before the kernel would stop it.
Projections are shown by `job ls`, `job room` and `job history`. Deployments may use them (for example, to warn
before a rented machine's budget runs out).

## Host settings file

`~/.config/agent-pueue/admission.json`. Its presence makes a host an admission host. JSON, because the tool is
standard-library Python 3.8. An example for a 32-thread host with 256 GB RAM (illustrative numbers):

```json
{
  "state_dir": "~/.local/state/agent-pueue/admission",
  "cpu_busy_threads_max": 28,
  "placed_cores_max": 48,
  "ram_headroom_gb": 24,
  "ram_hard_factor": 1.5,
  "memory_pressure_max_pct": 10,
  "gpu_headroom_mb": 1024,
  "gpu_busy_max_pct": 90,
  "iowait_max_pct": 20,
  "disk_busy_max_pct": 30,
  "disk_heavy_mb_s": 50,
  "disks": ["/", "/data", "/scratch"],
  "outside_window_s": 600,
  "cpu_window_s": 600,
  "warmup_s": 600,
  "sample_interval_s": 15,
  "pilot_sample_interval_s": 1,
  "fairness_wait_min": 30,
  "small": {"cpus": 1, "ram_gb": 4},
  "pilot": {"max_minutes": 30, "gpu_min_gb": 2},
  "margins": {"cpu": 1.2, "ram": 1.25, "gpu_mem": 1.15},
  "preemptible": false,
  "oom_score_adj": 500,
  "gpu_env": {"TF_FORCE_GPU_ALLOW_GROWTH": "true", "XLA_PYTHON_CLIENT_PREALLOCATE": "false"}
}
```

- `state_dir` (default `~/.local/state/agent-pueue/admission`, home-expanded, never taken from `XDG_*`, so the
  loop's environment and an ssh session can't disagree) holds the queue files, records and lock. It must be on a
  local disk, not a network filesystem. The scripts and
  exit records of every job are under `$XDG_STATE_HOME/agent-pueue` (default `~/.local/state/agent-pueue`), as
  before admission, so ssh sessions and the user manager must agree on `XDG_STATE_HOME` (both leave it unset on
  a stock Ubuntu).
- An unreadable or invalid file stops admission on that host: submits there are refused with the reason. It
  never falls back to admitting without rules.
- `disks` lists paths; each is resolved to its disk at loop start, and `job room` shows each disk's busy time.
  `warmup_s` (default: the longer window, at most that) is how much unbroken measurement the loop needs before
  it releases anything (see "The admission loop").
- Needs cgroup v2, kernel 5.19 or later (`memory.peak`), systemd 243 or later (scope `OOMPolicy`), and a user
  manager that outlives logins (lingering). `job admission <host> start` checks these with a test scope and
  refuses to start the loop when one is missing; there is no mode without limits.

## The admission loop

`job _admit` runs on each admission host, one instance (an exclusive lock on `state_dir/loop.lock`). It runs as
a systemd user service with `Restart=always`: `job admission <host> start` writes
`~/.config/systemd/user/agent-pueue-admit.service`, enables and starts it; `stop` disables and stops it;
`status` shows the service state, whether the settings are valid, and the heartbeat's age. The user manager
must outlive logins (`loginctl enable-linger`), as pueued's does. On a host without a systemd user manager (some containers), it can
run as a pueue task in its own group `admission`, parallel 1; there it dies with pueued and nothing restarts it,
which the heartbeat makes visible.

It writes a heartbeat (`state_dir/heartbeat`) every cycle. A heartbeat older than
`max(3 × sample_interval_s, 120 s)` (longer than one slow pueue call) means the host is not admitting: `room`
says so, `auto` skips it, and held jobs' waits get ALERT. When `bin/job` changes on disk (after an install), the
loop re-executes itself with the new code.

Each cycle it measures, counts room, releases held jobs that fit, and writes samples, records and alerts. An
error in one cycle is written to the journal and the loop goes on; the heartbeat is renewed only by a cycle that
completes and could read pueue's status, so an error that repeats, or pueued being down, shows as a stale loop.

Its last samples are kept in `admission.sqlite` (`recent`, the longest window plus a minute), so the windows
carry on over a restart or an install. The loop releases nothing until it has `warmup_s` of unbroken
measurement (a gap longer than the heartbeat limit breaks it): after a long stop it can't know the outside use
over the windows. Meanwhile held jobs say `the loop is measuring this host first`, `job room` says so, `auto`
may hold jobs there but not start them, and holds are refused.
Between cycles it checks the results folder every second, so a job ending wakes it at once.

### Submitting, holding and releasing

Submits and releases on a host take one short lock (`state_dir/queue.lock`), so the room a submit sees and the
room the loop releases into can't race.

- `host_submit` on an admission host, under the lock: creates `sched` if it is missing (parallel 256); checks the
  token against existing tasks (a retried submit returns the first one, unless that one is a held task without
  a queue file, left by a submit that failed after `pueue add`: that task is removed and the job queued again);
  writes `state_dir/queue/<token>.json` (written to a temporary name, then renamed) and the job's first record;
  then adds the task with `pueue add --stashed`. A submit that fails removes its queue file, its record and, if
  pueue added it, its held task. The loop ignores a stashed task without a queue file and a queue file without
  a task.
- A queue file and pueue can disagree. A job marked released whose task is still held (the loop stopped
  between the two) is held again; a held job whose task started anyway (a `pueue enqueue` that failed after
  pueue acted, or a person) is counted as released from then on, and ends with a record. When `pueue enqueue`
  fails, the job stays counted, since pueue may have acted.
- The client treats a host's refusal (its JSON reply says so, or `job` there rejects the command line) as
  definite. Anything else without a reply (ssh failed, `job` there was killed) means the request may or may not
  have taken effect, so a submit is retried only with the same token, and `auto` never sends it to another
  host.
- A submit marked `fit_now` (used by `auto`) is queued only if the job would be released in the next cycle,
  counting the jobs already held ahead of it; otherwise it is refused without queueing, and `auto` tries the next
  host.
- Release, under the lock, is `pueue enqueue <id>`, only while the job's group has a free slot and is not
  paused. A paused group gets one ALERT per held job.
- A job is in flight, and counts against room, from release while its pueue task is Queued, Running, Paused or
  Locked, or while its scope's `job/` cgroup has processes. A job paused with pueue still holds its RAM and GPU memory.

Jobs in other groups on non-admission hosts, and anything started outside `job`, are not admitted jobs: their
use is outside use.

### Per-job scope

Three layers, all written by `job`:

1. **The wrapper**, pueue's child, as today. It ends any scope an earlier run of the same job left (a restart
   reuses the token), runs the scope, and writes the exit record.
2. **`systemd-run --user --scope --quiet --unit=agent-pueue-job-<token>.scope -p Delegate=yes
   -p OOMPolicy=continue -p CPUWeight=<1 or 100>`**, with `XDG_RUNTIME_DIR=/run/user/<uid>` set explicitly
   (without it `systemd-run --user` fails). The scope is delegated, so the shim can divide it. The RAM limit and
   the GPUs come from an assignment file the wrapper reads: written at submit for a scope given in the request,
   or by the loop at release for an admitted job.
3. **The shim**, inside the scope. It first checks that it is in its own scope (it never changes any other
   cgroup), then splits the scope into two cgroups: `shim/`, where it moves itself, and `job/`, which gets
   `memory.max` (the hard limit), `memory.swap.max` 0 and `memory.oom.group` 1. It reads these back; if any
   step fails or a value did not take effect, the job does not run and the result is a wrapper error naming
   the step. It runs the command in `job/` at the wrapper's priority (nice 19 and idle I/O, or normal with
   `--normal-priority`), with `oom_score_adj` from the settings, so the kernel's global out-of-memory killer
   picks jobs before other users' processes.

Because the shim is outside `job/`, it outlives a memory kill of the job. When the command ends it waits up to
two seconds for processes still dying from a kill, stops any processes the job left behind (`cgroup.kill`), then
writes `job/`'s final `memory.peak` (which counts page cache, so it is not a RAM size), `memory.events` and
`cpu.stat` next to the exit record, and exits with the command's code. How the job ended, from those counters:

- `oom_kill` or `oom_group_kill` above 0 with `oom` above 0: it went over its RAM limit; the whole job was
  stopped.
- `oom_kill` above 0 with `oom` 0: the host ran out of memory and the kernel's out-of-memory killer stopped the
  job while it was under its limit.
- Exit 137 (signal 9) with no out-of-memory event: not its RAM limit; systemd-oomd, a person or the job itself
  sent it (the exit code can't say which).
- A clean exit (0) stays a success; a memory kill of a process it left running is noted.
- The shim's record is missing although the job started: the whole scope was killed from outside, for example by
  systemd-oomd.
- The `started` marker is missing: `systemd-run` failed or the setup failed. Recorded as a wrapper error,
  never as the job's exit.

These hold on every systemd version the spec supports; none of them needs systemd's `MemoryPeak` property
(systemd 256 and later). The kernel stores the limit in whole pages, so limits are rounded to pages.

The limits guard against accidents, not against a job that sets out to escape them: the job runs as the user who
owns its scope, so it could change them. Restarting the user's systemd manager (or losing lingering) ends every
scope, as it ends pueued.

Limits and settings:

- **RAM:** the hard limit on `job/` is the job's size times `ram_hard_factor` (a pilot's is its counted room).
  Room is counted at `max(size, current use)`, so a job that runs over its size takes room from new jobs at
  once, and only past the hard limit is it stopped. `memory.swap.max` 0 keeps an over-size job from spilling
  into swap. `memory.oom.group` 1 stops the whole job when one process is killed for memory, instead of leaving
  it running short of a worker.
- **CPU:** no quota; a quota throttles jobs that size their worker pools from the core count. Ranking jobs below
  other users' work needs CPU weights set by root on the slices involved (the user's own `user-<uid>.slice`, and
  any slice other users' work runs in, such as a notebook server's); that is up to the deployment. Nice ranks
  processes only inside one cgroup, and each job is a cgroup of its own, so a job at idle priority gets
  `CPUWeight=1` (about nice 19's share) and one at `--normal-priority` gets 100.
- **GPU:** `CUDA_VISIBLE_DEVICES=<GPU UUIDs>` and `CUDA_DEVICE_ORDER=PCI_BUS_ID`; the assigned UUIDs are also in
  `JOB_GPU_UUIDS` for jobs that pass them on (for example to a container). Among cards that fit, the one with the
  least room that still fits is chosen. `--gpus 2` takes two cards on one host. Code with a hard-coded device
  number other than 0 fails under this; it should use device 0..N-1. The `gpu_env` settings are added unless the
  job's `--env` sets them.
- **Probe:** `job admission <host> start` runs a test scope with the same setup as the shim and checks that
  `memory.max`, `memory.swap.max` and `memory.oom.group` took effect on its `job/` and that `memory.peak`
  exists. If not, it refuses to start the loop, and the host stays without admission: there is no mode
  without limits.

### Ending, killing and clean-up

- `job kill` first records the job's use so far (its shim dies with it), then kills the pueue task and every
  process left in the scope with SIGKILL, as `pueue kill` does, so detached processes end at once.
- Processes a job leaves behind when its command ends are stopped by the shim within two seconds.
- Each cycle the loop stops any scope named `agent-pueue-job-<32 hex>` of one of its admitted jobs (in the queue
  or the records) that has started and still has processes after its task is Done (the shim was killed, for example by a `pueue kill` outside `job`), or after its task is absent in a
  status read that succeeded and this host has the job's shim script, unless the job started again after the
  status was read. A failed or timed-out pueue read never counts as "absent". Scopes with other names, and
  scopes of tasks pueue still runs, are never touched.
- A pilot past its time limit or a job past `--stop-at` is killed while Running; a paused one is killed once it
  is resumed. A kill that fails is tried again each cycle, with an ALERT. The wait ends `KILLED` with a note
  saying the loop stopped it and why (for a pilot: "as planned").

### Jobs that run containers

Processes started by a container engine run in the engine's cgroups, outside the job's scope: the scope's
limits, nice and per-job measurement don't reach them, and their use shows as outside use. A job that runs its
work in containers says `--in-containers`. It is counted at its size until its containers' use has had
`outside_window_s` to appear as outside use, then at zero, so its use is counted twice only for that window
(the safe direction). Deployments that want limits on container work set them in the container engine.

### Measurement

**GPU**, through NVML via `ctypes` (standard library), one pass per sample:

- Per card: total and free memory (`nvmlDeviceGetMemoryInfo`); used means `total − free`, so memory the driver
  reserves counts as outside use. The `nvidia-smi` fallback reads `memory.total` and `memory.free` too, so both
  paths agree. Name, UUID and utilization.
- Per process: compute and graphics process lists, merged by pid (they overlap), with each one's GPU memory.
  Utilization is the card's, not per process. When NVML fails, the loop reads `nvidia-smi` (compute processes
  only) and tries NVML again after 10 minutes.
- Because device and process values come from separate calls, the outside-use value of a sample is the lower of
  two consecutive passes, on both paths.
- Utilization for the busy rule is the average over the last minute.

**Per admitted job**, from its scope's `job/` cgroup: `memory.current`, `memory.stat`, `cpu.stat`. The RAM size basis is
`memory.current − active_file − inactive_file` (anonymous, shared and kernel memory; `/dev/shm` included, file
cache excluded). Per-process GPU values map to a job through `/proc/<pid>/cgroup`. The disk rate comes from
`/proc/<pid>/io` summed over the scope's processes (a soft value: short-lived children are missed).

**Per host:** busy threads from `/proc/stat` deltas; `MemAvailable`; memory pressure (`full avg10`); per-disk
busy time from `/proc/diskstats` deltas for the devices in `disks`.

### Room

For each resource, outside use is measured (what admitted jobs don't account for), and admitted jobs count at
the larger of their size and their current use. A job that ends frees its room at once.

- **GPU memory, per card:** `total − gpu_headroom − outside − Σ over admitted jobs of max(size on the card,
  current use on the card) − Σ over holds on the card of max(size, own use)`, where `outside` is the highest
  over `outside_window_s` of `max(0, used − jobs' use − holds' own use)`. A job's use counts on every card it
  uses, including one it was not given (it set `CUDA_VISIBLE_DEVICES` itself, for example), and that gets an
  ALERT. A hold's own use is the GPU memory of the
  processes it names (`--while-pid` and its descendants, or the cgroup of `--unit`/`--cgroup`); a hold that
  names none is counted at its size and its real use again as outside use (safe, but it wastes room), so
  services should name their processes.
- **RAM, per host:** the lowest over `outside_window_s` of `MemAvailable + Σ admitted jobs' current use` (what
  would be free with no admitted jobs), minus `ram_headroom`, minus `Σ over admitted jobs of max(size, current
  use)`. Nothing is released while memory pressure is over `memory_pressure_max_pct`.
- **CPU, per host** (CPU can be shared by time, so older jobs count at their measured use): `outside` is the
  average over `cpu_window_s` of busy threads minus the admitted jobs' measured threads. A job fits when
  `outside + Σ over jobs older than cpu_window_s of their average use over it + Σ over younger jobs of max(size,
  their use) + its CPU size ≤ cpu_busy_threads_max`, and the CPU sizes of admitted jobs including it total at
  most `placed_cores_max`.
- **GPU time, per card:** closed to new jobs when its average utilization over the last minute is over
  `gpu_busy_max_pct`, or the recorded GPU shares of its admitted jobs add up to more than that. These are
  fractions of time with any kernel running, not exact shares, so this is a soft rule.
- **Disk:** a job whose earlier run (its `--like` record) read or wrote faster than `disk_heavy_mb_s` at its
  peak is not released while the disk of its `--data` path is busier than `disk_busy_max_pct`, or while iowait
  is over `iowait_max_pct` and that disk is the busiest. A job's disk rate is the sum of `read_bytes +
  write_bytes` from `/proc/<pid>/io` over its processes between samples (the io controller is not delegated to
  user managers, so cgroup `io.stat` is not available).

Room a job is sized for but doesn't use is not given to others for memory, because whoever got it would fail
when the first job grows back. The record keeps the measured peaks, so the next `--like` uses the measured number.

### Fairness

Release is oldest first. Once the oldest held job has waited `fairness_wait_min`, a younger job is released only
if it takes nothing from a resource on which the oldest job is short; a resource is one GPU card, or one host's
RAM, CPU or disk. A held job does not block others when it is held for a reason other than room (its group is
full or paused), or when it would be short even with every admitted job on the host ended, because outside use
and holds alone leave too little.

### Records

`state_dir/admission.sqlite` (standard-library `sqlite3`). A submit writes a job's first row before it adds
the task to pueue; the loop writes the rest. The lock order is `queue.lock`, then the database: the loop
commits before it takes the lock.

- `jobs`: token, task, session, host, pueue id, group, submitted, released, started, ended, exit, how it ended
  (exit, killed, memory limit, host out of memory, stop-at, pilot time limit, wrapper error), size and where it
  came from (pilot, like, explicit, small), whether the peaks are final or sampled, constraints, GPU UUIDs, peaks
  (CPU, RAM basis, `memory.peak`, GPU memory, peak disk rate) and how many samples they come from, and the
  earlier job a `--like` size came from, the progress series (time, done, total, RAM; last 200 lines) and the
  projected end.
- `samples`: per-resource values, kept 7 days; `recent`: the loop's last samples in full.
- `holds`: id, name, card, size, start, end, and how it ended.

Peaks are kept for good. `job history [HOST ...] [--task T] [--session S] [--token X]` reads every admission
host's records over the same ssh channel as `job ls`: newest first, each with its size, peaks, how it ended
and its token (for `--like`).

### Alerts

The loop appends to `state_dir/alerts/<token>.jsonl`. `host_probe` returns alerts after the client's cursor; a
new wait starts its cursor at the current end, so reattaching doesn't replay old alerts. ALERT is for things the
owner must act on: the job's group is paused; the loop's heartbeat is stale; a GPU job uses more GPU memory than
its size and no cap holds it there (no new job goes on that card until it is back under); the job's RAM trend
reaches its hard limit before its `total`.

### Holds

A service that uses a GPU outside `job` (a model server, for example) registers a hold before it loads:
`job hold <host> --gpu <index|uuid> --mem SIZE --name NAME`, naming its processes with `--while-pid PID` (the
hold ends when that process exits), `--unit NAME` or `--cgroup PATH` (a service or container), and optionally
`--ttl MIN` (it ends unless renewed with `job hold --renew <id>`). A hold on a unit or cgroup also ends once that
unit or cgroup, seen before, is gone. Under the queue lock, a hold is granted only if the card has the room:
the loop writes its snapshot under the same lock after each cycle's releases, and the grant takes from that
room the holds granted since. In each cycle the loop reads the holds again under the lock before it releases,
and takes out of its room any hold granted since it measured. A refused hold is refused at once, and the
service decides what to do. A caller that wants room kept for a later step registers a hold the same way.
Holds and jobs are first come: neither displaces the other.

## Placement (client side)

`job run auto` asks every admission host for its room (a new host operation, `room`, which returns the loop's
snapshot, its age, and the room left after that cycle's releases, with the jobs held there listed). It filters hosts by the constraints and
orders them: hosts with room now, then the host that holds its `--data`, then the one with the fewest held jobs.
It submits with `fit_now` to each host with room in turn; the host checks under its queue lock, so two callers
can't both take the last room. A host that refuses (too small for the job, no record for its `--like`, a
missing `--needs-path`) is skipped. If no host takes it now, it submits normally to each host in the order until
one takes it, and the job is held there; a host whose loop is still measuring can hold a job but not start it.
The host is chosen before the submit's retries begin, and retries reuse the token, so a retry can't run the job
twice; a host that gives no reply stops the placement, since the job may have been queued there. Hosts that don't answer within the timeout, or whose snapshot is stale, are skipped and named
in the output.

`job room` prints, per host: snapshot age, busy threads, RAM room, memory pressure, each disk's busy time, and
per GPU its name, memory room, outside use, busy time and the jobs and holds on it, plus the held queue with each
reason. `--json` gives the same for scripts.

Not built yet: rented machines appear as hosts when a deployment's tooling writes a file for each into
`~/.config/agent-pueue/hosts.d/` (same format as the hosts file), read after the hosts file.

## Protocol and rollout

- `room` replies say whether the host is an admission host (`admission`); old clients never ask.
- Protocol 5 is a superset of 4. A new client sends a submit at 5 only when it carries a size or a scope;
  other submits go at 3 (or 4 for `--normal-priority`), as before. A host that predates protocol 5 refuses a
  sized submit ("protocol mismatch"), and a host without settings refuses one too, so a size is never silently
  ignored.
- An admission host refuses protocol 3 and 4 submits with a message that starts "protocol mismatch" (so old
  clients report it as a version problem) and says to update `job` on the submitting machine. The check is in
  `host_submit` itself, so it also applies to `local`, which skips `host_main`.
- Probes stay at protocol 3; an admission host adds held reasons and alerts to the reply.
- New operations (`room`, `hold`, `admission`, `history`) on a host that predates them fail with a usage error (exit 2,
  no JSON); `job room` shows that host as `its job predates admission`.
- Rollout: install the new `bin/job` on every machine first. No host has a settings file yet, so nothing changes.
  Then turn admission on one host at a time: add the settings file, start the loop, check it, then the next. No
  pueued restart is needed (a restart kills running jobs). Jobs already running in other groups keep running and
  count as outside use.

## What admission does not do

- It doesn't stop a caller from bypassing it: a process started with `pueue` or a shell directly shows up as
  outside use and counts as taken.
- It doesn't authenticate callers: the session name comes from the caller's environment, and records show it.
- The loop never kills a job because of what it measured. Jobs stop in three ways besides ending: the kernel
  enforces the hard RAM limit (`ram_hard_factor` times the size); the caller asked for a stop (`--pilot` time
  limit, `--stop-at`, `job kill`); or the job's command ended and its leftover processes are cleaned up.
- It doesn't move a held job to another host. A job held for a long time while another host has room shows that
  in its held line; the caller can kill it and submit again.
- It doesn't cap GPU memory by default. Consumer GPUs have no MIG; TASK-003.05 tests MPS and HAMi-core caps.
  Jobs whose GPU memory a cap or MPS server moves to another process are measured as outside use until that is
  settled.
- It covers one Unix user's jobs. Another user's jobs on the same host are outside use.
- It doesn't rent machines. Deployments do, and register them through `hosts.d`.

## Tests

As for the rest of `job`: unit tests with a throwaway pueued in a `mktemp -d` folder and faked measurement
inputs (a fake `/proc`, NVML results and cgroup tree under the test folder), covering: fit and no fit for each
resource; outside use, including a hold and another user's process on the same card; a finished job freeing its
room at once; a paused job keeping its room; CPU sharing by measured use; fairness, including a job held for a
non-room reason; holds by pid, unit and ttl; group limits and a paused group; `fit_now` with two concurrent
submits; a stale heartbeat; an invalid settings file; a scope left behind after the task ended, and a scope of
another name left alone; `--like` refusing a lower-bound run; and protocol 3/4/5 mixes in both directions. The
remote test runs against a real host with a real scope: a job over its hard RAM limit (exit record written,
verdict says memory limit), a `systemd-run` failure (wrapper error, not the job's exit), a job using `/dev/shm`
(counted in its size), the shim's final peaks for a clean exit, and `job kill` of a job with a detached child.
