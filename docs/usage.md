# Using `job`

`job` (the agent-pueue tool) runs a long job (a data run, a download, an
archive pass) under [pueue](https://github.com/Nukesor/pueue) on this machine
or a remote host, and waits on it correctly. It is one command an agent runs in
the background; the agent is woken once, when the job ends, with the real
outcome.

Pueue owns every process. `job` never finds a process by its name or command
line, so it cannot match itself, the way a hand-written `until ! pgrep -f
my_script.py` loop does when the loop's own command line contains the name.

## Setup

**Linux only**, on every machine involved: stall detection reads `/proc`, and
jobs run under `nice` and `ionice` (util-linux) unless `--normal-priority` is given.

On every machine that runs jobs:

1. Install pueue 4.x (for example its official release binaries in
   `~/.local/bin`) and keep its daemon running: `pueued -d`, or better a
   systemd user unit that runs `pueued`. To keep jobs running after you log
   out, enable linger (`loginctl enable-linger $USER`).
2. Copy `bin/job` to `~/.local/bin/job` (Python 3.8+, standard library only)
   and have `~/.local/bin` on your `PATH`. `job` finds `pueue` on the `PATH`,
   adding `~/.local/bin`.

On the machine the agents run on, list the remote hosts in
`~/.config/agent-pueue/hosts`, one per line: a name, then the ssh destination
if it differs from the name. `local` is always this machine.

```text
# name   ssh destination
gpu1                       # ssh gpu1
gpu2     me@gpu2.example.org
```

Then `./install.sh` copies `bin/job` to this machine and every listed host and
checks that every copy has the repo file's sha256. The client and the hosts
must run the same protocol version; a mismatch is refused with a message saying
to reinstall.

`--normal-priority` fails closed. A submit that sets it is sent with protocol
version 4; every other request, `job wait` included, is still sent with
version 3, and a host accepts both. A host that has not been updated accepts
only 3, so it refuses the priority submit with the protocol-mismatch error and
nothing is queued, instead of the job silently running at idle priority.
Running `job wait` processes keep working across an install. `job ls` does not
show a job's priority.

`job` also works when run on a host itself: there, `local` and the
machine's own host name (any case, short or full) both mean that machine. A
host without its own hosts file knows only `local` and its own name.

Environment variables:

| Variable | Effect |
|---|---|
| `JOB_HOSTS_FILE` | Use this hosts file instead of `~/.config/agent-pueue/hosts`. |
| `JOB_PUEUE_CONFIG` | Pass `-c <file>` to pueue on this machine (a non-default pueue config). |
| `JOB_SESSION` | The session name `job ls` shows (see "Sessions"). |
| `JOB_REMOTE_PUEUE_CONFIG` | Tests only: the pueue config the remote side uses. |

## The one command: `job run`

```bash
job run <host> <task-id> [--cwd DIR] [--group G] [--env NAME[=VALUE]] [--script FILE]
        [--normal-priority] [--stall MIN] [--stall-cpu PCT] [--check SCRIPT] [--poll SEC] [--tail N] -- <command>
```

- `<host>` is `local` or a name from the hosts file.
- `<task-id>` is the task the job belongs to in your tracker (for example
  `TASK-12`). It becomes the pueue label and shows in `job ls`.
- Everything after `--` is the command. One argument is a shell command line
  (`-- 'python run.py --month 2026-10 > out.txt'`); several arguments are one
  argv, quoted for you. For anything long or multi-line, put it in a file and
  use `--script FILE` instead of `--`. The file is run by bash whatever its
  first line says; to run a Python file, give `-- 'python3 -u file.py'`.
- `--cwd DIR` is the working directory on the host. Default: the current
  directory for `local`, the home directory for a remote host. Give it for remote hosts.
- `--normal-priority` (also on `job submit`) runs the job without `nice` and
  `ionice`, so it has the CPU and disk priority pueued itself runs at (normally
  the default) instead of `nice -n 19 ionice -c 3`. Use it only
  when a project's rule says its jobs must not run at idle priority. Without
  the option nothing changes.
- The command runs in bash with `set -o pipefail`, so `python run.py 2>&1 | tee
  out.log` fails when `python` fails. There is no `set -e`: a multi-line
  command's exit code is its last command's. Chain steps with `&&`, or start
  the script with `set -e`.
- Python buffers its output when it writes to a file. Use `python -u` (or
  `--env PYTHONUNBUFFERED=1`) so the log, `--check` and stall detection see
  output as it is written.

Run it with the Bash tool's `run_in_background: true`. Do not add `&`, a
`pgrep` loop, a log grep or a PID file: the background command's exit is the
wake-up.

```bash
job run gpu1 TASK-12 --cwd /data/runs -- 'python -u bootstrap.py --n 500'
```

On start it prints (to stderr) the job's id and the exact command to reattach:

```text
job: queued gpu1#14 (TASK-12); waiting. If this wait is lost, reattach with: job wait gpu1 14 --token 5b94...
```

When it ends it prints the outcome and exits with the job's own exit code:

```text
job: FAILED gpu1#14 (TASK-12) exit 3 after 2.1 h
status: Done / Failed
started: 2026-11-05 14:52:04 ET   ended: 2026-11-05 17:00:11 ET
command: python -u bootstrap.py --n 500
log: gpu1:/home/me/.local/share/pueue/task_logs/14.log
--- last 40 log lines ---
...
--- end of log tail ---
job: exit 3
```

**Read the first line.** It is the verdict. The exit code alone can be
ambiguous: a job that itself exits 200 looks like `STALLED` to anyone reading
only the code. The last line repeats the exit code.

## Verdicts and exit codes

| Verdict | Exit code | Meaning | Job still running? |
|---|---|---|---|
| `SUCCESS` | 0 | The command exited 0. | no |
| `FAILED` | the job's own code | The command exited nonzero. A job killed by a signal from outside (the OOM killer, `kill -9`) ends here with code 128 + signal, usually 137. | no |
| `KILLED` | 137 | Killed with `job kill` or `pueue kill`, or the pueue daemon was restarted while it ran. | no |
| `FAILED_TO_SPAWN` | 127 | pueue could not start the command. | no |
| `ERRORED`, `DEPENDENCY_FAILED` | 203 | pueue-side error, or a dependency failed. | no |
| `STALLED` | 200 | See "Stall detection". | **yes** |
| `ALERT` | 201 | The `--check` script failed on new output; its message is printed. | **yes**, unless it had just finished |
| `UNREACHABLE` | 202 | For 15 minutes `job` could not get the job's state: the host was unreachable over ssh, or pueue there did not answer. Shorter gaps are retried quietly. | probably |
| `GONE` | 204 | The pueue task no longer exists, or its id now belongs to another job. | no |
| `WRAPPER_ERROR` | 205 | `job` could not do what was asked (bad group, missing directory, bad `--check` path, pueue down at submit time, client and host versions differ, or an admitted job's systemd scope or memory limit could not be set up, so the job never ran); the message says what. If a `note:` line says the job was queued, it was, and keeps running; otherwise it was not queued. | see the message |

After `STALLED`, `ALERT` or `UNREACHABLE` the job is left alone. Look at the
log, then wait again or stop it, with the command the output prints
(`job wait <host> <id> --token T` / `job kill <host> <id> --token T`).

A pueue daemon restart (the machine rebooted, or the service was restarted)
ends its running jobs; they show as `KILLED`. Without linger, logging out also
stops a user service, and with it the daemon and its jobs.

If a job's pueue entry is removed after it ended (`pueue clean`), `job wait`
still reports its real verdict and exit code from the job's own exit record
(`~/.local/state/agent-pueue/results/<token>.exit`), with a note that the log is
gone. `GONE` means there is no record either: the job was removed before it
ran, or was killed outright.

## Stall detection

While the job runs, `job` watches two things on the host: the size of the
job's pueue log, and the CPU time used by the job's processes. The processes
are found by a random token `job submit` puts in the job's environment, plus
every descendant of those processes; nothing is matched by name. CPU time of
children that have already exited is counted too.

The wait ends with `STALLED` when, over the last `--stall` minutes (default 30),
the log did not grow **and** the job used less than `--stall-cpu` percent of
one CPU core (default 0.5). A hung job that still wakes now and then (a wait
loop, a logging thread) uses far less than that and is caught; a job that
computes silently uses far more and is not.

- `--stall 0` turns stall detection off.
- A job waiting in its group's queue, or paused, does not stall.
- A job whose work runs **outside its own process tree** looks idle: `docker
  run`, `ssh other-host ...`, `systemd-run --user --wait`, `srun`, anything that
  hands work to a daemon. If such a job also writes nothing to its log for the
  stall time, give it a larger `--stall` or `--stall 0`.
- A job that legitimately sleeps or waits on the network longer than the stall
  time will be reported; give it a larger `--stall`.
- On a busy machine a niced job can get very little CPU; a process that is
  running or waiting for a CPU at any probe in the window counts as active.
- A job started with plain `pueue add` (not through `job`) has no token, so its
  processes cannot be found; `job wait` turns stall detection off for it and
  says so.

## Output checks: `--check SCRIPT`

`SCRIPT` is an executable on the machine where you run `job` (not on the
remote host). Each time the job's log grows, the new **whole lines** are piped to it
on stdin, with `JOB_HOST` and `JOB_ID` set. A line ends at a newline or a
carriage return (progress bars); an unfinished last line is held back until it
is finished (or until the job ends). Exit 0 means fine; any other
exit ends the wait with `ALERT` and prints the script's output. Output written
just before the job ends is checked before the final verdict.

```bash
cat > "$SCRATCH/nan-check.sh" <<'EOF'
#!/bin/sh
if grep -qE 'nan|Traceback'; then echo "NaN or traceback in output"; exit 1; fi
EOF
chmod +x "$SCRATCH/nan-check.sh"
job run gpu1 TASK-12 --check "$SCRATCH/nan-check.sh" -- 'python train.py'
```

- `job run --check` checks all of the job's output.
- `job wait --check` checks only output written after the wait starts, so
  waiting again after an `ALERT` does not trip on the same line. Add
  `--from-start` to check everything.
- `job` refuses to start if the script does not exist or is not executable.
- When an `ALERT` comes from the job's last output, the report also gives the
  job's own end (`the job has also ended: FAILED exit 9`).

## Other commands

| Command | What it does |
|---|---|
| `job submit <host> <task-id> [--group G] [--cwd DIR] [--normal-priority] -- <command>` | Queue only. Prints the pueue id on stdout and, on stderr, the exact `job wait ... --token T` to wait on it. |
| `job wait <host> <id> [--token T] [--from-start] [--stall MIN] [--check SCRIPT] [--poll SEC] [--tail N]` | Wait on a job queued earlier, or one that already finished; same output and exit code as `job run`. With `--token`, a pueue id that now belongs to another job (pueue reuses ids after `pueue clean`) ends with `GONE` instead of waiting on the wrong job. |
| `job wait <host> <id> <id> ...` | Wait on several jobs on one host at once. Ends when all have ended (first line `job: ALL ENDED ...`, exit 0 if all succeeded, else the exit code of the lowest-id job that did not), or as soon as one of them is `STALLED`, `ALERT`, `UNREACHABLE` or `GONE` (first line names it and lists the ids still running). |
| `job ls [host ...] [--all] [--recent H]` | Every job on this machine and every host in the hosts file: host, id, task id, status, run time, group, session, command. Finished jobs older than `--recent` hours (default 24) are hidden unless `--all`. Exits 202 if a host could not be reached (the reachable ones are still listed). |
| `job group <host> <name> <n>` | Create a pueue group (if missing) and let it run `n` jobs at a time. Then `job submit/run ... --group <name>`. |
| `job room [host ...] [--json]` | Each admission host's room: CPU, RAM, memory pressure, disks, each GPU's free memory and outside use, the jobs running and the jobs held with their reasons. |
| `job kill <host> <id> [--token T]` | Kill a running job (pueue kill), or take a queued one out of the queue (pueue remove). With `--token`, refuses if the id now belongs to another job. |

## Queues of jobs

A queue of jobs is a pueue group, not a hand-written queue script:

```bash
job group gpu1 pilot 2
for m in 2026-08 2026-09 2026-10; do
  job submit gpu1 TASK-12 --group pilot --cwd /data/runs -- "python -u pilot.py --month $m"
done
```

`submit` prints each id. Then wait on all of them in **one** background call,
`job wait gpu1 <id1> <id2> <id3>`. Do not wait on only the last id: in a group
that runs several at a time, the last id can finish first.

**The `default` group runs one job at a time** (pueue's default). A job submitted to it while another job runs there, yours or another
session's, waits its turn; `job run` says so on stderr (`queued behind 1
running job(s) in group default (limit 1)`). Use your own group for work that
should run alongside others. `job group <host> default N` raises the shared
limit for everyone; don't change it without a reason.

A queued or paused job waits without limit and does not count as stalled;
`job ls` shows it as `Queued` or `Paused`.

## Admission hosts: jobs start only where they fit

A host with an admission settings file (`~/.config/agent-pueue/admission.json`) runs an admission loop
(`job _admit`) that measures what is in use, including processes `job` did not start, and starts each job only
when it fits. The design is [admission.md](admission.md). On such a host:

- **Every job needs a size**, one of:
  - `--pilot`: a first run to measure the job. It gets all the free RAM (and, with `--gpus N`, all the free
    memory on those cards) and stops at its time limit (`--max-minutes`, or the host's setting).
    It takes `--cpus N` and `--gpus N`, not `--ram`. Its time limit counts from its start.
  - `--like TOKEN` (or `HOST#ID`): sized from that earlier job's measured peaks plus margins, printed as
    `sized like ...`, and run on the host that holds that record. Refused when that run was killed or
    crashed (its peaks are only a lower bound) or was measured fewer than 3 times.
  - `--ram SIZE` (required) with `--cpus N`, `--gpus N`, `--gpu-mem SIZE` (per GPU), `--gpu-share PCT`; or
    `--small`. Sizes: `20G` is 20×10⁹ bytes, `512Mi` is 512×2²⁰.
- `job run auto <task-id> ...` lets `job` choose the host: one with room now, else the job waits (is held) on
  the first host that takes it. A host that refuses the job (too small, no record for `--like`, a missing
  `--needs-path`) is skipped; an unreachable host stops the placement, since the job may have been queued
  there. `--host NAME` (repeatable) limits the choice.
- A held job's wait prints why every 10 minutes (`held: needs 18.0 GB RAM, 9.4 GB free`); being held is not a
  verdict. `job ls` shows it as `Held` with the reason; `job room` shows every host's room and held jobs.
- The job runs in its own systemd scope with a hard RAM limit (its size times the host's `ram_hard_factor`, or
  for a pilot its free RAM at release), no swap, and whole-job kill. A job over its limit ends `FAILED 137` with
  `note: it went over its RAM limit of N GB; the whole job was stopped`. Processes it leaves running when its
  command ends are stopped.
- GPUs are given by UUID in `CUDA_VISIBLE_DEVICES` (also in `JOB_GPU_UUIDS`); a job without `--gpus` sees none.
  `--env CUDA_VISIBLE_DEVICES=...` is refused. Code should use devices 0..N-1.
- `ALERT` (exit 201) can also come from the loop: the job's group is paused, it uses more GPU memory than its
  size, or the host's loop has stopped while the job is held. The job keeps its place.
- Without `--group`, admitted jobs run in the group `sched`, so they start as soon as they fit.
- Other constraints: `--gpu-model TEXT`, `--needs-path PATH`, `--data PATH` (its disk; heavy-disk jobs wait
  while that disk is busy), `--stop-at TIME` (with its UTC offset; stopped then, dropped if still held),
  `--in-containers`, `--restartable`.
- After the loop starts with no recent history (a reboot, a long stop), it measures the host for `warmup_s`
  (10 minutes by default) before it starts anything; `job room` shows `measuring the host first`.
- A job may append `done total` lines (for example `120 6264`) to the file in `$JOB_PROGRESS`. It then gets a
  projected end (`job ls` shows `[120 of 6264, ends about ...]`), and an early `ALERT` if its RAM grows with
  its progress fast enough to reach its RAM limit before the end.
- Restarting an admitted job in place (`pueue restart --in-place`) ends it `WRAPPER_ERROR` (its assignment is
  gone); submit it again instead.
- A submit without a size, or from an older `job` (protocol 3 or 4), is refused with a message that starts
  `protocol mismatch` or names the size options.
- A service that uses a GPU outside `job` (a model server) registers its GPU memory before it loads:
  `job hold HOST --gpu 0 --mem 18G --name NAME` plus `--while-pid PID`, `--unit NAME` or `--cgroup PATH` for
  its processes, and optionally `--ttl MIN` (renew with `job hold HOST --renew ID`). It prints the hold id;
  `job release HOST ID` ends it. A hold is granted only if the card has the room now, else refused at once.
  Holds and jobs are first come.
- `job admission HOST start|stop|status` installs and starts, stops, or shows the host's admission loop
  service (`agent-pueue-admit.service`, a systemd user unit).

## The job's environment

pueue stores each job's environment in its state file, so `job` passes only a
small fixed set of variables from the submitting shell (`PATH`, `HOME`, `USER`,
`LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TZ`, `TERM`, `TMPDIR`, the `XDG_*`
directories), plus its own `JOB_TOKEN`, `JOB_TASK`, `JOB_SESSION`,
`JOB_SUMMARY`, `JOB_SUBMITTED_FROM`. Pass anything else explicitly:
`--env CUDA_VISIBLE_DEVICES=1`, or `--env NAME` to pass this shell's value.
Activate conda or virtual environments inside the command itself.

## Sessions

`job ls` shows which session started each job. It takes the value from
`JOB_SESSION` if set, otherwise from `CLAUDE_CODE_HOST_SESSION_ID` or
`CLAUDE_CODE_SESSION_ID` (set by Claude Code), otherwise `unknown`.

## What runs on the host

- The command is written to a script file on the host,
  `~/.local/state/agent-pueue/scripts/<token>.sh` (kept 30 days), sent over ssh
  stdin. Nothing the agent typed goes into the ssh command line.
- The script starts with `export PATH="$HOME/.local/bin:$PATH"`.
- pueue runs it as `nice -n 19 ionice -c 3 bash <script>`, on every host,
  `local` included; with `--normal-priority` it runs as `bash <script>`.
- pueue keeps each job's command and environment in its state file, and `job ls`
  shows the first 60 characters of each command. Do not put secrets in command
  lines; pass them through a file the command reads.
- If pueue has a finish callback configured (for example a push notification),
  it fires for every job end, whether or not anyone is waiting.

## Polling

`job` asks for the job's state every 3 s on `local` and every 20 s on a remote
host (`--poll SEC` changes it). Remote calls reuse one ssh connection per
host (`ControlMaster`, socket `~/.ssh/job-cm-*`, kept 5 minutes). A failed call
is retried; after 15 minutes without an answer the wait ends with
`UNREACHABLE` and the job keeps running. Times in the output are in the local
time zone of the machine where `job` runs.

## Tests

```bash
python3 -m unittest discover -s tests -v
JOB_TEST_HOST=gpu1 python3 -m unittest tests.test_remote -v
```

The local tests start throwaway pueued daemons in `mktemp -d` folders under
`/tmp`. The remote tests start one on the host in
`mktemp -d $JOB_TEST_REMOTE_TMP/agent-XXXXXX` (default `/tmp`); with
`JOB_TEST_REAL_DAEMON=1` they also run two jobs on the host's real pueued.
