---
id: TASK-007.01
title: >-
  Oversized jobs: ALERT the owner and let it shrink; resize a held job keeping
  its place (audit items 1d, 3)
status: To Do
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T18:20:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Kevin 2026-11-14 option (d): keep counting running jobs at their hard limit. When a running job's peak stays under half its size for 30 min, ALERT the owning wait that it is oversized. New job resize HOST ID --ram/--cpus: on a held job it changes the size and keeps its queue place; on a running job it may only shrink, lowering memory.max and the count, refused below the measured peak plus margin. A held-job resize below an earlier recorded peak of the same task is refused. Held line warns that a size below need ends in exit 137.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A running non-pilot admitted job whose RAM peak stays under oversize_fraction of its size for oversize_after_min gets one ALERT with peak, size, counted limit and the exact job resize command; job room shows it
- [x] #2 job resize on a held job changes its size and keeps its submit time; refused below an earlier finished run's RAM peak of the same task (names the run) and for a size the host could never fit
- [x] #3 job resize on a running job only shrinks: memory.max lowered and read back, counted lower from the next cycle; refused under peak x resize_margin or current use
- [x] #4 job resize on a pilot is refused
- [x] #5 The resize is in the run record; job history shows the size it ended with and the size it was resized from
- [x] #6 A held reason for RAM warns that a size below need ends in exit 137 and points to --pilot / --like
- [ ] #7 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:20 ET, branch audit-fixes commit 65a66b1: built. Settings oversize_after_min 30, oversize_fraction 0.5, resize_margin 1.25 (the peak is sampled, so a lower bound; 1.25 is the margin --like already puts on a peak). Resizes are written to state_dir/resize/<token>.json under the queue lock, not the queue file (the loop rewrites queue files from the copy it read at cycle start, which would lose a resize); every queue reader applies it and the loop applies it again under the lock before releasing. A running job must have been measured 3 times before it can be resized. The loop lowers a resized job's memory.max again if a starting scope set the old one. Suggested size = peak x margins.ram (what --like gives), at least the resize floor, rounded up to 0.1 GB. New host op resize; PROTOCOL unchanged (additive). Tests: ResizeRules (6 unit) and LoopTests oversize/shrink, held resize, pilot refused, all with real scopes; full suite 227 OK, 12 remote skipped. Remote test not run (brief: no real hosts).
<!-- SECTION:NOTES:END -->
