---
id: TASK-007.01
title: >-
  Oversized jobs: ALERT the owner and let it shrink; resize a held job keeping
  its place (audit items 1d, 3)
status: Done
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T22:22:00Z'
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
- [x] #7 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:20 ET, branch audit-fixes commit 65a66b1: built. Settings oversize_after_min 30, oversize_fraction 0.5, resize_margin 1.25 (the peak is sampled, so a lower bound; 1.25 is the margin --like already puts on a peak). Resizes are written to state_dir/resize/<token>.json under the queue lock, not the queue file (the loop rewrites queue files from the copy it read at cycle start, which would lose a resize); every queue reader applies it and the loop applies it again under the lock before releasing. A running job must have been measured 3 times before it can be resized. The loop lowers a resized job's memory.max again if a starting scope set the old one. Suggested size = peak x margins.ram (what --like gives), at least the resize floor, rounded up to 0.1 GB. New host op resize; PROTOCOL unchanged (additive). Tests: ResizeRules (6 unit) and LoopTests oversize/shrink, held resize, pilot refused, all with real scopes; full suite 227 OK, 12 remote skipped. Remote test not run (brief: no real hosts).

2026-11-14 15:45 ET, review 3 and the peak measure: the ALERT, its suggestion and the running-resize floor now use one measure, what a job can't give back if its limit is lowered: max(sampled RAM-basis peak x resize_margin, use now, memory.peak less the file cache it holds now). memory.peak counts file cache; a streaming job fills its limit with cache, so taken whole it blocked every shrink and silently dropped the ALERT (live: 64 GB job, 26.5 GB used, memory.peak 80 GB). Less the cache now it still covers a real anonymous spike (little cache now leaves the floor at memory.peak); the one case it cannot see is cache squeezed by a spike between samples and refilled since (documented). A rising progress RAM trend counts as the need (no ALERT when it reaches oversize_fraction of the size; the suggestion covers it). No ALERT when ram_hard_factor was raised so that no accepted size exists. Also fixed: resize reads capacity (GPU query) before queue.lock; _simulate_fit/fit_now pass data_disk; --like last and the held floor prefer the same session and name it; _like_source_text and the reply text after pueue add never raise; record_peaks fills ram_counted/gpu_counted/memory_max only where NULL (a mid-cycle resize is kept); _submit_reason checks the heartbeat first; keep_resized_limit quiet when the scope ended; resize of a released-but-stashed job says "being released, try again"; test_scope shim test compared the test process's cgroup control files, which systemd changes when a sibling scope with CPUWeight starts or ends (checked live), now compares child cgroups and subtree_control only.

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
