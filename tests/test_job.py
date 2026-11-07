"""Real tests of `job` against a real, throwaway pueued.

Each test class starts its own pueued with its own config, data and socket in a
fresh `mktemp -d` folder (short path under /tmp: unix socket paths are limited
to 108 bytes), points `job` at it with JOB_PUEUE_CONFIG, and removes the folder
afterwards. No callback is configured.

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = os.path.join(REPO, "bin", "job")


def _read(path):
    with open(path) as f:
        return f.read().strip()


class Daemon:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        self.cfg = os.path.join(self.dir, "pueue.yml")
        os.makedirs(os.path.join(self.dir, "run"))
        with open(self.cfg, "w") as f:
            f.write(textwrap.dedent("""\
                shared:
                  pueue_directory: {d}/data
                  runtime_directory: {d}/run
                  use_unix_socket: true
                  unix_socket_path: {d}/run/pueue.socket
                daemon:
                  callback: null
                """).format(d=self.dir))
        self.log = open(os.path.join(self.dir, "daemon.log"), "w")
        self.proc = subprocess.Popen(["pueued", "-c", self.cfg], stdout=self.log, stderr=self.log)
        for _ in range(100):
            if os.path.exists(os.path.join(self.dir, "run", "pueue.socket")):
                break
            time.sleep(0.05)
        self.pueue("parallel", "8")

    def restart(self, down_for=3.0):
        """Stop pueued (SIGTERM) and start it again after down_for seconds."""
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(10)
        time.sleep(down_for)
        sock = os.path.join(self.dir, "run", "pueue.socket")
        if os.path.exists(sock):
            os.remove(sock)
        self.proc = subprocess.Popen(["pueued", "-c", self.cfg], stdout=self.log, stderr=self.log)
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)

    def env(self, **extra):
        e = dict(os.environ)
        e["JOB_HOSTS_FILE"] = os.path.join(self.dir, "hosts")
        e["JOB_PUEUE_CONFIG"] = self.cfg
        e["XDG_STATE_HOME"] = os.path.join(self.dir, "state")
        e.pop("JOB_SESSION", None)
        e.update(extra)
        return e

    def pueue(self, *args):
        return subprocess.run(["pueue", "-c", self.cfg] + list(args), capture_output=True, text=True, check=True)

    def status(self, tid):
        st = json.loads(self.pueue("status", "--json").stdout)
        return st["tasks"][str(tid)]["status"]

    def stop(self):
        try:
            self.pueue("kill", "--all")
        except subprocess.CalledProcessError:
            pass
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        if self.dir.startswith("/tmp/jobt.") and os.path.isdir(self.dir):
            shutil.rmtree(self.dir)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    @staticmethod
    def load_job():
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("jobmod", JOB)
        spec = importlib.util.spec_from_loader("jobmod", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def job(self, *args, timeout=120, **env):
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, JOB] + list(args), capture_output=True, text=True,
                           env=self.d.env(**env), timeout=timeout)
        p.elapsed = time.monotonic() - t0
        return p

    def submit(self, *cmd, task="T-sub", extra=()):
        p = self.job("submit", "local", task, *extra, "--", *cmd)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return int(p.stdout.strip())

    def wait_status(self, tid, kind, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if kind in self.d.status(tid):
                return
            time.sleep(0.1)
        self.fail("task %s never reached %s: %s" % (tid, kind, self.d.status(tid)))


class JobTests(Base):
    # -- outcomes ------------------------------------------------------------

    def test_success(self):
        p = self.job("run", "local", "T-ok", "--poll", "0.3", "--", "echo hello; echo world")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        first = p.stdout.splitlines()[0]
        self.assertTrue(first.startswith("job: SUCCESS local#"), first)
        self.assertIn("(T-ok)", first)
        self.assertIn("world", p.stdout)
        self.assertTrue(p.stdout.rstrip().endswith("job: exit 0"))

    def test_failure_passes_exit_code(self):
        p = self.job("run", "local", "T-fail", "--poll", "0.3", "--", "echo before; exit 7")
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("job: FAILED local#", p.stdout)
        self.assertIn("exit 7", p.stdout.splitlines()[0])
        self.assertIn("before", p.stdout)

    def test_argv_command_is_quoted(self):
        # several arguments after -- are one argv, not a shell line
        p = self.job("run", "local", "T-argv", "--poll", "0.3", "--", "printf", "%s|", "a b", "$HOME")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("a b|$HOME|", p.stdout)

    def test_script_file(self):
        f = os.path.join(self.d.dir, "long.sh")
        with open(f, "w") as fh:
            fh.write("set -e\nx=41\necho \"answer $((x+1))\"\nexit 3\n")
        p = self.job("run", "local", "T-script", "--poll", "0.3", "--script", f)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("answer 42", p.stdout)

    def test_runs_niced_with_local_bin_on_path(self):
        p = self.job("run", "local", "T-nice", "--poll", "0.3", "--",
                     "echo nice=$(nice); ionice -p $$; echo path=$PATH")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("nice=19", p.stdout)
        self.assertIn("idle", p.stdout)
        self.assertIn("path=%s/.local/bin:" % os.path.expanduser("~"), p.stdout)

    def test_default_runner_is_idle_priority(self):
        job = self.load_job()
        r = job._runner("/x/s.sh")
        self.assertTrue(r.startswith("nice -n 19 ionice -c 3 bash "), r)
        self.assertEqual(r, job._runner("/x/s.sh", False))

    def test_normal_priority_runner_has_no_nice_or_ionice(self):
        r = self.load_job()._runner("/x/s.sh", True)
        self.assertNotIn("nice", r)
        self.assertEqual(r, "bash /x/s.sh")

    def test_normal_priority_job_runs_at_normal_priority(self):
        # compare with this test process's own niceness so the test also holds under a niced runner
        base = int(subprocess.run(["nice"], capture_output=True, text=True).stdout)
        p = self.job("run", "local", "T-norm", "--poll", "0.3", "--normal-priority", "--",
                     "echo nice=$(nice); ionice -p $$")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("nice=%d" % base, p.stdout)
        self.assertNotIn("idle", p.stdout)

    def test_submit_accepts_normal_priority(self):
        tid = self.submit("true", task="T-norm2", extra=("--normal-priority",))
        self.assertGreaterEqual(tid, 0)

    def test_request_json_carries_normal_priority(self):
        import argparse
        job = self.load_job()
        seen = []
        job.call = lambda host, op, req, **kw: seen.append(req) or {"id": 1, "token": req["token"]}
        for flag in (True, False):
            a = argparse.Namespace(host="local", task="T", group=None, cwd="/", env=None,
                                   normal_priority=flag)
            job.submit(a, "true")
        self.assertEqual([r["normal_priority"] for r in seen], [True, False])

    def test_host_refuses_non_boolean_normal_priority(self):
        job = self.load_job()
        with self.assertRaises(job.JobError):
            job.host_submit({"command": "true", "task": "T", "normal_priority": "yes"})

    def test_request_without_normal_priority_runs_at_default_priority(self):
        job = self.load_job()
        self.assertEqual(job.PROTOCOL, 3)
        # a request from an older client has no normal_priority field and a matching protocol
        p = subprocess.run([sys.executable, JOB, "_host", "submit"], capture_output=True, text=True,
                           input=json.dumps({"protocol": 3, "command": "true", "task": "T-old",
                                             "cwd": "/tmp"}),
                           env=self.d.env())
        reply = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(reply["ok"], reply)
        st = json.loads(self.d.pueue("status", "--json").stdout)
        cmd = st["tasks"][str(reply["data"]["id"])]["command"]
        self.assertTrue(cmd.startswith("nice -n 19 ionice -c 3 bash "), cmd)
        pr = self.job("run", "local", "T-old2", "--poll", "0.3", "--", "echo nice=$(nice); ionice -p $$")
        self.assertIn("nice=19", pr.stdout)
        self.assertIn("idle", pr.stdout)

    def test_killed_with_pueue_kill(self):
        tid = self.submit("sleep 300", task="T-pkill")
        self.wait_status(tid, "Running")
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        time.sleep(1)
        self.d.pueue("kill", str(tid))
        out, err = w.communicate(timeout=30)
        self.assertEqual(w.returncode, 137, out + err)
        self.assertIn("job: KILLED local#%d" % tid, out)

    def test_killed_from_outside_by_signal(self):
        # the job's own process is killed with SIGKILL by someone else (OOM killer, a user)
        tid = self.submit("echo $$ > pid; exec sleep 300", task="T-sigkill",
                          extra=("--cwd", self.d.dir))
        self.wait_status(tid, "Running")
        pidfile = os.path.join(self.d.dir, "pid")
        for _ in range(100):
            if os.path.exists(pidfile) and _read(pidfile):
                break
            time.sleep(0.05)
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        time.sleep(1)
        os.kill(int(_read(pidfile)), signal.SIGKILL)
        os.remove(pidfile)
        out, err = w.communicate(timeout=30)
        # pueue records a signal death as Failed with the shell's code, 128 + 9
        self.assertEqual(w.returncode, 137, out + err)
        self.assertTrue(out.startswith("job: FAILED local#%d" % tid), out)

    # -- stall -----------------------------------------------------------------

    def test_stall_reported_and_job_left_running(self):
        p = self.job("run", "local", "T-stall", "--poll", "0.3", "--stall", "0.05", "--",
                     "echo started; sleep 300")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        self.assertIn("job: STALLED local#", p.stdout)
        self.assertIn("left running", p.stdout)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.assertIn("Running", self.d.status(tid))
        self.d.pueue("kill", str(tid))

    def test_busy_silent_job_is_not_a_stall(self):
        # no output at all, but burning CPU: must not be called stalled
        p = self.job("run", "local", "T-busy", "--poll", "0.3", "--stall", "0.05", "--",
                     "timeout 6 sh -c 'while :; do :; done'; echo done")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("job: SUCCESS", p.stdout)

    def test_silent_child_work_is_not_a_stall(self):
        # the CPU is used by a grandchild that clears its environment
        p = self.job("run", "local", "T-child", "--poll", "0.3", "--stall", "0.05", "--",
                     "env -i /bin/sh -c 'timeout 6 sh -c \"while :; do :; done\"'; echo done")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_queued_job_is_not_a_stall(self):
        self.job("group", "local", "one", "1")
        blocker = self.submit("sleep 8", task="T-block", extra=("--group", "one"))
        self.wait_status(blocker, "Running")
        p = self.job("run", "local", "T-queued", "--group", "one", "--poll", "0.3", "--stall", "0.05",
                     "--", "echo ran")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    # -- check -------------------------------------------------------------------

    def test_check_alert_leaves_job_running(self):
        chk = os.path.join(self.d.dir, "check.sh")
        with open(chk, "w") as f:
            f.write("#!/bin/sh\nif grep -q ERROR; then echo 'found ERROR in output'; exit 1; fi\n")
        os.chmod(chk, 0o755)
        p = self.job("run", "local", "T-check", "--poll", "0.3", "--check", chk, "--",
                     "echo fine; sleep 1; echo 'ERROR disk full'; sleep 300")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("job: ALERT local#", p.stdout)
        self.assertIn("found ERROR in output", p.stdout)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.assertIn("Running", self.d.status(tid))
        self.d.pueue("kill", str(tid))

    def test_check_passes_through_clean_job(self):
        chk = os.path.join(self.d.dir, "check_ok.sh")
        with open(chk, "w") as f:
            f.write("#!/bin/sh\n! grep -q ERROR\n")
        os.chmod(chk, 0o755)
        p = self.job("run", "local", "T-check-ok", "--poll", "0.3", "--check", chk, "--",
                     "for i in 1 2 3; do echo line $i; sleep 0.5; done")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_check_sees_output_written_just_before_exit(self):
        chk = os.path.join(self.d.dir, "check_last.sh")
        with open(chk, "w") as f:
            f.write("#!/bin/sh\nif grep -q NaN; then echo NaN seen; exit 1; fi\n")
        os.chmod(chk, 0o755)
        p = self.job("run", "local", "T-check-last", "--poll", "1", "--check", chk, "--",
                     "sleep 0.2; echo 'loss NaN'")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("NaN seen", p.stdout)

    # -- wait ----------------------------------------------------------------------

    def test_wait_on_already_finished_job(self):
        tid = self.submit("echo finished-early; exit 5", task="T-early")
        self.wait_status(tid, "Done")
        p = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("finished-early", p.stdout)
        self.assertLess(p.elapsed, 10)

    def test_wait_on_missing_job(self):
        p = self.job("wait", "local", "99999", "--poll", "0.3")
        self.assertEqual(p.returncode, 204, p.stdout + p.stderr)
        self.assertIn("GONE", p.stdout)

    # -- the bug this tool exists for ------------------------------------------------

    def test_command_containing_the_wrappers_own_words(self):
        """The bug this tool exists for: a watcher matched its own command line and never fired.

        The job's command line holds every word a name-matching watcher would search
        for, and a decoy process outside pueue carries the same command line and keeps
        running. The wait must end when the job ends, not when the decoy ends.
        """
        words = "job wait local pueue pgrep -f mag_check.py JOB_TOKEN T-self"
        decoy = subprocess.Popen(["bash", "-c", "sleep 60 # " + words])
        try:
            p = self.job("run", "local", "T-self", "--poll", "0.3", "--",
                         "sleep 1; echo '%s'; exit 4 # %s" % (words, words))
            self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
            self.assertLess(p.elapsed, 20)
            self.assertIsNone(decoy.poll(), "decoy should still be running")
        finally:
            decoy.kill()
            decoy.wait()

    def test_decoy_cpu_does_not_hide_a_stall(self):
        words = "job run local T-decoy sleep 300"
        decoy = subprocess.Popen(["bash", "-c", "while :; do :; done # " + words])
        try:
            p = self.job("run", "local", "T-decoy", "--poll", "0.3", "--stall", "0.05", "--",
                         "sleep 300 # " + words)
            self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
            tid = int(p.stdout.split("#", 1)[1].split()[0])
            self.d.pueue("kill", str(tid))
        finally:
            decoy.kill()
            decoy.wait()

    # -- ls, groups, session ------------------------------------------------------------

    def test_ls_shows_task_and_session(self):
        p = self.job("run", "local", "T-ls", "--poll", "0.3", "--", "true", JOB_SESSION="sess-123")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ls = self.job("ls", "local")
        self.assertEqual(ls.returncode, 0, ls.stdout + ls.stderr)
        row = [l for l in ls.stdout.splitlines() if " T-ls " in l]
        self.assertTrue(row, ls.stdout)
        self.assertIn("sess-123", row[0])
        self.assertIn("SUCCESS", row[0])

    def test_group_parallel_limit(self):
        r = self.job("group", "local", "pair", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ids = [self.submit("sleep 4", task="T-g%d" % i, extra=("--group", "pair")) for i in range(3)]
        self.wait_status(ids[0], "Running")
        self.wait_status(ids[1], "Running")
        time.sleep(0.5)
        self.assertIn("Queued", self.d.status(ids[2]))
        p = self.job("wait", "local", str(ids[2]), "--poll", "0.3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_unknown_group_is_refused(self):
        p = self.job("submit", "local", "T-nogroup", "--group", "does-not-exist", "--", "true")
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("does not exist", p.stdout)

    def test_flags_after_task_are_flags_not_command(self):
        p = self.job("run", "local", "T-flags", "--poll", "0.3", "--tail", "1", "--", "echo a; echo b")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("last 1 log lines", p.stdout)


class RegressionTests(Base):
    """Regression cases from code review."""

    def chk(self, name, body):
        f = os.path.join(self.d.dir, name)
        with open(f, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(f, 0o755)
        return f

    def test_hung_job_with_heartbeat_stalls(self):
        # a hung job that still wakes every second (a wait loop, a logging thread)
        p = self.job("run", "local", "T-heartbeat", "--poll", "0.5", "--stall", "0.2", "--",
                     "echo started; while :; do sleep 1; done", timeout=180)
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.d.pueue("kill", str(tid))

    def test_busy_short_lived_children_are_not_a_stall(self):
        # CPU work done in a stream of short-lived children (counted through cutime)
        p = self.job("run", "local", "T-kids", "--poll", "0.3", "--stall", "0.05", "--",
                     "end=$((SECONDS+6)); while [ $SECONDS -lt $end ]; do timeout 0.3 sh -c 'while :; do :; done'; done")
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)

    def test_stall_zero_turns_it_off(self):
        p = self.job("run", "local", "T-nostall", "--poll", "0.3", "--stall", "0", "--", "sleep 3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_check_sees_a_line_split_across_chunks(self):
        c = self.chk("split.sh", "if grep -q 'CUDA out of memory'; then echo OOM; exit 1; fi")
        p = self.job("run", "local", "T-split", "--poll", "0.3", "--check", c, "--",
                     "printf 'RuntimeError: CUDA out of '; sleep 2; printf 'memory\\n'; sleep 1")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("OOM", p.stdout)

    def test_wait_check_resumes_after_alert(self):
        c = self.chk("err.sh", "if grep -q ERROR; then echo ERR; exit 1; fi")
        p = self.job("run", "local", "T-resume", "--poll", "0.3", "--check", c, "--",
                     "echo ERROR once; sleep 4; echo fine; exit 0")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        tid = p.stdout.split("#", 1)[1].split()[0]
        w = self.job("wait", "local", tid, "--poll", "0.3", "--check", c)
        self.assertEqual(w.returncode, 0, w.stdout + w.stderr)  # the old ERROR line is not checked again
        w = self.job("wait", "local", tid, "--poll", "0.3", "--check", c, "--from-start")
        self.assertEqual(w.returncode, 201, w.stdout + w.stderr)

    def test_bad_check_path_is_refused_before_queueing(self):
        p = self.job("run", "local", "T-badcheck", "--check", "/nonexistent/check.sh", "--", "true")
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertNotIn("queued", p.stderr)

    def test_wait_on_several_jobs(self):
        ids = [self.submit(c, task="T-multi") for c in ("sleep 1", "sleep 2; exit 3", "true")]
        p = self.job("wait", "local", *[str(i) for i in ids], "--poll", "0.3")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: ALL ENDED local: 1 FAILED, 2 SUCCESS"), p.stdout)
        self.assertEqual(p.stdout.count("job: SUCCESS"), 2)

    def test_several_jobs_end_early_on_a_stall(self):
        ids = [self.submit(c, task="T-multi-stall") for c in ("sleep 300", "sleep 20")]
        p = self.job("wait", "local", *[str(i) for i in ids], "--poll", "0.3", "--stall", "0.05")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        self.assertIn("still waiting", p.stdout.splitlines()[0])
        for i in ids:
            self.d.pueue("kill", str(i))

    def test_huge_progress_bar_log_tail_is_fast_and_short(self):
        gen = ("python3 -c \"import sys\nfor i in range(400000): sys.stdout.write('\\\\r%d%% |' % i + '#' * 80)\n"
               "sys.stdout.write('\\\\nfinished\\\\n')\"")
        p = self.job("run", "local", "T-tqdm", "--poll", "0.5", "--tail", "5", "--", gen)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(p.elapsed, 30)
        self.assertIn("finished", p.stdout)
        self.assertLess(len(p.stdout), 8000)

    def test_environment_is_an_allowlist(self):
        p = self.job("run", "local", "T-env", "--poll", "0.3", "--env", "PASSED=yes", "--",
                     "echo secret=${SECRET_X:-none} passed=$PASSED", SECRET_X="leak")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("secret=none passed=yes", p.stdout)


class MoreRegressionTests(Base):
    """More regression cases from code review."""

    chk = RegressionTests.chk

    def test_command_trap_cannot_erase_the_exit_record(self):
        sub = self.job("submit", "local", "T-trap", "--", "trap 'echo bye' EXIT; exit 6")
        tid = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Done")
        rec = os.path.join(self.d.dir, "state", "agent-pueue", "results", token + ".exit")
        self.assertEqual(_read(rec), "6")

    def test_no_queued_notice_when_a_slot_is_free(self):
        p = self.job("run", "local", "T-free", "--poll", "0.3", "--", "true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("queued behind", p.stderr)

    def test_pipeline_failure_is_not_success(self):
        p = self.job("run", "local", "T-pipe", "--poll", "0.3", "--", "echo x; exit 3 | cat")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        p = self.job("run", "local", "T-pipe2", "--poll", "0.3", "--",
                     "python3 -c 'import sys; sys.exit(4)' 2>&1 | tail -1")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)

    def test_alert_after_end_shows_the_jobs_exit(self):
        c = self.chk("e9.sh", "if grep -q ERROR; then echo ERR; exit 1; fi")
        # the first probe comes before the output; the next one finds the job ended
        p = self.job("run", "local", "T-alert-end", "--poll", "3", "--check", c, "--", "sleep 1; echo ERROR; exit 9")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("the job has also ended: FAILED exit 9", p.stdout)

    def test_check_sees_carriage_return_lines_while_running(self):
        c = self.chk("nan.sh", "if grep -q nan; then echo NaN; exit 1; fi")
        p = self.job("run", "local", "T-cr", "--poll", "0.3", "--check", c, "--",
                     "printf '\\repoch 1 loss=0.3'; sleep 1; printf '\\repoch 2 loss=nan'; sleep 1; "
                     "printf '\\repoch 3'; sleep 60")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertLess(p.elapsed, 30)
        self.assertIn("job was left running", p.stdout)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.d.pueue("kill", str(tid))

    def test_kill_removes_a_queued_job(self):
        self.job("group", "local", "solo", "1")
        a = self.submit("sleep 20", task="T-kq-a", extra=("--group", "solo"))
        b = self.submit("true", task="T-kq-b", extra=("--group", "solo"))
        self.wait_status(a, "Running")
        k = self.job("kill", "local", str(b))
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        self.assertIn("removed from the queue", k.stdout)
        self.d.pueue("kill", str(a))

    def test_queued_notice(self):
        self.job("group", "local", "solo2", "1")
        a = self.submit("sleep 4", task="T-qn-a", extra=("--group", "solo2"))
        self.wait_status(a, "Running")
        p = self.job("run", "local", "T-qn-b", "--group", "solo2", "--poll", "0.3", "--", "true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("queued behind 1 running job(s) in group solo2 (limit 1)", p.stderr)

    def test_plain_pueue_job_is_not_called_stalled(self):
        out = self.d.pueue("add", "--print-task-id", "--", "timeout 6 sh -c 'while :; do :; done' > /dev/null").stdout
        tid = int(out.strip())
        p = self.job("wait", "local", str(tid), "--poll", "0.3", "--stall", "0.05")
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        self.assertIn("stall detection is off", p.stderr)


class HostsFileTests(Base):
    def test_hosts_file(self):
        with open(os.path.join(self.d.dir, "hosts"), "w") as f:
            f.write("# my machines\nbox1 nosuchhost.invalid  # comment\nbox2\nlocal bad\n-x y\n")
        p = self.job("ls", "box2")
        self.assertEqual(p.returncode, 202, p.stdout + p.stderr)  # known, but cannot be reached
        self.assertIn("ignored", p.stderr)  # the 'local' and '-x' lines
        ls = self.job("ls", "box1")
        self.assertEqual(ls.returncode, 202, ls.stdout + ls.stderr)
        self.assertIn("box1: UNREACHABLE", ls.stdout)
        bad = self.job("ls", "box9")
        self.assertEqual(bad.returncode, 205, bad.stdout + bad.stderr)
        w = self.job("wait", "box9", "1")
        self.assertEqual(w.returncode, 205, w.stdout + w.stderr)
        self.assertIn("add it to", w.stdout)
        me = socket.gethostname().split(".")[0].upper()
        p = self.job("run", me, "T-self-name", "--poll", "0.3", "--", "true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.d.dir, "hosts"), "wb") as f:
            f.write(b"\xff\xfe bad\n")
        v = self.job("--version")
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn("cannot read the hosts file", v.stderr)
        os.remove(os.path.join(self.d.dir, "hosts"))


class FreshDaemonTests(Base):
    """Each test here needs a daemon of its own."""

    def test_reused_id_without_a_record_is_gone(self):
        # a job removed from the queue before it ran leaves no exit record
        self.job("group", "local", "held", "1")
        self.d.pueue("pause", "--group", "held")
        sub = self.job("submit", "local", "T-held", "--group", "held", "--", "true")
        tid = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        self.d.pueue("remove", str(tid))
        p = self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3")
        self.assertEqual(p.returncode, 204, p.stdout + p.stderr)
        self.assertIn("no exit record", p.stdout)

    def setUp(self):
        self.d = Daemon()

    def tearDown(self):
        self.d.stop()

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def test_token_refuses_a_reused_id(self):
        sub = self.job("submit", "local", "T-reuse-a", "--", "true")
        tid = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Done")
        self.d.pueue("clean")
        nid = int(self.job("submit", "local", "T-reuse-b", "--", "sleep 2").stdout)
        self.assertEqual(nid, tid, "pueue did not reuse the id; cannot test")
        p = self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3")
        # not the new job's outcome: the original job's own exit record (it ran `true`)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("now belongs to a different job", p.stdout)
        self.assertIn("job's own record", p.stdout)
        k = self.job("kill", "local", str(tid), "--token", token)
        self.assertEqual(k.returncode, 205, k.stdout + k.stderr)

    def test_outcome_survives_pueue_clean(self):
        sub = self.job("submit", "local", "T-clean", "--", "echo x; exit 5")
        tid = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Done")
        self.d.pueue("clean")
        p = self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("job's own record", p.stdout)

    def test_wait_survives_a_daemon_restart(self):
        tid = self.submit("sleep 6", task="T-restart")
        self.wait_status(tid, "Running")
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        time.sleep(1)
        self.d.restart(down_for=3)
        out, err = w.communicate(timeout=60)
        self.assertNotEqual(w.returncode, 205, out + err)
        self.assertNotIn("WRAPPER_ERROR", out)
        self.assertIn("retrying", err)
        self.assertTrue(out.startswith("job: "), out)


if __name__ == "__main__":
    unittest.main()
