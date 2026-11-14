---
id: TASK-007.01
title: >-
  Oversized jobs: ALERT the owner and let it shrink; resize a held job keeping
  its place (audit items 1d, 3)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Kevin 2026-11-14 option (d): keep counting running jobs at their hard limit. When a running job's peak stays under half its size for 30 min, ALERT the owning wait that it is oversized. New job resize HOST ID --ram/--cpus: on a held job it changes the size and keeps its queue place; on a running job it may only shrink, lowering memory.max and the count, refused below the measured peak plus margin. A held-job resize below an earlier recorded peak of the same task is refused. Held line warns that a size below need ends in exit 137.
<!-- SECTION:DESCRIPTION:END -->
