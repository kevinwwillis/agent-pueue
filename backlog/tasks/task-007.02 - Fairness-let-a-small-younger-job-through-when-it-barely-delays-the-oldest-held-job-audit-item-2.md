---
id: TASK-007.02
title: >-
  Fairness: let a small younger job through when it barely delays the oldest
  held job (audit item 2)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
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

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 After fairness_wait_min, a younger job that fits may start when its counted need of each blocked resource is under fairness_small_fraction (0.1) of what the oldest lacks; otherwise held as before
- [x] #2 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:20 ET, commit 6b057f4: built. My call beyond the brief: the share is a budget kept with the oldest job across cycles: younger jobs let past it and still in flight, together, stay under fairness_small_fraction of the smaller of what it lacked when first blocked and what it lacks now (fair_base, fair_passed in its queue entry). Without it, each small job raises what the oldest lacks, and the budget grows each cycle (seen in the test: a second tiny job got through one cycle later). Busy GPU, busy disk and a pilot's RAM lack 0, so nothing passes there, as before. Tests: _lack unit test; LoopTests small-younger test (one tiny passes, a second is held, existing 2 GB younger still held).

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
