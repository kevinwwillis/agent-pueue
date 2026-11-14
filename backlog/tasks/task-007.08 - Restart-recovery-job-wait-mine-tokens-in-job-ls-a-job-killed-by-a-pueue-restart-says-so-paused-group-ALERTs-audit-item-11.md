---
id: TASK-007.08
title: >-
  Restart recovery: job wait --mine, tokens in job ls, a job killed by a pueue
  restart says so, paused group ALERTs (audit item 11)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 22000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
After an app or PC restart agents grepped output files for tokens and jobs ended unnoticed up to 11.7 h; pueue restarts killed jobs with a Rust backtrace and no reason and left a group paused for 5 days (cause: TASK-004). Keep --check scripts with the job.
<!-- SECTION:DESCRIPTION:END -->
