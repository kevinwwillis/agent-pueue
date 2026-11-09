---
id: TASK-003.03
title: 'Admission loop on each host: measure, count room, release held jobs, record'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T20:34:00Z'
labels: []
dependencies:
  - TASK-003.01
  - TASK-003.02
parent_task_id: TASK-003
priority: high
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
One loop per host is the only thing that starts admitted jobs, so two jobs cannot take the same room. It measures CPU busy, available RAM, memory pressure, per-disk busy time and per-process GPU memory; separates what admitted jobs use from everything else; applies the room rules from the host settings file; and releases held jobs from a dedicated pueue group that nothing else uses and nobody pauses. It writes one record per job, with measured peaks.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The loop releases a held job only when its size fits the room rules from the settings file
- [ ] #2 Use by processes that job did not start counts against room
- [ ] #3 A job is counted from release until its scope is gone
- [ ] #4 Every admitted job gets a record with its measured peaks
- [ ] #5 Tests cover fit, no fit, outside use, and a finished job freeing its room
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 12:50 ET, two spec changes for the loop (my calls, from writing the first deployment's settings): (1) 'disks' becomes a list of paths; the loop finds each path's block device at start (mountinfo major:minor to /sys/dev/block), because sdX names change between boots. (2) New setting iowait_max_pct (host iowait, the measure that deployment's dial uses). A heavy-disk job (earlier run over disk_heavy_mb_s) waits while its data's disk is busier than disk_busy_max_pct, or while host iowait is over iowait_max_pct and its disk is the busiest listed disk, so jobs on quiet disks still start. Update docs/admission.md 'Host settings file' and 'Room' with the loop code.

2026-11-09 13:35 ET: first version written in bin/job: settings file (validated, unknown keys refused, invalid file refuses every submit), Measure (/proc stat, meminfo, memory PSI, diskstats; GPUs through NVML via ctypes with nvidia-smi fallback; job scopes found under the user manager; RAM basis memory.current minus file cache; GPU processes mapped to jobs by cgroup), compute_room and fits (RAM at the window low, CPU outside use with older jobs at measured use, GPU outside use as the lower of two passes, best-fit card, GPU busy, disk and iowait rule, memory pressure), fits_empty refusal at submit, held submits (queue file, pueue add --stashed, group sched), fit_now, release under the queue lock with an assignment file the wrapper reads (RAM limit, GPU UUIDs), fairness (an older job short of room that would fit once admitted jobs end blocks younger ones that take that room), pilots (all the RAM room, stopped at their time limit), --like from records (refuses lower-bound runs), --stop-at, records in sqlite with peaks, alerts (group paused, GPU over size), room snapshot, heartbeat, leftover scope clean-up, job _admit with re-exec on install. My calls: GPUs and pilot RAM are decided at release, so the wrapper reads an assignment file the loop writes before pueue enqueue; --like is resolved on the host from its records. Found by tests: the kernel rounds memory.max to pages. 29 admission tests pass. Not yet: holds, the systemd unit, client flags (TASK-003.04), a test on a real server.

2026-11-09 14:13 ET: full review cycle 1 (Claude Sonnet 5.5, sonnet at high) of the loop and client found 17 defects. Fixed in 477d1be: F1 stop-at crash, F2 leftover clean-up, F3 pilot sizes, F4 peaks after restart, F5 unmeasured --like, F6 first-cycle CPU and windows (recent samples kept, warmup_s), F11 lock order, F13 snapshot after releases, F14 loop survives a failed cycle, F15 pilot limit from start, F7/F8/F10/F16 docs and dropped flags (--max-minutes, --needs-path, --in-containers, disk rate, scope probe at admission start), F17 lows. Set aside: F12 (protocol 5 shared with the unreleased scope-only build, which no host runs); argparse abbreviations (an ambiguous prefix is an error, not a silent change); an alert repeated after a crash between alert and save (harmless); room replies carry the settings (the client ranks hosts with them; nothing secret). Next: review cycle 2 of the whole branch, including holds and the service command.

2026-11-09 14:45 ET: full review cycle 2 (Claude Sonnet 5.5, sonnet at high) of the whole admission change found 10 defects plus smaller items and doc mismatches. Fixed in the commit after ef75237: F1 GPU use on a card a job was not given, F2 hold and job given the same room (holds re-read and snapshot written under the queue lock), F3 queue file vs pueue disagreement, F4 retry after late pueue add, F5 no reply treated as a refusal in auto, F6 impossible GPU share and zero sizes, F7 NaN settings, F8 heartbeat renewed without pueue, F9 clean-up scope, plus unit/cgroup holds ending, NVML retry, probe robustness, alert file clean-up, pilot stop note, docs. Set aside: F10 STATE_DIR follows XDG_STATE_HOME (checked unset in ssh, user manager, pueued and profiles on sttp1 and sttp2; documented); per-cycle systemctl calls for unit holds and per-second pueue status during pilots. Two review cycles done for this piece; no more.

2026-11-09 15:36 ET: live on sttp1 since 14:51 ET. Checked there: the smoke job was held through the 10-minute warm-up and released at 15:01:56 ET into its own scope, no GPU, RAM limit 6.0 GB (4 GB x 1.5), nice 19; room matched the host (RAM 102.2 GB = the 10-minute minimum of MemAvailable 118.2 GB less 16 GB; each card's outside use = the driver's reserved memory, 406 and 444 MiB; CPU 3.7 against a 3.5-thread average); a GPU pilot got the 2080 Ti by UUID and its record kept 1.0 GB peak over 36 samples; a pilot's RAM limit was 102.16 GB, all the free room. Defect found there: records kept a pilot's placeholder size, so job history showed 4.0 GB for a pilot limited to 102 GB; fixed in 0.6.1 (2babe25), reviewed by Claude Sonnet 5.5 (sonnet) at high, repairs in 9428150 (records-file upgrade under a write lock, a job held again loses its release in its record, counted values filled in each cycle, pilot size text for every case). Also fixed: the loop did not prime its disk reading at start, so the first cycle after a restart had no disk readings. Second review of the repairs running.
<!-- SECTION:NOTES:END -->
