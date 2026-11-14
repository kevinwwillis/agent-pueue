---
id: TASK-006
title: >-
  job wait with several ids does not return early when one job ends FAILED or
  KILLED
status: Done
assignee: []
created_date: '2026-11-14T16:15:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
priority: high
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-14 11:20 ET by a review of the Starla full HMI run staging. wait_all in bin/job returns early only for URGENT = STALLED, ALERT, UNREACHABLE, GONE, WRAPPER_ERROR (around line 99). A FAILED (e.g. exit 137 RAM-limit kill) or KILLED (daemon restart) job in a multi-id wait stays silent until every job in the wait has ended, so a long queue (Starla: ~197 month jobs over weeks) learns of the failure days late. Also: a job printing a status line every minute is never STALLED even when its worker hangs (log growth only). Proposed: return early (or report and continue) on any job ending non-SUCCESS; optional stall rule on a pattern of progress lines.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A wait on several ids ends as soon as one ends badly (anything but SUCCESS or PILOT_TIME_LIMIT), naming it and giving the exact command to keep waiting on the rest
- [x] #2 N of M ended counts only jobs that really ended
- [x] #3 ID:TOKEN pairs are accepted; --token with several ids is refused with that hint, documented
- [x] #4 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 14:10 ET, commit e36eaf9. Not built: the optional stall rule on a pattern of progress lines (not in this batch's brief). Tests: MultiWaitTests (5), updated multi-wait tests.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
