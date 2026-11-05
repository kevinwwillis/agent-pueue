# agent-pueue

`job`: run long jobs under [pueue](https://github.com/Nukesor/pueue), on this
machine or over ssh on other hosts, and wait on them correctly. It is built for
AI coding agents (Claude Code and the like) that start a job, run one command in
the background, and need to be woken exactly once, with the real outcome.

```bash
job run gpu1 TASK-12 --cwd /data/runs -- 'python -u train.py --epochs 50'
```

That one call queues the command under pueue on `gpu1` and blocks until the
job ends. Then it prints the verdict, run time and the last log lines, and
exits with the job's own exit code. It ends early, leaving the job running,
when:

- **STALLED:** for 30 minutes (adjustable) the log has not grown and the job's
  processes have used almost no CPU;
- **ALERT:** a `--check` script you give it fails on the job's new output.

## Why

Agents that write their own watchers (a `pgrep` loop, a log grep, a PID file)
get them wrong. A classic: `until ! pgrep -f my_job.py; do sleep 60; done`,
run inside `bash -c`, matches its own command line and never ends. `pueue wait`
exits 0 even when the job failed. `job` leaves process tracking to pueue and
never matches processes by name or command line.

## Also

- `job submit` queues without waiting; `job wait <host> <id> [<id> ...]` attaches
  later, to one job or several, and also works on jobs that already ended.
- `job ls` lists the jobs on every host, with the task id and the agent session
  that started each one.
- `job group <host> <name> <n>` creates a queue that runs `n` jobs at a time.
- `job kill <host> <id>` stops a job.
- Commands reach remote hosts as a file over ssh stdin, never inside the ssh
  command line. They run under `nice -n 19 ionice -c 3` and `set -o pipefail`.
- The tool is one standard-library Python file (3.8+), for Linux. It is
  installed on the agent's machine and on every host.

## Install

See [docs/usage.md](docs/usage.md) for setup, every option, the verdicts and
exit codes, and how stall detection works. In short: install pueue 4.x with its
daemon running on each host, list your hosts in `~/.config/agent-pueue/hosts`,
and run `./install.sh`.

## License

MIT; see [LICENSE](LICENSE).
