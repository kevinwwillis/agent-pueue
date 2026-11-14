---
id: TASK-003.08
title: >-
  A hold whose --while-pid process has exited still counts until the loop's next
  cycle, so an immediate reload of the same service is refused against its own
  stale hold
status: Done
assignee: []
created_date: '2026-11-14T17:52:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 33000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Reported 2026-11-14 12:52 ET by a model-service owner: a 'sleep 20' test hold was released 31.9 s after it was granted (about 12 s after its process exited). host_hold reads room from the last snapshot, which still counts the dead hold, so a reload in that window is counted against its own hold; a guard-side job release cannot help because the snapshot still counts it. Fix: host_hold credits holds whose --while-pid process (or cgroup/unit) is already gone when it computes room.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 job hold ends holds whose process, ttl, unit or cgroup is gone and credits back what the snapshot counted for them
- [x] #2 A reload right after the process exits is granted (RAM and GPU)
- [x] #3 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 14:03 ET, commit ab06239. Same test as update_holds (unit/cgroup only once seen). Credit is the hold's size, the safe side of what the snapshot counted (max of size and measured use); a GPU card whose only hold died regains its headroom rule. Tests: 2 LoopTests, both fail on the commit before.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
