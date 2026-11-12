---
id: TASK-004
title: >-
  sttp2 pueued restarted twice on 2026-11-07 (13:48:44 and 18:29:32 ET), killing
  every running task and leaving its group paused; cause unknown
status: To Do
assignee: []
created_date: '2026-11-12T20:57:00Z'
labels: []
dependencies: []
priority: high
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found by the Starla manager session 2026-11-12 15:56 ET. sttp2 pueued (systemd user unit pueued.service, Restart=on-failure, jge lingering on) restarted at 13:48:44 ET and 18:29:32 ET on 2026-11-07 (current pueued started 18:29:32). Each restart: every running task marked 'killed by system or user' (starla-pilot jobs sttp2#80-84 and #85-89, a dataorg etdisk11 check), and group starla-pilot left paused, so its queued jobs never started; job wait clients saw 'Connection refused' on /run/user/1000/pueue_jge.socket and their waits ended. No cron, timer, binary change (pueue binaries dated 2026-11-05 14:42) or Claude session on KEVIN-PC-1 found that restarted it; journalctl (system and _SYSTEMD_USER_UNIT=pueued.service) has no entries for those minutes. Cost: the Starla HMI pilot rerun (student-help SH-038.17.20.05.12) sat paused from 2026-11-07 18:29 ET to 2026-11-12 15:57 ET. Needed: find what restarts pueued; and job should detect a restart (jobs killed by a daemon restart, groups left paused) and say so in job ls / wake the waiting session instead of the wait just dying.
<!-- SECTION:DESCRIPTION:END -->
