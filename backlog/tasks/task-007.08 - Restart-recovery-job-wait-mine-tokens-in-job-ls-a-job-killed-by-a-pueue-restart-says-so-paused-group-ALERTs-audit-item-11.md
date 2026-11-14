---
id: TASK-007.08
title: >-
  Restart recovery: job wait --mine, tokens in job ls, a job killed by a pueue
  restart says so, paused group ALERTs (audit item 11)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 22000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
After an app or PC restart agents grepped output files for tokens and jobs ended unnoticed up to 11.7 h; pueue restarts killed jobs with a Rust backtrace and no reason and left a group paused for 5 days (cause: TASK-004). Keep --check scripts with the job.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 job wait --mine waits on every unfinished job of this session on every host
- [x] #2 job ls shows each job's token
- [x] #3 --check scripts are kept with the job; a reattach runs them
- [x] #4 A job killed by a pueue daemon stop ends KILLED_BY_RESTART (exit 208), documented
- [x] #5 pueue's errors are one line, no backtrace
- [x] #6 A job waiting in a paused group raises an ALERT naming job resume
- [x] #7 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 14:24 ET, commit f463c54 (one-line pueue errors in 8ed69d8). Restart test: started before the daemon now running (pid file in runtime_directory, XDG_RUNTIME_DIR or the pueue directory, start time from /proc), no job-kill mark, ended at most 24 h before the daemon start (graceful stop; measured with pueue 4.0.4: end = stop time, group left paused) or within 2 min after (daemon killed: end = restore time, group not paused). A pueue kill by hand under 24 h before a restart is taken for the restart (documented). --mine also names, in notes, the session's jobs killed by a restart in the last 24 h. Kept checks: STATE_DIR/checks/<token> on the waiting machine, 30 days. The cause of the restarts themselves is TASK-004. Tests: RestartRecoveryTests (6), restart test in FreshDaemonTests.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
