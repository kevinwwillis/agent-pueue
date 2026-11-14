---
id: TASK-007.05
title: >-
  Non-admission hosts: a job without --group gets its session's own group, not
  the shared default (audit item 7)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
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
