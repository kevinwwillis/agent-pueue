---
id: TASK-002
title: 'job: option to run a job at normal CPU and disk priority'
status: Done
assignee: []
created_date: '2026-11-07T02:33:00Z'
updated_date: '2026-11-07T15:58:00Z'
labels:
  - feature
dependencies: []
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
job always runs a job as 'nice -n 19 ionice -c 3 bash <script>' (bin/job, the runner string in the host-side submit). Some projects have a standing rule that their long server jobs run at normal priority (for example a data-organization project whose owner decided on 2026-11-02 17:16 ET that its copies, syncs and hash checks must not run at idle priority, because slowing them only keeps the work going longer). Today such a project cannot follow its rule through job. Add an explicit per-job option (run, submit) that runs the job at normal priority; the default stays nice 19 / ionice idle. The option travels in the JSON request (bump PROTOCOL if needed), is shown in job ls if cheap, and is documented in docs/usage.md.
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-07 10:58 ET: shipped as job 0.5.0 (merge 6e3d0dd), installed on this PC, sttp1 and sttp2 (sha256 49e1162729d77822... on all three). --normal-priority on run/submit; a priority submit uses protocol 4, which an old host refuses (fail closed); every other request stays protocol 3, so running waits survive the install. Two full reviews (Sonnet, high); local suite 67 OK, remote suite on sttp1 12 OK; checked by hand: a --normal-priority job on sttp1 and sttp2 shows nice 0 and ionice none. Low items left: the old-host test simulates the host by a constant, and the remote test's mismatch branch passes silently.
<!-- SECTION:NOTES:END -->
