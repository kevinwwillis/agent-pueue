---
id: TASK-003.04
title: 'Client: job run auto, --pilot, --like, job room, job history, job hold'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T20:03:00Z'
labels: []
dependencies:
  - TASK-003.01
  - TASK-003.03
parent_task_id: TASK-003
priority: high
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Callers need one command that places a job without choosing a host, sizes it from its pilot, and wakes them exactly as job run does today. job run auto asks every admission host for its room and submits with fit_now to each host with room in turn; if none accepts, the job is held on the first host in the order (held jobs do not move between hosts: moving broke reattach and could run a job twice; spec review 2026-11-09). Services that use GPUs outside job (model servers, for example) register holds, so there is one record of who holds each card; holds and jobs are first come. Jobs report progress through a file, so their end time is projected from measurement rather than guessed.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 job run auto places or queues a job and wakes the caller like job run
- [x] #2 job room shows the room on every host
- [x] #3 job hold and job release register and free GPU holds, first come
- [x] #4 A job that writes progress gets a projected end time
- [x] #5 The admission loop can send ALERT to the caller wait
- [x] #6 job run auto tries hosts with room now (fit_now) and otherwise holds the job on one host; a retried submit never runs it twice
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 13:36 ET: description and criterion 2 brought in line with docs/admission.md (no moving held jobs between hosts; set aside in the spec's second review). The host side of fit_now, room and records exists (TASK-003.03); this task adds the client flags, auto placement, job room, job hold/release, progress and ALERT relay.

2026-11-09 13:43 ET: client written: size and constraint flags on run and submit (--pilot, --max-minutes, --like, --small, --cpus, --ram, --gpus, --gpu-mem, --gpu-share, --gpu-model, --data, --stop-at, --in-containers, --restartable, --host for auto); sizes as 20G (10^9) or 512Mi (2^20); job run auto (asks every host's room in parallel, orders hosts with room first, then the host that has --data, then fewest held; fit_now to each; else held on the first); job room (text and --json); wait prints why a job is held every 10 minutes, relays the loop's alerts as ALERT, and ALERTs once when the loop of a held job's host is not running; job ls shows Held with the reason. Not yet: holds (job hold/release), progress file and projected end. Tests: 33 admission tests pass here and on sttp1 (systemd 255); measurement checked on sttp1 (NVML and nvidia-smi agree on both cards; disks by path).

2026-11-09 14:13 ET: client repairs from review cycle 1 in 477d1be: auto tries the next host on a refusal (never after Unreachable), HOST#ID only to HOST, sized-like line printed, --needs-path, old hosts shown as predating admission in job room, this machine asked once. Holds and job admission start|stop|status built in 581a9ca.

2026-11-09 15:04 ET: released as job 0.6.0 (tag v0.6.0, main 6150f18), installed on this PC, sttp1 and sttp2 with the same sha256 (3c401842...). Evidence per criterion: 1, 3, 4, 5 and 6 by the suite (188 tests here; 163 on sttp1, where auto placement ran against two throwaway hosts; 12 remote tests against sttp1); 2 live: job room at 15:03 ET showed sttp1's measured room and named this PC and sttp2 as not admission hosts. Review cycle 2 repairs in c571672.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
The admission client in job 0.6.0: size flags (--small, --pilot, --like, --cpus/--ram/--gpus/--gpu-mem), job run auto, job room, job history, job hold and job release for GPU holds, held and ALERT relay in the wait, and progress files with a projected end and a RAM-trend ALERT. Two full reviews by Claude Sonnet 5.5 (sonnet) at high; repairs in 477d1be and c571672. Verified by the test suite here and on sttp1, and live on sttp1 since admission went on there at 14:51 ET. Open elsewhere: rented hosts (hosts.d) under TASK-003.
<!-- SECTION:FINAL_SUMMARY:END -->
