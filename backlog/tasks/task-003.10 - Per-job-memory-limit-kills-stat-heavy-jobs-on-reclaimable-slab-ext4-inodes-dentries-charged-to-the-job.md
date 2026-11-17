---
id: TASK-003.10
title: >-
  Per-job memory limit kills stat-heavy jobs on reclaimable slab (ext4 inodes,
  dentries charged to the job)
status: To Do
assignee: []
created_date: '2026-11-17T10:25:00Z'
labels: []
dependencies: []
parent_task_id: TASK-003
priority: high
ordinal: 35000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-17 by data-organization (TASK-044.11.03): sttp2 job 746, a proof that stats about 6 M files across ext4 disks, was OOM-killed at its 2.5 GB limit with python anon-rss 562 MB; the job cgroup held 0.58 GB anon, 1.92 GB slab_reclaimable (ext4 inodes and dentries) and 0 page cache. The failing allocation was GFP_NOFS (ext4), which cannot reclaim fs slab, so the kernel killed the job instead of dropping the cache. Job 747 (same work): anon 0.52-0.59 GB, slab rising about 4.7 MB/s; writing to its memory.reclaim dropped slab 1.82 GB to 17 MB in 5.6 s with anon untouched. So any stat- or find-heavy job is sized and killed by kernel cache, not by what it uses, and its measured peak (job history) overstates its size. Options to weigh: memory.high below memory.max so the kernel throttles and reclaims before the hard limit; periodic memory.reclaim from the job wrapper; count anon (not slab) in measured peaks. data-organization is adding a per-chunk self-reclaim in its own tool meanwhile.
<!-- SECTION:DESCRIPTION:END -->
