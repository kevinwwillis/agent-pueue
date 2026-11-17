---
id: TASK-018.07
title: >-
  A paused pilot keeps its whole count: its time limit and the cut to its use
  apply only while it runs
status: To Do
assignee: []
created_date: '2026-11-17T21:17:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: medium
ordinal: 43000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Review 2026-11-17 (finding 10): the pilot ceiling and the auto cut both require status Running, so a pilot paused before its measuring window holds all the RAM room at its release until resumed. Old behavior, left open by TASK-018. Decide: count a paused pilot at its use, or end it at the ceiling counted from release.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A paused pilot does not hold more than its measured use plus margin past the ceiling
<!-- AC:END -->
