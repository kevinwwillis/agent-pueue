---
id: TASK-018.05
title: >-
  Check the sttp1-strata service hold (78 GB RAM since 2026-11-17 13:20 ET)
  against its real use; fix the hold size if oversized
status: Done
assignee: []
created_date: '2026-11-17T20:36:00Z'
updated_date: '2026-11-17T20:36:00Z'
labels: []
dependencies: []
parent_task_id: TASK-018
priority: high
ordinal: 41000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The hold's RAM matches measured use plus margin, or the reason it needs 78 GB is recorded
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-17 15:37 ET: strata-host RSS 73.2 GB (ps on sttp1); hold 78.0 GB = use plus about 7%. room.json: free_no_jobs 110.7 GB already adds back the held service's own use, then subtracts the 78 GB hold, jobs counted 18.7 GB and 16 GB headroom = -2.1 GB. No double count; the hold is honest. sttp1 is genuinely full; the room auto-shrink (TASK-018.03) frees is the jobs' counted-minus-peak (18.7 GB counted vs 10.5 GB peaks now).
<!-- SECTION:NOTES:END -->
