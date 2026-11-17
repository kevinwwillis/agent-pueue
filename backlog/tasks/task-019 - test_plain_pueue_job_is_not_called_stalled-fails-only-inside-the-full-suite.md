---
id: TASK-019
title: test_plain_pueue_job_is_not_called_stalled fails only inside the full suite
status: To Do
assignee: []
created_date: '2026-11-17T22:21:00Z'
labels: []
dependencies: []
priority: low
ordinal: 44000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-17 17:21 ET: fails in every full run (python3 -m unittest discover -s tests) on 3ca8cb0 (before TASK-018) and after it: 'stall detection is off' not in stderr. Passes alone, with its class, and alone under load. Likely an earlier test leaves the shared state so the plain pueue task never shows Running to job wait. Find the leftover; fix the test or the leak.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The full suite passes with this test in it
<!-- AC:END -->
