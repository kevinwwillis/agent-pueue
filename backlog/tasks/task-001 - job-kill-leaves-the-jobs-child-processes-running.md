---
id: TASK-001
title: job kill leaves the job's child processes running
status: Done
assignee: []
created_date: '2026-11-06T21:10:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
priority: high
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Reported 2026-11-06 16:09 ET by the MARVELS optics lead session (via the MARVELS Oracle): after `job kill` on sttp1, the pueue job was marked killed but its child processes kept running; the session had to stop them by PID. Expected: killing a job ends its whole process group (or cgroup), on every host. Not yet reproduced by this tool's owner; the reporting session's job ids are in its MARVELS task notes (TASK-001.31.12.01.05.03).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Reproduced with a job that forks children
- [x] #2 job kill ends every descendant process on local, sttp1 and sttp2
- [x] #3 A test covers it
- [x] #4 Help says kill a running job or remove a queued or held one; job kill reports processes ended and any left
- [x] #5 A job removed by job kill before it ran ends KILLED, removed by job kill
- [x] #6 job kill HOST --task NAME, refused for other sessions' jobs unless --all-sessions
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 14:17 ET, commit b5a1574. Reproduced locally: timeout 300 sh -c ... survived job kill (pueue kills only the job's process group; timeout makes its own). Fix: processes found by token and descendants (with start times) before pueue kill, SIGKILL any still alive for up to 3 s; exit 205 if any is left. #2 (sttp1, sttp2) needs the remote test, not run per the brief. Tests: KillTests (5).

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
