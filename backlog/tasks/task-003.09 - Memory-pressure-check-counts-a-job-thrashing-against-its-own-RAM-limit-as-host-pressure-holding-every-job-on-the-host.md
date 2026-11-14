---
id: TASK-003.09
title: >-
  Memory pressure check counts a job thrashing against its own RAM limit as host
  pressure, holding every job on the host
status: To Do
assignee: []
created_date: '2026-11-14T20:04:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 34000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Seen 2026-11-14 15:03 ET on a server: 105 GB available, but system-wide memory PSI full avg10 ~22% because one admitted job (4 GB size, 5 GB limit) sat at its limit with 30% stall in its own scope; the loop held all 7 waiting jobs for 'memory pressure 22.4% is over 5.0%' for up to 67 min. A job's stalls against its own memory.max are not host pressure. Fix: measure pressure outside admitted job scopes (other slices and the user's non-job units), not /proc/pressure/memory; the job's own thrashing belongs in its owner's ALERT (it is undersized).
<!-- SECTION:DESCRIPTION:END -->
