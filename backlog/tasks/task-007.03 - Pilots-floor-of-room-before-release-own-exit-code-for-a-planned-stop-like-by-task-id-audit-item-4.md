---
id: TASK-007.03
title: >-
  Pilots: floor of room before release, own exit code for a planned stop, --like
  by task id (audit item 4)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A pilot's limit is the leftover room at release (4-6 GB on a busy host, killed within minutes). Pilot waits for pilot.ram_floor_gb of room. A planned time-limit stop gets its own exit code, not 137. job run can size from the last good run of the same task id, showing the peak.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A pilot is released only with at least pilot.ram_floor_gb (16) of RAM room; a host with less for jobs refuses pilots at submit
- [x] #2 A pilot stopped at its time limit ends PILOT_TIME_LIMIT, exit 207 (unused before), documented in usage.md
- [x] #3 --like last sizes from the last good run of the same task id on that host and prints which run and its peaks; --like TOKEN unchanged
- [x] #4 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:20 ET, commit 4dc1d01: built. --like last picks the newest record of the task that exited 0 or was a pilot that reached its limit and was measured at least 3 times (a newer run measured fewer times is skipped). Older clients still see KILLED 137 with the 'as planned' note (their own verdict mapping). Tests: pilot floor and verdict unit tests; LoopTests pilot test now checks exit 207 and --like last; --like last refusal; pilot held under its floor then released.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
