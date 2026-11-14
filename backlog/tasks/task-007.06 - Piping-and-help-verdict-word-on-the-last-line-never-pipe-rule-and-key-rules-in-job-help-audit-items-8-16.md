---
id: TASK-007.06
title: >-
  Piping and help: verdict word on the last line, never-pipe rule and key rules
  in job --help (audit items 8, 16)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
About 140 piped job run/wait calls; 8 failed jobs reported as exit 0 by the harness. Agents learn job from --help (about 70 calls). Last line becomes 'job: FAILED host#id exit N'; usage.md and --help epilog: never pipe (use --tail), shared default group, size with a pilot, kill removes queued jobs.
<!-- SECTION:DESCRIPTION:END -->
