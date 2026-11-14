---
id: TASK-007.02
title: >-
  Fairness: let a small younger job through when it barely delays the oldest
  held job (audit item 2)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
After the oldest held job has waited fairness_wait_min, every younger job needing ram or cpu is held, whatever its size. Let a younger job through when its size is under a tenth of what the oldest still lacks.
<!-- SECTION:DESCRIPTION:END -->
