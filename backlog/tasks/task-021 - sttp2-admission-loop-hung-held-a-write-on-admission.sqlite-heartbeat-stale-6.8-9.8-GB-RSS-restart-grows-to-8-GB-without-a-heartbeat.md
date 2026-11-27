---
id: TASK-021
title: >-
  sttp2 admission loop hung: held a write on admission.sqlite, heartbeat stale,
  6.8-9.8 GB RSS; restart grows to 8 GB without a heartbeat
status: Done
assignee: []
created_date: '2026-11-27T21:54:00Z'
updated_date: '2026-11-27T22:27:00Z'
labels:
  - bug
dependencies: []
priority: high
ordinal: 46000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-27 4:49 PM ET by Lucas Wang's agent (STTC student-help) while submitting jobs on sttp2.
- job submit sttp2 failed twice: WRAPPER_ERROR 'OperationalError: database is locked'. job room: 'sttp2: admission loop not running (heartbeat 9.3 min old)'.
- The loop (pid 2827, job _admit, up 8 d 23 h, version 0.9.1) was running at 80% CPU, 6.8 GB RSS, with admission.sqlite-journal open (written 4:48:44 PM); last heartbeat 4:39:41 PM ET. fuser: pids 2827 (_admit) and a job _host submit held admission.sqlite. Probes (job _host probe) piled up.
- admission.sqlite 51 MB: jobs 1,050, holds 402, samples 491,139, recent 2.
- 4:52 PM ET: restarted with 'job admission sttp2 stop' / 'start'. systemd: old loop consumed 11 h 15 min CPU, 9.8 GB memory peak. The two running job scopes survived.
- The new loop (pid 2409449) reached 7-8 GB RSS within 90 s and had not written a heartbeat by 4:54 PM ET. Cause not found; py-spy is not installed on sttp2. Memory: 84 GB available on sttp2.
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-27 5:27 PM ET: cause: a resize file written with the loop's soft-limit entries; overlay() added them again on each read of the queue file (85,104 copies in job d3bca5..., 1.4 GB). Fix 8e62fed (0.9.2): resize file holds only hard resizes; overlay merges without repeats; jobs record deduped on write and at job end; regression test. Full review Claude Sonnet (sonnet) at high: no blocking defect; F1 (sqlite record) and F2 (doc) fixed. Local suite: two failures are flaky on main too (memory-peak test failed 1 of 3 on main). Remote suite on sttp1: OK. Installed on local, sttp1, sttp2 (sha256 match); both loops restarted.
<!-- SECTION:NOTES:END -->
