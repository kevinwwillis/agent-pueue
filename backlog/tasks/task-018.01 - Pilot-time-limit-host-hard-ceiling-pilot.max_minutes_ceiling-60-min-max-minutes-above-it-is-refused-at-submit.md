---
id: TASK-018.01
title: >-
  Pilot time limit: host hard ceiling pilot.max_minutes_ceiling (60 min);
  --max-minutes above it is refused at submit
status: Done
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T22:39:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 37000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 --max-minutes above the host ceiling is refused with a message naming the ceiling
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 15:55 ET: built on branch task-018-sizing (commit 55880c9, job 0.9.0); new tests pass; full suite, sttp1 remote test and full review (Claude Sonnet, high) running.
<!-- SECTION:NOTES:END -->
