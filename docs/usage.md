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

Run it with the Bash tool's `run_in_background: true`. Do not add `&`, a pipe,
a `pgrep` loop, a log grep or a PID file: the background command's exit is the
wake-up. `job --help` lists the rules agents most often miss.

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
job: FAILED gpu1#14 exit 3
```

**Read the first line.** It is the verdict. The exit code alone can be
ambiguous: a job that itself exits 200 looks like `STALLED` to anyone reading
only the code. The last line repeats the verdict, the job and the exit code
(`job: FAILED gpu1#14 exit 3`), so it survives a cut-off output too.

**Never pipe `job run` or `job wait`** (`| tail`, `| head`, `| grep`). The
pipe's exit code replaces the job's, so a failed job reads as exit 0, and
`head` or `tail` can cut off the verdict. For fewer log lines use `--tail N`.

## Verdicts and exit codes

| Verdict | Exit code | Meaning | Job still running? |
|---|---|---|---|
| `SUCCESS` | 0 | The command exited 0. | no |
| `FAILED` | the job's own code | The command exited nonzero. A job killed by a signal from outside (the OOM killer, `kill -9`) ends here with code 128 + signal, usually 137. | no |
| `KILLED` | 137 | Killed with `job kill` or `pueue kill`, or removed from the queue with `job kill` before it ran (the note says so). | no |
| `KILLED_BY_RESTART` | 208 | It was still running when the pueue daemon stopped (the machine rebooted, or the service was restarted or died); see below. Submit it again. | no |
| `UNTRACKED` | 209 | The pueue daemon died without stopping it (killed outright, out of memory, a crash), so pueue lists it as killed, but its command still runs; the note lists its pids. Do not submit it again. Either wait for it with the `job wait <host> <id> --token T` the note prints (a wait after an `UNTRACKED` report follows its processes and ends with its real outcome), or stop it with the `job kill <host> <id> --token T` the note prints (a wait on it then ends `KILLED`). | **yes** |
| `FAILED_TO_SPAWN` | 127 | pueue could not start the command. | no |
| `ERRORED`, `DEPENDENCY_FAILED` | 203 | pueue-side error, or a dependency failed. | no |
| `STALLED` | 200 | See "Stall detection". | **yes** |
| `ALERT` | 201 | The `--check` script failed on new output; its message is printed. | **yes**, unless it had just finished |
| `UNREACHABLE` | 202 | For 15 minutes `job` could not get the job's state: the host was unreachable over ssh, or pueue there did not answer. Shorter gaps are retried quietly. | probably |
| `GONE` | 204 | The pueue task no longer exists, or its id now belongs to another job. | no |
| `WRAPPER_ERROR` | 205 | `job` could not do what was asked (bad group, missing directory, bad `--check` path, pueue down at submit time (pueue's own error, in one line), client and host versions differ, or an admitted job's systemd scope or memory limit could not be set up, so the job never ran); the message says what. If a `note:` line says the job was queued, it was, and keeps running; otherwise it was not queued. | see the message |
| `PILOT_TIME_LIMIT` | 207 | A `--pilot` on an admission host was stopped at its time limit, as planned. Its peaks are recorded; size the next run with `--like`. | no |
| `NO_ROOM` | 206 | `job hold` only: the host has no room for the hold now. Any other failure of `job hold` (not an admission host, its loop stopped or still measuring, unreachable) exits as above, so a service refuses its load on 206 and otherwise falls back to its own check. | the service decides |

After `STALLED`, `ALERT` or `UNREACHABLE` the job is left alone. Look at the
log, then wait again or stop it, with the command the output prints
(`job wait <host> <id> --token T` / `job kill <host> <id> --token T`).

A pueue daemon restart (the machine rebooted, or the service was restarted)
ends its running jobs; they end `KILLED_BY_RESTART` (exit 208), with a note
saying when the daemon started again. pueue records no reason for a kill, so
`job` reads the restart from the evidence the stop itself leaves in pueue's
task times. The job was killed not by `job kill`, started before the daemon
now running (found from its pid file), and either:

- ended at or after that start, within two minutes: the daemon died without
  stopping it (killed outright, out of memory, a crash), and the new daemon
  marked it killed when it started; or
- ended before that start, within 5 s of the last task event (a job queued,
  started or ended) before it: a clean stop kills every running job at once,
  and nothing happens after it until the daemon starts again.

Blind spots: a kill from outside `job kill` (systemd-oomd, a scope kill, a kill
by hand, or a `job kill` from a version that kept no record of its kills) that was the last thing to happen on
the daemon before a clean stop, with no job queued, started or ended between
them, is reported `KILLED_BY_RESTART`. A stop that took longer than 5 s to kill
its jobs reports the ones it killed first as `KILLED`.

The restart that can have killed a job is the first daemon start after the job
started. `job` records each daemon start it sees (any `job` command on that
host, in `~/.local/state/agent-pueue/daemon-starts`), so a job killed by one
restart is still known as one after a later restart. Blind spot: two restarts
with no `job` command on that host between them; a job killed by the first is
then judged against the second, and is usually reported `KILLED`.

A daemon that died outright does not kill its jobs: their processes live on,
unknown to pueue. Before giving `KILLED_BY_RESTART`, `job` looks for the job's
processes by its token (as `job kill` does) and for the job's own exit record:

- its command still runs: `UNTRACKED` (exit 209) with their pids, so the job is
  never started twice. The note offers both: wait for it (`job wait` with its
  token; a wait after the `UNTRACKED` report follows its processes like a
  running job, with stall detection and `--check`, and ends with its real
  outcome), or stop it (`job kill`; the wait then ends `KILLED`). `job wait
  --mine` keeps such a job until its processes are gone and its outcome was
  reported, however long ago pueue lost it.
- its command ended on its own (its exit record was written after it started):
  its real verdict (`SUCCESS`, `FAILED` with its code), with a note that pueue
  lost track of it, and the pids of any process it left running.
- its processes ended later without an exit record: `KILLED`, with a note that
  something outside `job` killed them (not the restart); read its log first.

A host sends such a job as killed by `daemon-lost`, not `daemon`, so a `job`
from before this change reports it `KILLED`, never "submit it again". A
restart also pauses the groups that had running
jobs: a wait on a job queued or paused in a paused group ends with `ALERT` and
the exact `job resume` command (the job stays queued). Without linger, logging
out also stops a user service, and with it the daemon and its jobs.

### After a restart of this machine or the agent app

Every wait ends when the agent app or the machine restarts; the jobs on other
hosts keep running. Reattach with **one** background call:

```bash
job wait --mine
```

It waits on every unfinished job (queued, held, running) this session
submitted, on this machine and every host in the hosts file, with the same
verdicts and early end as `job wait` on several ids. It also reports, with its
verdict, each of the session's finished jobs its host still lists that no
wait has reported, whatever its age (a wait records each job it reports, in
`~/.local/state/agent-pueue/reported/`, and marks it delivered on its host,
which keeps it listed until then; the marks go to all hosts at once, and the
wait waits for them 5 s at most in all; a host older than that mark is asked
about the last 24 h only): the jobs that ended
while no one waited, and each job pueue lost track of whose command still runs
(see above). A job counts as reported only once the whole report was written
out; a job left running (`STALLED`, `ALERT`, `UNREACHABLE`, `UNTRACKED`) is not
counted, even when it ended right after. Jobs that already ended badly end the wait at once; the
exit code is the worst verdict's (`UNTRACKED` first, then `WRAPPER_ERROR`,
`GONE`, `UNREACHABLE`, `STALLED`, `ALERT`, `KILLED_BY_RESTART`, then the
others). `job ls --all` also lists every job whose outcome no wait reported
(see "session group" below). Marks older than 30 days are pruned. The mark on
the host is set best effort, after the report, waiting at most 5 s for a host,
so a hung host never holds the wait's exit. A host that cannot be listed is named on stderr at
once and in a `note:` line at the end (exit 202 if nothing else was found).
`job ls` shows each job's token, for `job wait <host> <id> --token T`. A
`--check` script given to `job run`, or to `job wait` with the job's token
(`--token` or `ID:TOKEN`), is kept with the job (a copy in
`~/.local/state/agent-pueue/checks/<token>`, on the machine that waits, for 30
days), so a later `job wait` without `--check` still runs it, even when its
original was in a temporary folder that is gone.

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
| `job wait <host> <id>[:<token>] <id>[:<token>] ...` | Wait on several jobs on one host at once. Ends as soon as one of them ends badly (anything but `SUCCESS` or `PILOT_TIME_LIMIT`: `FAILED`, `KILLED`, `STALLED`, `ALERT`, `UNREACHABLE`, `GONE`, ...); the first line names it, says how many of the jobs really ended (a `STALLED` or `ALERT` job still runs, so it is not counted) and which have not, and a line gives the exact command to keep waiting on the others (`job wait <host> 13:<token> 14:<token>`). Otherwise it ends when all have ended (first line `job: ALL ENDED ...`, exit 0 if all succeeded, else the exit code of the lowest-id job that did not). `ID:TOKEN` refuses an id that now belongs to another job, like `--token` does for a single id; `--token` with several ids is refused (exit 205). |
| `job ls [host ...] [--all] [--recent H]` | Every job on this machine and every host in the hosts file: host, id, task id, status, run time, group, session, token, command. Finished jobs older than `--recent` hours (default 24) are hidden unless `--all`. Exits 202 if a host could not be reached (the reachable ones are still listed). |
| `job group <host> <name> <n>` | Create a pueue group (if missing) and let it run `n` jobs at a time. Then `job submit/run ... --group <name>`. |
| `job room [host ...] [--json]` | Each admission host's room: CPU, RAM, memory pressure (leaving out cgroups stalled against their own RAM limits, never above the system-wide figure, and where it is highest), disks, each GPU's free memory and outside use, the jobs running and the jobs held with their reasons. |
| `job log <host> <id> [--token T] [--lines N] [--grep RE] [--full]` | The job's output: its last N lines (default 50; N is at least 1), the last N lines matching the regular expression RE, or the whole log (up to its last 32 MB). Use it instead of `pueue log` or reading the log file over ssh; it finds the log the way `job` does, whatever the host's PATH. |
| `job show <host> <id> [--token T]` | Everything about one job: task, state (with the verdict once it ended), group and whether it is paused, session, token, working directory, times, size and RAM limit on admission hosts, the log's path, and the full command. |
| `job resume <host> --group G` | Start a paused group again (`pueue start --group G`). A pueue daemon restart leaves the groups that had running jobs paused; a wait on a job queued there ends with an `ALERT` naming this command. |
| `job kill <host> <id> [--token T]` | Kill a running job, or remove a queued or held one from the queue. A running job's whole process tree ends: pueue's kill, then SIGKILL to every process of the job still alive (found by its token and as descendants, so children in their own process group, like `timeout`'s, or that called `setsid`, end too). It prints how many processes were running and whether any is left (exit 205 if one is, with a last `job: WRAPPER_ERROR` line). It also ends the processes of an `UNTRACKED` job (one pueue lost track of whose processes still run), and nothing of a job that ended otherwise: a process a finished job left running on purpose is not its to kill. A `--token` that is not a 32-character job token is refused at once. It cannot reach a process that cleared its environment (`env -i`) and whose parent then exited, so it is no longer anyone's descendant; for a job in a systemd scope (admission hosts), ending the scope reaches that one too. A wait on a job removed before it ran ends `KILLED` with `removed from the queue by job kill before it ran`. With `--token`, refuses if the id now belongs to another job. |
| `job kill <host> --task NAME [--all-sessions]` | The same for every unfinished job of task id NAME on the host, `UNTRACKED` ones included: queued and held ones first, so killing a running one cannot start them. Refused, killing nothing, when any of them belongs to another session, unless `--all-sessions`. |

## Queues of jobs

A queue of jobs is a pueue group, not a hand-written queue script:

```bash
job group gpu1 pilot 2
for m in 2026-08 2026-09 2026-10; do
  job submit gpu1 TASK-12 --group pilot --cwd /data/runs -- "python -u pilot.py --month $m"
done
```

`submit` prints each id. Then wait on all of them in **one** background call,
`job wait gpu1 <id1> <id2> <id3>` (or `<id>:<token>` each). The wait ends as soon as one of them fails, so you
hear of it at once, and prints the command that waits on the rest. Do not wait on only the last id: in a group
that runs several at a time, the last id can finish first.

**A job without `--group` runs in its session's own group** (`session-<session id>`, created when needed), which
runs as many jobs at a time as the host's `default` group had when the session group was created (one, unless the
host's owner raised it; a later change to `default` does not change it, use `job group` for that). So your jobs
without a group never wait behind another session's jobs. A job submitted while that many of yours run there waits
its turn; `job run` says so on stderr (`queued behind 1 running job(s) in group session-... (limit 1)`). Use a
group of your own (`job group`) for a different limit. A session group is removed, by any later submit on that
host, once no submit has used it for an hour, every job in it ended over 24 h ago, a wait reported each job's
outcome, and no process of any of them still runs; those finished jobs are removed from pueue's list with it (their
logs go too; their exit codes stay in `job`'s records for 30 days, so `job wait` still reports them). A job whose
outcome no wait reported stays in pueue's list (and `job ls --all`) until one does. Jobs of a session without a session id (see "Sessions") share `session-unknown`. On
admission hosts, a job without `--group` runs in the group `sched` instead (see below). pueue's `default` group
is used only when you name it (`--group default`); it runs one job at a time and is shared by everyone.

A queued or paused job waits without limit and does not count as stalled;
`job ls` shows it as `Queued` or `Paused`.

## Admission hosts: jobs start only where they fit

A host with an admission settings file (`~/.config/agent-pueue/admission.json`) runs an admission loop
(`job _admit`) that measures what is in use, including processes `job` did not start, and starts each job only
when it fits. The design is [admission.md](admission.md). On such a host:

- **Every job needs a size**, one of:
  - `--pilot`: a first run to measure the job. It starts with all the free RAM (and, with `--gpus N`, all the free
    memory on those cards); after a few minutes (`pilot.shrink_after_min`) the loop cuts it to its measured peak
    times `pilot.shrink_margin` (and raises it again if it presses against that while the host has room). It stops
    at its time limit (`--max-minutes`, or the host's setting), never over the host's ceiling
    (`pilot.max_minutes_ceiling`, 60 by default; a larger `--max-minutes` is refused). A pilot only measures:
    run the job itself with `--like` after it.
    It takes `--cpus N` and `--gpus N`, not `--ram`. Its time limit counts from its start. It starts only when
    the host has at least `pilot.ram_floor_gb` (16 GB by default) of RAM room, so it is not limited to a few GB.
  - `--like TOKEN` (or `HOST#ID`): sized from that earlier job's measured peaks plus margins, printed as
    `sized like ...`, and run on the host that holds that record. Refused when that run was killed or
    crashed (its peaks are only a lower bound) or was measured fewer than 3 times.
  - `--like last`: sized from the newest run of the same task id on that host that exited 0, or, only when none
    did, from the newest pilot of it that reached its time limit (it says so); it prints which run (with its
    session) and its peaks. Runs of the same session come first; another session's run of the same task id is
    used only when the session has none.
  - `--ram SIZE` (required, at least 16 MiB) with `--cpus N` (more than 0), `--gpus N`, `--gpu-mem SIZE` (per
    GPU), `--gpu-share PCT`; or `--small`. Sizes: `20G` is 20×10⁹ bytes, `512Mi` is 512×2²⁰.
- `job run auto <task-id> ...` lets `job` choose the host: one with room now, else the job waits (is held) on
  the first host that takes it. A host that refuses the job (too small, no record for `--like`, a missing
  `--needs-path`) is skipped; an unreachable host stops the placement, since the job may have been queued
  there. `--host NAME` (repeatable) limits the choice.
- A held job's wait prints why every 10 minutes (`held: needs 22.5 GB RAM (its limit, 1.5 times its size), 9.4 GB
  free; ...`); being held is not a verdict. A job needs room for its hard RAM limit, not just its size. `job ls` shows it as `Held` with the reason; `job room` shows every host's room and held jobs.
  The first held line, printed at submit, already gives the reason, from the loop's last measurement (or says
  the loop is still measuring the host). A job held behind an older one names it (task id and token) and what it
  needs. When the running jobs holding the room it lacks have projected ends (see `$JOB_PROGRESS` below), the
  reason says when enough room should free (`enough room should free about ...`); otherwise it says no start
  estimate is known, and why.
  A job held short of RAM whose size was not measured gets one extra line, once: a size below what it needs ends in
  exit 137, so size it from a measured run (`--pilot`, then `--like`). Don't shrink a held job's size below what it
  needs to get it started.
- Release is oldest first. Once the oldest held job has waited `fairness_wait_min` (30 minutes), younger jobs that
  need the room it lacks wait behind it, except a small one: one counted at under a tenth
  (`fairness_small_fraction`) of what the oldest still lacks of each resource they share.
- `job resize HOST ID [--token T] [--ram SIZE] [--cpus N]` (at least one of the two) changes an admitted job's
  size. A held job may take any size the host could run, keeps its place in the queue, and is refused a RAM size
  below the highest RAM peak of an earlier finished run of the same task id there (of the same session's runs when
  it has any; the reply names the run and its session). A running job may only shrink: its RAM limit is lowered at
  once and it is counted at the new limit from the loop's next cycle, so held jobs can use the room freed. The new
  limit must cover what the job cannot give back: its measured peak times the host's `resize_margin` (default
  1.25), its use now less the file cache a lower limit drops (not its memory-mapped hot pages), and the kernel's
  peak of its cgroup (`memory.peak`), less that file cache only when the loop's sampling saw that peak (file cache
  is dropped at a lower limit, so a job that streams data through its limit can still shrink; growth in its own
  memory, and a spike between samples, cannot). A job stalled reclaiming memory in its own cgroup (over 1% of the
  last minute) or reading dropped file pages back in is not shrunk, and the refusal says which. Its RAM limit is never raised, so a `--cpus`-only resize keeps the limit it runs under, and
  `--cpus` may not go under its measured CPU peak. A refused or failed resize changes nothing. A pilot is not
  resized by hand (the loop shrinks it), and a job the loop is releasing at that moment is refused with "try again". `job history` shows the
  size a job ended with, and the size it was resized from.
- **The loop resizes running jobs itself** (`auto_resize`, on by default); nothing is asked of the owner:
  - A job that uses well under its size is shrunk, after 30 minutes (`oversize_after_min`; a pilot after
    `pilot.shrink_after_min`), to the size `--like` would give it, with the checks `job resize` makes, when that
    lowers its limit by at least a fifth (`auto_shrink_min_saving`). A job whose RAM trend (its `$JOB_PROGRESS`
    lines) is still rising is sized by where the trend ends.
  - A job that presses against its RAM limit (uses 90% of it, file cache excluded, stalls against it, or its RAM
    trend is on course to reach it) gets its limit raised by half, or to what it needs if more, as far as the
    host's RAM room allows. Only when there is no room does it get an undersized `ALERT` (below).
  - `job history` shows the size a job ended with and each size it was resized from.
- A running job that spends over `undersize_stall_pct` (10%) of its time stalled reclaiming memory against its
  own RAM limit, and that the loop could not grow for lack of room, gets one `ALERT` that it is undersized: it runs
  slowly, and its limit is raised as soon as room frees. `job room` shows it while it lasts. Its stall is
  not host memory pressure and holds no other job. The alert is made only while nothing outside the jobs stalls
  and the host's `MemAvailable` is above its RAM headroom, so it is never a host short of RAM reclaiming from it.
  On a host with `auto_resize` off, an oversized job instead gets one `ALERT` with the exact `job resize` command
  that shrinks it, and an undersized one is never grown: kill it and submit it again with a larger `--ram`.
- The job runs in its own systemd scope with a hard RAM limit (its size times the host's `ram_hard_factor`, or
  for a pilot its free RAM at release until the loop cuts it to its use; the loop may lower or raise it, above), no swap, and whole-job kill. A job over its limit ends `FAILED 137` with
  `note: it went over its RAM limit of N GB; the whole job was stopped`. Processes it leaves running when its
  command ends are stopped.
- GPUs are given by UUID in `CUDA_VISIBLE_DEVICES` (also in `JOB_GPU_UUIDS`); a job without `--gpus` sees none.
  `--env CUDA_VISIBLE_DEVICES=...` is refused. Code should use devices 0..N-1.
- `ALERT` (exit 201) can also come from the loop: the job's group is paused (the note gives the exact `job resume`
  command), it uses more GPU memory than its
  size or a GPU it was not given, its RAM trend reaches its limit, it is oversized or undersized, a stop at its time limit failed, or the
  host's loop has stopped while the job is held. The job keeps its place.
- A pilot stopped at its time limit ends `PILOT_TIME_LIMIT` (exit 207, not 137) with a note saying so ("as
  planned"); size the next run `--like` its token, or `--like last`.
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
- A service that uses a GPU or much RAM outside `job` (a model server) registers it before it loads:
  `job hold HOST --gpu 0 --mem 18G --name NAME`, `--ram 72G`, or both, plus `--while-pid PID`, `--unit NAME`
  or `--cgroup PATH` for its processes, and optionally `--ttl MIN` (renew with `job hold HOST --renew ID`). It
  prints the hold id; `job release HOST ID` ends it. A hold is granted only if the host has the room now, else
  refused at once with exit 206 (`NO_ROOM`). A GPU hold alone on its card may use the card's headroom. Holds and
  jobs are first come. A hold whose process (or unit or cgroup) is already gone is ended by the next `job hold` on
  that host, which gets its room back at once, so a service that reloads right away is not refused against its own
  old hold.
- `job history [HOST ...] [--task T]` lists the records on admission hosts, newest first: size (for a pilot,
  the free room it was counted at), measured peaks, how each job ended, and its token for `--like`.
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
