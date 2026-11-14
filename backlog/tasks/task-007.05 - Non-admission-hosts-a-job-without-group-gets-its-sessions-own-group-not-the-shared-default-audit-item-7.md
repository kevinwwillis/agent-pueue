---
id: TASK-007.05
title: >-
  Non-admission hosts: a job without --group gets its session's own group, not
  the shared default (audit item 7)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The shared default group (limit 1) made sessions wait on each other's jobs up to 1 h, about 15 times; the queued-behind line goes only to background output.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A job without --group on a host without admission runs in session-<session id> (limit 1), created when needed
- [x] #2 Sessions never queue behind each other's jobs; a session's own jobs run one at a time
- [x] #3 Session groups job made, empty and unused for an hour, are removed at a later submit
- [x] #4 Admission hosts keep sched
- [x] #5 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:43 ET, branch audit-fixes commit 8ed69d8. Name: session- plus the session id (cut to 24 characters with a 6-hex sha256 when cut or unsafe). Marks in STATE_DIR/session-groups, touched by every submit; a mark older than 1 h whose group has no task (pueue refuses to remove a group with tasks, Done ones included) is removed with its group. Host-side, so an old client gets it too. Tests: SessionGroupTests (6). Remote test not run (brief).

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
