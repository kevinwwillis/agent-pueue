---
id: TASK-018
title: >-
  Jobs reserve only what they use: pilot time ceiling, pilots counted at
  measured use, automatic shrink and grow
status: Done
assignee: []
created_date: '2026-11-17T20:35:00Z'
updated_date: '2026-11-17T22:39:00Z'
labels:
  - admission
dependencies: []
priority: high
ordinal: 36000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Kevin, 2026-11-17 15:20 ET: "pilots are apparently allowed to reserve the entire PC indefinitely. and the software is apparently completely stupid and has no way to auto resize jobs to match what they actually use". Readback confirmed in aqua bc45db1dd27c (2026-11-17 15:35 ET): build it; pilot time ceiling 60 min; auto-grow yes; ask the 18 h pilot's owner to stop it; check the sttp1-strata 78 GB hold.

Found 2026-11-17 15:29 ET: a pilot gets a RAM limit of all free RAM at release and is counted at it for its whole run; --max-minutes has no upper bound; pilots are never resized. Live on sttp2: pilot 1ab06b90 (SH-038.17.20.06-2016-07) --max-minutes 1080, counted 49.5 GB, peak 4.5 GB, four jobs held behind it. Oversized jobs only get an ALERT with a resize command the owner must run; RAM limits are never raised.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 No job can be counted at more than its measured peak plus margin once measured
- [x] #2 Automatic shrink and grow are live on sttp1 and sttp2
<!-- AC:END -->
