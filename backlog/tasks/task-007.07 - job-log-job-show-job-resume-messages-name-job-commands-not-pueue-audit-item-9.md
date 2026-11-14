---
id: TASK-007.07
title: >-
  job log, job show, job resume; messages name job commands, not pueue (audit
  item 9)
status: In Progress
assignee: []
created_date: '2026-11-14T17:41:00Z'
updated_date: '2026-11-14T19:24:00Z'
labels: []
dependencies: []
parent_task_id: TASK-007
priority: high
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
About 100 raw pueue calls; pueue not on the hosts' non-login ssh PATH; job log guessed 5 times; the paused-group ALERT tells agents to run pueue start.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 job log HOST ID [--lines N] [--grep RE] [--full] [--token T]
- [x] #2 job show HOST ID: command, task, group, size, state, times, token, log path
- [x] #3 job resume HOST --group G
- [x] #4 No message tells agents to run a pueue command; the paused-group ALERT gives the exact job resume command
- [ ] #5 Remote test (tests.test_remote against a real host) passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-14 13:59 ET, commit d2dd953. New host ops log, show, resume (additive, PROTOCOL unchanged as for resize); a host with an older job gets 'predates job <op>'. --full sends at most the last 32 MB. The loop's group_paused alert carries {resume: {group}} and the waiter, which knows the host's name, prints the command. Tests: LogShowResumeTests (4), paused-group alert test extended.
<!-- SECTION:NOTES:END -->
