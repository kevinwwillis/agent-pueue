---
id: TASK-003.02
title: 'Per-job cgroup scope: memory limit without swap, whole-job kill, GPU by UUID'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T18:10:00Z'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Protocol 5 for submits that carry a scope request; hosts accept 3, 4, 5; a scope request at any other protocol is refused.
2. host_submit: validate the scope request (memory_max bytes, gpus as UUIDs or null, gpu_env, oom_score_adj); write a scoped wrapper (systemd-run --user --scope --quiet --unit=agent-pueue-job-<token> -p MemoryMax -p MemorySwapMax=0 -p OOMPolicy=kill, XDG_RUNTIME_DIR set) and an in-scope shim (started marker, oom_score_adj, runs the command, writes final memory.peak/memory.stat/memory.events/cpu.stat); the wrapper writes the exit record and a scope result file, then reset-failed.
3. GPU env: CUDA_VISIBLE_DEVICES and JOB_GPU_UUIDS from the assigned UUIDs, CUDA_DEVICE_ORDER=PCI_BUS_ID, gpu_env defaults unless the job's env sets them; --env CUDA_VISIBLE_DEVICES refused with a scope.
4. host_probe returns the parsed scope result; the client's wait turns a missing started marker into WRAPPER_ERROR and adds the memory-kill reason to the verdict note.
5. host_kill also stops the job's scope.
6. Tests (throwaway pueued, real user scopes, skipped where systemd-run --user is unavailable): over the memory limit (whole job stopped, reason reported), clean exit with final peaks, systemd-run failure, GPU env, kill with a detached child, stall detection in a scope, protocol checks. Then full review, remote test on one host, VERSION bump, install.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 12:45 ET: scope code written in bin/job (protocol 5, _check_scope, wrapper and shim, _scope_result, _stop_scope on kill, scope_verdict). tests/test_scope.py: 12 tests on real user scopes with a throwaway pueued (over the memory limit, clean exit with final peaks, limits in effect, systemd-run failure, protocol 5 required, bad request, GPU env by UUID, no GPU hides all, CUDA_VISIBLE_DEVICES in --env refused, kill stops a setsid child, stall found inside the scope, unscoped submit unchanged). Full local suite: 67 pass, 12 remote skipped. Full review next.

2026-11-09 13:10 ET: first full review (Claude Sonnet 5.5, sonnet, high) found 8 groups of defects. Root cause of most: the shim shared the job's cgroup, so a memory kill took the bookkeeping with it. Redesign (my call): the scope is delegated (Delegate=yes, OOMPolicy=continue) and the shim splits it into shim/ and job/; job/ carries memory.max, swap 0, oom.group 1; the shim survives, reads job/ memory.events and tells own limit (oom>0) from host OOM (oom_kill>0, oom 0) from outside SIGKILL, on systemd 255 too (verified on sttp1 and this PC). Fixed: limits read back, fail closed (1); job kill SIGKILLs the scope at once and keeps a usage snapshot (2); peak renamed memory_peak_incl_cache, not used for verdicts (3); attribution per spec (4); bookkeeping outside the limit (5); leftovers stopped at end, restart clears an old scope (6); VERSION 0.6.0, spec and usage.md updated (7); gpus default [] (fail closed), GPU variables refused in gpu_env and --env, MIG names, systemctl timeouts, test hook JOB_SYSTEMD_RUN removed (fake HOME instead), skip unless memory is delegated, tests for GONE path, normal priority, protocol 5 (8). Added: the shim refuses to touch any cgroup that is not its own scope. Set aside: TasksMax (not in the spec; a fork runaway is bounded by the user slice's TasksMax, and a per-job limit could break wide worker pools). 78 local tests pass. Second and final review next.
<!-- SECTION:NOTES:END -->
