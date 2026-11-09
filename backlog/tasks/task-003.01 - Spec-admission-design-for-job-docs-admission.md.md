---
id: TASK-003.01
title: 'Spec: admission design for job (docs/admission.md)'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-09T17:00:00Z'
updated_date: '2026-11-09T17:19:00Z'
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
- [ ] #1 docs/admission.md covers commands, room rules, the admission loop, scopes, records, the settings file, progress and rollout
- [ ] #2 The spec names no machine, user or path of any one deployment
- [ ] #3 A full review ran on the spec and each finding is fixed or set aside with its reason
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-09 12:05 ET: spec written to docs/admission.md on branch feat/admission (935996f, pushed). Full review by Claude Sonnet 5.5 (sonnet) at high running.

2026-11-09 12:20 ET: first full review (Claude Sonnet 5.5, sonnet, high) found 20 defects; spec rewritten (9ba8671). Set aside: moving held jobs between hosts (dropped from v1: it broke reattach and could run a job twice), --progress-from (ran caller text in the loop), --not-after (needed a total nobody supplies; replaced by --stop-at), --hold-gpu-free (replaced by job hold). Second and final review running.
<!-- SECTION:NOTES:END -->
