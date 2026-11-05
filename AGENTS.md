# agent-pueue agent instructions

`job` is a thin wrapper around pueue that agents use to run long jobs on this
machine or remote hosts and to be woken exactly once, with the real outcome,
when a job ends. Usage: `docs/usage.md`.

- The whole tool is one stdlib-only Python file, `bin/job` (Python 3.8+). The
  same file runs on every host; keep it that way.
- Pueue owns all process tracking. Never find a process by its name or command
  line; the job's processes are found only by the token in their environment
  and their descendants.
- Nothing a user typed goes into an ssh command line: requests and command text
  travel over ssh stdin as JSON.
- A change to `bin/job` is done when `python3 -m unittest discover -s tests -v`
  passes and `JOB_TEST_HOST=<host> python3 -m unittest tests.test_remote -v`
  passes against a real remote host; then bump `VERSION` and run `./install.sh`.
  Change `PROTOCOL` when the JSON between client and host changes.
- Tests use a throwaway pueued in a `mktemp -d` folder; never test against a
  real daemon's jobs except the opt-in `JOB_TEST_REAL_DAEMON` test.
- This repository is public. Keep machine names, user names, paths and other
  details of any one deployment out of it.

<!-- ORG-STACK RECORDS START (edit the table freely, keep the markers) -->
## Where records live

| Purpose | Where |
|---|---|
| Work queue and open bugs | `backlog/` via the `backlog` CLI |
| Usage and design | `docs/usage.md` |
| Release notes | `CHANGELOG.md`, generated from Conventional Commits and `vX.Y.Z` tags: `git-cliff -o CHANGELOG.md && markdownlint-cli2 --fix CHANGELOG.md`. Never hand-edit. |
| Docs hygiene | `markdownlint-cli2 && lychee --no-progress --offline --include-fragments --exclude-path node_modules --exclude-path backlog --exclude-path docs/archive **/*.md` runs markdownlint-cli2 and lychee (offline link check). Run it before committing documentation. |

Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:` ...) because the changelog is generated from them.
<!-- ORG-STACK RECORDS END -->
