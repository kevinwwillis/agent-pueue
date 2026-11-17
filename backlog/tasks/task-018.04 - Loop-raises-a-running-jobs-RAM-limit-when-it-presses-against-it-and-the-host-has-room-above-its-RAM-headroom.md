---
id: TASK-018.04
title: >-
  Loop raises a running job's RAM limit when it presses against it and the host
  has room above its RAM headroom
status: To Do
assignee: []
created_date: '2026-11-17T20:36:00Z'
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
