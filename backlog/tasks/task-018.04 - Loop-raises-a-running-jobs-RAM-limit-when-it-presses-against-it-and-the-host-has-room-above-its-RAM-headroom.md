---
id: TASK-018.04
title: >-
  Loop raises a running job's RAM limit when it presses against it and the host
  has room above its RAM headroom
status: In Progress
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T20:54:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 40000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A job stalling against its own limit gets a larger limit when room exists; otherwise the undersized ALERT as today
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 15:55 ET: built on branch task-018-sizing (commit 55880c9, job 0.9.0); new tests pass; full suite, sttp1 remote test and full review (Claude Sonnet, high) running.
<!-- SECTION:NOTES:END -->
