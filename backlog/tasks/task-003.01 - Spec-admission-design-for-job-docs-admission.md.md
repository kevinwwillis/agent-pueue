---
id: TASK-003.01
title: 'Spec: admission design for job (docs/admission.md)'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T17:36:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The admission work needs one written design before any code: the commands, how room is counted for CPU, RAM, GPU memory, GPU time and disk, how the per-host loop releases held jobs from pueue, per-job cgroup scopes, job records, the host settings file, progress and projected end times, protocol compatibility, and rollout without restarting pueued (a restart kills running jobs). The deployment that asked for this keeps a full design proposal in its own records; this spec is the public form, with no deployment details.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 docs/admission.md covers commands, room rules, the admission loop, scopes, records, the settings file, progress and rollout
- [x] #2 The spec names no machine, user or path of any one deployment
- [x] #3 A full review ran on the spec and each finding is fixed or set aside with its reason
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 12:05 ET: spec written to docs/admission.md on branch feat/admission (935996f, pushed). Full review by Claude Sonnet 5.5 (sonnet) at high running.

2026-11-09 12:20 ET: first full review (Claude Sonnet 5.5, sonnet, high) found 20 defects; spec rewritten (9ba8671). Set aside: moving held jobs between hosts (dropped from v1: it broke reattach and could run a job twice), --progress-from (ran caller text in the loop), --not-after (needed a total nobody supplies; replaced by --stop-at), --hold-gpu-free (replaced by job hold). Second and final review running.

2026-11-09 12:50 ET: second and final full review (Claude Sonnet 5.5, sonnet, high) found 18 defects; spec revised (39ab130). Set aside, with reasons: moving held jobs between hosts (still out of v1; replaced by fit_now submits under a per-host queue lock, which stops batches piling on one host); MemoryHigh at the size (throttling raises memory pressure that systemd-oomd acts on; a hard limit at size x ram_hard_factor with room counted at max(size, use) instead); two pilots at n and 2n (replaced by the RAM-trend ALERT from progress lines); an escape hatch to skip admission (bypass already shows as outside use); refusing admission on hosts without a systemd user manager (documented instead; the heartbeat makes a dead loop visible).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
docs/admission.md specifies admission for job: per-host loop, sizes from measured pilots or explicit sizes, room rules for GPU memory, RAM, CPU, GPU time and disk with outside use measured, per-job systemd scopes with wrapper/shim layers, holds, records, alerts, placement with fit_now, protocol 5 negotiation and a one-host-at-a-time rollout. Two full reviews (38 findings) were applied or set aside with reasons in the notes. No code yet; TASK-003.02-.05 build it.
<!-- SECTION:FINAL_SUMMARY:END -->
