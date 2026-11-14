---
id: TASK-003.09
title: >-
  Memory pressure check counts a job thrashing against its own RAM limit as host
  pressure, holding every job on the host
status: Done
assignee: []
created_date: '2026-11-14T20:04:00Z'
updated_date: '2026-11-14T22:22:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 34000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Seen 2026-11-14 15:03 ET on a server: 105 GB available, but system-wide memory PSI full avg10 ~22% because one admitted job (4 GB size, 5 GB limit) sat at its limit with 30% stall in its own scope; the loop held all 7 waiting jobs for 'memory pressure 22.4% is over 5.0%' for up to 67 min. A job's stalls against its own memory.max are not host pressure. Fix: measure pressure outside admitted job scopes (other slices and the user's non-job units), not /proc/pressure/memory; the job's own thrashing belongs in its owner's ALERT (it is undersized).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Held reasons and job room use memory pressure outside the jobs' own RAM limits; a job stalled against its own memory.max holds no other job
- [x] #2 Real pressure outside the jobs (another user's slice, system slice, this user's other units, a job stalled below its limit) still holds jobs
- [x] #3 Where a cgroup's pressure cannot be read, the system-wide figure is used and job room says so
- [x] #4 A running job stalled over undersize_stall_pct against its own limit gets one ALERT and shows in job room
- [x] #5 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 15:45 ET, branch audit-fixes: built. Source (my call): the highest memory.pressure full avg10 among the cgroups that hold no job scope (each top-level cgroup, each user slice under user.slice on its own, this user's units beside the job scopes) and the job scopes that are stalled but NOT at their own limit (job/ memory.events max did not grow since the last sample; first sight: never reached it, or memory.current < 95% of memory.max). Why: a real host shortage stalls whatever allocates outside the jobs and also stalls jobs in global reclaim, so it still holds jobs; a job reclaiming inside its own memory.max raises only its own scope (and the system-wide figure). The alternative (system-wide figure discounted when MemAvailable is high) needs a threshold for "plenty" and still cannot separate stalls. Fallback: any non-job cgroup's pressure file unreadable (not just gone), or none readable, uses the system-wide figure (safe side); room/held reason name the source and the cgroup where it is highest. Undersized: a running job at its limit with job/ full avg60 >= undersize_stall_pct (new setting, default 10) gets one ALERT (kind undersized) and job room shows it while it lasts. Measure takes psi_root so loop tests read a fake tree, not this machine's pressure. Tests: PressureMeasure (6, fake cgroup tree incl. unreadable file), LoopTests a_job_stalled_against_its_own_limit (real job, faked kernel figures, system-wide 22.4% does not hold a second job) and real_pressure_outside_the_jobs_still_holds_jobs. Remote test not run (brief: no real hosts).

Released in job 0.8.0 (v0.8.0, 603eeda), 2026-11-14 17:15-17:21 ET: sttp1, then sttp2, then this PC; remote tests passed on sttp1 and sttp2; two full review cycles (Claude Sonnet sonnet at high) plus a final judged repair.
<!-- SECTION:NOTES:END -->
