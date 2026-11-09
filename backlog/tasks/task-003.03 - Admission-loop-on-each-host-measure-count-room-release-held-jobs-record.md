---
id: TASK-003.03
title: 'Admission loop on each host: measure, count room, release held jobs, record'
status: To Do
assignee: []
created_date: '2026-11-09T17:00:00Z'
labels: []
dependencies:
  - TASK-003.01
  - TASK-003.02
parent_task_id: TASK-003
priority: high
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
One loop per host is the only thing that starts admitted jobs, so two jobs cannot take the same room. It measures CPU busy, available RAM, memory pressure, per-disk busy time and per-process GPU memory; separates what admitted jobs use from everything else; applies the room rules from the host settings file; and releases held jobs from a dedicated pueue group that nothing else uses and nobody pauses. It writes one record per job, with measured peaks.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The loop releases a held job only when its size fits the room rules from the settings file
- [ ] #2 Use by processes that job did not start counts against room
- [ ] #3 A job is counted from release until its scope is gone
- [ ] #4 Every admitted job gets a record with its measured peaks
- [ ] #5 Tests cover fit, no fit, outside use, and a finished job freeing its room
<!-- AC:END -->
