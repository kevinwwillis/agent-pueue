---
id: TASK-003.06
title: Room counts a scope whose queue entry is gone as free for up to a cycle
status: To Do
assignee: []
created_date: '2026-11-09T22:04:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
compute_room adds back the RAM of every job scope it measures (free_no_jobs) but subtracts only the jobs in its in-flight list. A scope that outlives its queue entry (a job that ended or was removed while its processes were still exiting) has its RAM counted as free for up to one cycle, until clean_scopes removes it, so a job released in that cycle can overcommit RAM by that scope's use. Found in the second review of limit counting (2026-11-09 17:00 ET). It is the likely cause of one failure of test_history_shows_what_a_pilot_was_counted_at (3.9 MB over) in a full run, which passed alone and on a rerun. Fix: count a measured scope with no in-flight entry at its current use in the room (not in the room computed as if no admitted job ran).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A measured scope with no in-flight entry is counted at its current use in the room
- [ ] #2 The room as if no admitted job ran is unchanged
- [ ] #3 A test covers a scope that outlives its queue entry
<!-- AC:END -->
