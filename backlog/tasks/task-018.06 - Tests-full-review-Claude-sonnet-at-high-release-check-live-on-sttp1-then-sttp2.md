---
id: TASK-018.06
title: >-
  Tests, full review (Claude sonnet at high), release; check live on sttp1 then
  sttp2
status: To Do
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T21:17:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 42000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Unit and remote tests pass; review findings handled; live room shows pilots and jobs counted at measured use
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 16:18 ET: review 1 (Claude Sonnet 5.5 at high) found 10 defects. Fixed: 1 lock order (regression test checked against the bug), 2 pilot GPU recount removed, 3 grow capped at grow_factor per cycle, 4 no shrink within oversize_after_min of a grow, 5 and 6 and 7 auto cut is now a soft limit (memory.high), hard limit kept, so nothing is killed by a cut and no reclaim runs under the lock, 8 grow_blocked cleared in the queue file, extras (sqlite errors caught, retry backoff, ceiling default). 9 mitigated by the soft limit (a pilot over it is slowed, not capped, so its peak is real). 10 filed as TASK-018.07. Full suite run 1 under job: 3 priority/stall tests failed only because the suite ran at nice 19; passed run directly. Remote tests on sttp1 passed. Review 2 running.
<!-- SECTION:NOTES:END -->
