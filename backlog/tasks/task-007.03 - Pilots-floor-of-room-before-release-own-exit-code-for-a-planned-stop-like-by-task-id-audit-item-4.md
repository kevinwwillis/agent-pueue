---
id: TASK-007.03
title: >-
  Pilots: floor of room before release, own exit code for a planned stop, --like
  by task id (audit item 4)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A pilot's limit is the leftover room at release (4-6 GB on a busy host, killed within minutes). Pilot waits for pilot.ram_floor_gb of room. A planned time-limit stop gets its own exit code, not 137. job run can size from the last good run of the same task id, showing the peak.
<!-- SECTION:DESCRIPTION:END -->
