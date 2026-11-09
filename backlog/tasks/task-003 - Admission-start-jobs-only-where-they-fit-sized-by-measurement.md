---
id: TASK-003
title: 'Admission: start jobs only where they fit, sized by measurement'
status: In Progress
assignee: []
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T21:10:00Z'
labels: []
dependencies: []
priority: high
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Several agents share a few machines: servers that people also use directly, and rented cloud machines. Until now a dispatcher decided by hand where each big job went, which cost a lot of agent time. The repo owner decided (2026-11-09 11:56 ET) that `job` itself takes this over. Each host runs one admission loop that measures what is in use, including processes `job` did not start, counts the room left for CPU, RAM, GPU memory and disk, and starts queued jobs that fit. Job sizes come from measured pilot runs, not guesses, and no rule depends on a caller's run-time estimate ("Agents are really bad at estimating time costs", owner, 2026-11-09). GPU holds by services outside `job` are first come. Settings for any one deployment (thresholds, host names) live outside this public repo.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A job given to job run auto starts only on a host where its CPU, RAM and GPU memory fit, and otherwise waits in a queue that survives the caller ending
- [x] #2 Room shown by job room is computed from measurement, including processes that job did not start
- [x] #3 Job sizes can come from a measured pilot run; no admission rule uses a caller run-time estimate
- [x] #4 Every admitted job gets an automatic record: size, host, start, end, exit, measured peaks
- [x] #5 A host can move to the new protocol while other hosts still run the old one
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 16:11 ET: criteria checked: 1 (tests on two throwaway hosts; live on sttp1, held through warm-up and released when it fit), 2 (live: job room on sttp1 matches MemAvailable and the driver's reserved GPU memory), 3 (pilot then --like, tests; no rule takes a run time), 4 (live records on sttp1 with size, counted room, RAM limit and peaks), 5 (sttp1 on protocol 5 while sttp2 is not an admission host; tests of old hosts). Subtasks .01 to .04 done (job 0.6.2). Still open here: rented hosts (hosts.d) and TASK-003.05 (GPU memory caps); service holds with RAM are agent-callers TASK-012.
<!-- SECTION:NOTES:END -->
