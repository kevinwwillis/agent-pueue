---
id: TASK-001
title: job kill leaves the job's child processes running
status: To Do
assignee: []
created_date: '2026-11-06T21:10:00Z'
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
- [ ] #1 Reproduced with a job that forks children
- [ ] #2 job kill ends every descendant process on local, sttp1 and sttp2
- [ ] #3 A test covers it
<!-- AC:END -->
