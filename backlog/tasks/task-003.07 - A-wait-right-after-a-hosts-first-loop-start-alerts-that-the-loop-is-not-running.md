---
id: TASK-003.07
title: >-
  A wait right after a host's first loop start alerts that the loop is not
  running
status: To Do
assignee: []
created_date: '2026-11-13T03:14:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
On a host whose admission loop has just started for the first time, there is no heartbeat file until the first cycle ends, about one sample interval later; the first cycle waits that interval so its rates cover a whole interval. A job wait in that gap sees no heartbeat and ends with ALERT 'the admission loop on HOST is not running' (exit 201), though the loop is running and the job is held normally. Seen 2026-11-12 22:13 ET on the first start of a second admission host. A restart after an install or a settings change keeps the old heartbeat, which is still fresh, so only a first start hits it. Fix: tell a missing heartbeat with the loop service just started (or a loop.lock held) from a stopped loop, or write a heartbeat marked 'starting' at loop start.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A wait on a job submitted in the first sample interval after a first loop start does not ALERT
- [ ] #2 A wait still ALERTs when the loop is stopped
<!-- AC:END -->
