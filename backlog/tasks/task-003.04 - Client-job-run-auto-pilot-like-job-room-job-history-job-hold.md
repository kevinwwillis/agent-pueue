---
id: TASK-003.04
title: 'Client: job run auto, --pilot, --like, job room, job history, job hold'
status: To Do
assignee: []
created_date: '2026-11-09T17:00:00Z'
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
Callers need one command that places a job without choosing a host, sizes it from its pilot, and wakes them exactly as job run does today. Services that use GPUs outside job (model servers, for example) register holds, so there is one record of who holds each card; holds and jobs are first come. A job still queued after a wait moves to another host when room opens there. Jobs report progress through a file, so their end time is projected from measurement rather than guessed.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 job run auto places or queues a job and wakes the caller like job run
- [ ] #2 A queued job moves to another host with room after a wait, and never runs twice
- [ ] #3 job room shows the room on every host
- [ ] #4 job hold and job release register and free GPU holds, first come
- [ ] #5 A job that writes progress gets a projected end time
- [ ] #6 The admission loop can send ALERT to the caller wait
<!-- AC:END -->
