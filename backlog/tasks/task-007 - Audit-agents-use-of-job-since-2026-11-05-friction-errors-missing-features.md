---
id: TASK-007
title: 'Audit agents'' use of job since 2026-11-05: friction, errors, missing features'
status: To Do
assignee: []
created_date: '2026-11-14T17:05:00Z'
updated_date: '2026-11-14T17:41:00Z'
labels:
  - audit
dependencies: []
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Kevin, 2026-11-14 11:58 ET: find the friction and errors in agents' use of job since it shipped (2026-11-05), and what would have made it easier; list them to pick fix / later / skip. Readback confirmed 12:04 ET. Phase: inspect and propose. Sources: about 30 agent sessions that ran job 1,672 times (read by four blind Claude Sonnet readers at high), and the admission run records of the two servers. Findings (23, after a full review that corrected 14 defects) are kept in the operator's private records, not in this public repo; Kevin's verdicts go on this task.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Findings list with evidence, frequency and proposed change
- [ ] #2 Kevin's verdict per finding recorded; each 'fix' filed as a subtask
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Kevin's verdicts 2026-11-14 12:41 ET (aqua 93d146953e4f): fix now 1,2,3,4,5,7,8,9,11,12,15,16,23; later 6,10,13,14,17,18,19,20,21,22; item 1 by option (d): tell the owner when its job is oversized and let it shrink, keeping count-at-limit.
<!-- SECTION:NOTES:END -->
