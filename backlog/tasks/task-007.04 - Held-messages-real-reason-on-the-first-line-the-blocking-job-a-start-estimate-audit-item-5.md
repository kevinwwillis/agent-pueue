---
id: TASK-007.04
title: >-
  Held messages: real reason on the first line, the blocking job, a start
  estimate (audit item 5)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
First held line often reads 'waiting for the next check'; 'waiting behind an older job' names no job; no estimate.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The first held line comes from the submit's own reason (snapshot-based; 'measuring the host' without a snapshot), never 'waiting for the next check'
- [x] #2 Waiting behind an older job names it (task id and token) and what it needs
- [x] #3 With projected ends, the reason estimates when enough room should free; otherwise it says none is known and why
- [x] #4 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:20 ET, commit ca5a01f: built. Estimate: running jobs holding the lacking resource in order of projected end until what they free covers the lack (GPU: as many cards as still needed); a job with no projected end on the way gives 'none known'. Times are in the host's zone; snapshot carries start_estimate. job submit prints the held line. Tests: start_estimate unit test; LoopTests first-held-line test; reason text asserted in the fairness test.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
