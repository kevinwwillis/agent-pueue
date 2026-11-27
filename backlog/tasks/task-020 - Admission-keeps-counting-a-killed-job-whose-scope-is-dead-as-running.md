---
id: TASK-020
title: Admission keeps counting a killed job whose scope is dead as running
status: To Do
assignee: []
created_date: '2026-11-27T05:25:00Z'
labels: []
dependencies: []
priority: medium
ordinal: 45000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-27 00:25 ET by the data-org sessions on sttp2. pueue task #1158 (dr-battery2, token 6598e003f4eb4f91871148c2f5a81f88) is Done: KILLED exit 137, ended 2026-11-25 11:36 PM ET; no process carries its token; its scope agent-pueue-job-6598e003....scope is loaded inactive dead. Yet job room on sttp2 still lists it as running (1 CPU, 4.0 GB RAM, counted at 3.0 GB), so admission holds that room. job kill refuses (task is Done, exit 205) and usage.md names no command for this case; admission.md says the loop stops a leftover scope only while it still has processes. Wanted: a finished pueue task, or a dead scope with no processes, stops counting toward admitted room on the next loop pass; add a test for a killed task.
<!-- SECTION:DESCRIPTION:END -->
