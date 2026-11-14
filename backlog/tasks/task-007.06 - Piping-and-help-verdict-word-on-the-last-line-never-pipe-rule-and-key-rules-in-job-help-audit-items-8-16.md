---
id: TASK-007.06
title: >-
  Piping and help: verdict word on the last line, never-pipe rule and key rules
  in job --help (audit items 8, 16)
status: In Progress
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T19:24:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
About 140 piped job run/wait calls; 8 failed jobs reported as exit 0 by the harness. Agents learn job from --help (about 70 calls). Last line becomes 'job: FAILED host#id exit N'; usage.md and --help epilog: never pipe (use --tail), shared default group, size with a pilot, kill removes queued jobs.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The last line of a run or wait is job: VERDICT HOST#ID exit N (wrapper errors: job: WRAPPER_ERROR exit 205)
- [x] #2 docs/usage.md and job run/wait --help say never pipe, use --tail N
- [x] #3 job --help epilog: never pipe, session groups, size with --pilot then --like, kill removes queued/held, reattach, job log/show/resume
- [ ] #4 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:53 ET, commit 697cf3e (epilog lines for --mine and job log/show/resume added with those commits). Several jobs: the last line names the job that decided the exit code, with a summary. Tests: HelpAndLastLineTests (3) and updated success/failure tests.
<!-- SECTION:NOTES:END -->
