---
id: TASK-002
title: 'job: option to run a job at normal CPU and disk priority'
status: To Do
assignee: []
created_date: '2026-11-07T02:33:00Z'
labels:
  - feature
dependencies: []
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
job always runs a job as 'nice -n 19 ionice -c 3 bash <script>' (bin/job, the runner string in the host-side submit). Some projects have a standing rule that their long server jobs run at normal priority (for example a data-organization project whose owner decided on 2026-11-02 17:16 ET that its copies, syncs and hash checks must not run at idle priority, because slowing them only keeps the work going longer). Today such a project cannot follow its rule through job. Add an explicit per-job option (run, submit) that runs the job at normal priority; the default stays nice 19 / ionice idle. The option travels in the JSON request (bump PROTOCOL if needed), is shown in job ls if cheap, and is documented in docs/usage.md.
<!-- SECTION:DESCRIPTION:END -->
