---
id: TASK-018.02
title: >-
  Pilot counted at measured peak plus margin after a measuring window, so held
  jobs start beside it; kernel limit stays generous
status: In Progress
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T20:54:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 38000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 After the window, room counts a running pilot at peak x margin (or its trend), not free RAM at release
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 15:55 ET: built on branch task-018-sizing (commit 55880c9, job 0.9.0); new tests pass; full suite, sttp1 remote test and full review (Claude Sonnet, high) running.

My call: the pilot's kernel limit comes down with its count (peak x pilot.shrink_margin 2.0), not left high, because a count below the limit lets the pilot grow into room given to others; growth is covered by auto-grow while room exists.
<!-- SECTION:NOTES:END -->
