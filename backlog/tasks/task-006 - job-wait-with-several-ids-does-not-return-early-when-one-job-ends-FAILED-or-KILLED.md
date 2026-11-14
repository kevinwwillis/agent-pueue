---
id: TASK-006
title: >-
  job wait with several ids does not return early when one job ends FAILED or
  KILLED
status: To Do
assignee: []
created_date: '2026-11-14T16:15:00Z'
updated_date: '2026-11-14T17:41:00Z'
labels: []
dependencies: []
priority: high
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-14 11:20 ET by a review of the Starla full HMI run staging. wait_all in bin/job returns early only for URGENT = STALLED, ALERT, UNREACHABLE, GONE, WRAPPER_ERROR (around line 99). A FAILED (e.g. exit 137 RAM-limit kill) or KILLED (daemon restart) job in a multi-id wait stays silent until every job in the wait has ended, so a long queue (Starla: ~197 month jobs over weeks) learns of the failure days late. Also: a job printing a status line every minute is never STALLED even when its worker hangs (log growth only). Proposed: return early (or report and continue) on any job ending non-SUCCESS; optional stall rule on a pattern of progress lines.
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Audit 2026-11-14 item 12 (Kevin: fix now): also (a) a multi-id wait ends for all ids when one is STALLED, (b) 'N of M ended' counts STALLED/ALERT jobs that still run, (c) --token is refused with several ids (exit 205), undocumented; accept id:token pairs.
<!-- SECTION:NOTES:END -->
