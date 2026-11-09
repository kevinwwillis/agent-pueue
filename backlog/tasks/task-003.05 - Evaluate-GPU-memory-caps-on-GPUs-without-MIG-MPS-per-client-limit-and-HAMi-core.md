---
id: TASK-003.05
title: >-
  Evaluate GPU memory caps on GPUs without MIG: MPS per-client limit and
  HAMi-core
status: To Do
assignee: []
created_date: '2026-11-09T17:00:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Several jobs can share a GPU safely only if each is held to its memory size, and consumer GPUs have no MIG partitioning. Two candidates: NVIDIA MPS with CUDA_MPS_PINNED_DEVICE_MEM_LIMIT per client (an HTCondor site runs this), and HAMi-core loaded with LD_PRELOAD (CUDA_DEVICE_MEMORY_LIMIT). Both are soft: a job can change its own environment. Open points: processes outside job must not be routed into MPS; whether per-job GPU memory can still be measured under MPS; how a crash in one client affects its neighbors. Without a cap, sharing relies on measured pilot peaks plus headroom.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Each cap is tested with the frameworks the jobs use, on a shared GPU, and the results are written up in docs/
- [ ] #2 Processes not started by job keep running normally beside an MPS server
- [ ] #3 The write-up recommends one cap or none, with evidence
<!-- AC:END -->
