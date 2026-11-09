---
id: TASK-003.02
title: 'Per-job cgroup scope: memory limit without swap, whole-job kill, GPU by UUID'
status: To Do
assignee: []
created_date: '2026-11-09T17:00:00Z'
labels: []
dependencies:
  - TASK-003.01
parent_task_id: TASK-003
priority: high
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Admitted jobs need hard RAM limits and exact per-job measurement. Running each job in its own systemd user scope gives both: MemoryMax at its size, MemorySwapMax=0 so an over-size job cannot spill into swap and slow everyone, OOMPolicy=kill so it dies whole instead of losing one worker and running on half-broken, and per-job memory and CPU counters. The GPU is chosen by UUID because CUDA's default device order can differ from nvidia-smi's. Default environment keeps TensorFlow and JAX from reserving most of a card. Related: TASK-001 (job kill leaves child processes running), which a scope kill may also fix.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Each admitted job runs in its own systemd user scope with a memory limit, no swap and whole-job kill on out-of-memory
- [ ] #2 A job over its memory limit is stopped whole and job reports why
- [ ] #3 The GPU a job gets is set by UUID
- [ ] #4 Stall detection still works for jobs in scopes
- [ ] #5 Tests cover a job that goes over its memory limit
<!-- AC:END -->
