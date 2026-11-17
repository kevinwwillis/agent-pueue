---
id: TASK-018.03
title: >-
  Loop shrinks oversized running jobs itself, with the existing resize safety
  checks, and notes it to the owner
status: In Progress
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T20:54:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 39000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 An oversized job is resized by the loop without owner action; the owner gets a note, not a command to run
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 15:55 ET: built on branch task-018-sizing (commit 55880c9, job 0.9.0); new tests pass; full suite, sttp1 remote test and full review (Claude Sonnet, high) running.
<!-- SECTION:NOTES:END -->
