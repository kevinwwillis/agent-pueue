---
id: TASK-003.08
title: >-
  A hold whose --while-pid process has exited still counts until the loop's next
  cycle, so an immediate reload of the same service is refused against its own
  stale hold
status: To Do
assignee: []
created_date: '2026-11-14T17:52:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 33000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Reported 2026-11-14 12:52 ET by a model-service owner: a 'sleep 20' test hold was released 31.9 s after it was granted (about 12 s after its process exited). host_hold reads room from the last snapshot, which still counts the dead hold, so a reload in that window is counted against its own hold; a guard-side job release cannot help because the snapshot still counts it. Fix: host_hold credits holds whose --while-pid process (or cgroup/unit) is already gone when it computes room.
<!-- SECTION:DESCRIPTION:END -->
