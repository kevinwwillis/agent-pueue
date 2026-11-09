---
id: TASK-003.01
title: 'Spec: admission design for job (docs/admission.md)'
status: To Do
assignee: []
created_date: '2026-11-09T17:00:00Z'
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
