---
id: TASK-005
title: >-
  pueued -vv floods sttp1/sttp2 journals; every other log survives only about 5
  hours
status: To Do
assignee: []
created_date: '2026-11-14T14:24:00Z'
updated_date: '2026-11-18T16:29:00Z'
labels: []
dependencies: []
priority: high
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found 2026-11-14 09:23 ET by the TARS/MO manager. pueued.service on sttp1 and sttp2 (jge user unit) runs 'pueued -vv'. On sttp2 it wrote 1,108,294 journal lines in the last hour (the next unit had 6,659), mostly debug dumps of the task list (status: Done { priority: 0, label: Some(..., envs: hidden ...). The journal is capped at 4G (journald.conf.d/50-retention.conf), so it now reaches back only to 2026-11-14 04:14 ET: logs of every service (TARS worker, Meeting Ops, JupyterHub, sshd) are gone after ~5 h. This also explains TASK-004's 'journalctl has no entries for those minutes'. Needed: run pueued at normal verbosity (no -vv) on both servers and in the install path that writes the unit; check the journal then reaches back days.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 pueued runs without -vv on sttp1 and sttp2, and the installer writes it that way
- [ ] #2 sttp2's journal covers more than 3 days a few days after the change
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-18 11:26 ET (data-org manager): sttp2 / at 95% (46 GB free): /var/log/syslog 316 GB since 11-15 00:02 ET plus syslog.1 92 GB, almost all pueued -vv task-list dumps (193,104 of the last ~194k lines in 20 MB). Growth 172 KB/s measured over 60 s, about 1 MB/s average since 11-15. sttp1: syslog 94 GB + syslog.1 46 GB, / at 40%. Stopgap asked of Kevin (aqua c976e096161a): rsyslog filter dropping pueued lines, delete syslog.1.

2026-11-18 11:28 ET: stopgap done on sttp2 with Kevin's OK (aqua c976e096161a): /etc/rsyslog.d/10-drop-pueued.conf drops pueued lines from syslog (journal still gets them); syslog.1 (92 GB) removed; / at 85%. Remove the filter once pueued runs without -vv. sttp1 unchanged (syslog 94+46 GB, / at 40%).
<!-- SECTION:NOTES:END -->
