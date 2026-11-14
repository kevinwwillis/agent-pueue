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


def own_tmpdir():
    """A fresh folder for one test or class, removed by remove_own_dir."""
    return tempfile.mkdtemp(prefix="jobt.", dir="/tmp")


def remove_own_dir(path):
    """Remove a folder a test made with own_tmpdir, and nothing else. A job killed at the end of a test can still be
    writing its results there for a moment, so a removal that meets a changing tree is tried again."""
    if not (isinstance(path, str) and path.startswith("/tmp/jobt.") and os.path.isdir(path)):
        return
    for _ in range(40):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            continue
        except OSError:
            time.sleep(0.25)
    shutil.rmtree(path)  # still failing after 10 s: let the error show


def _read(path):
    with open(path) as f:
        return f.read().strip()


class Daemon:
    def __init__(self):
        self.dir = own_tmpdir()
        self.stopped = False
        try:
            self._start()
        except BaseException:
            remove_own_dir(self.dir)
            raise

    def _start(self):
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

    def restart(self, down_for=3.0, sig=signal.SIGTERM):
        """Stop pueued (SIGTERM: a clean stop; SIGKILL: it dies without stopping its tasks) and start it again after
        down_for seconds."""
        self.proc.send_signal(sig)
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
        e["JOB_ADMISSION_CONFIG"] = os.path.join(self.dir, "no-admission.json")  # not an admission host
        # each call its own session, so jobs without --group do not queue behind each other in one session group
        e["JOB_SESSION"] = "t-" + os.urandom(4).hex()
        e.update(extra)
        return e

    def pueue(self, *args):
        return subprocess.run(["pueue", "-c", self.cfg] + list(args), capture_output=True, text=True, check=True)

    def status(self, tid):
        st = json.loads(self.pueue("status", "--json").stdout)
        return st["tasks"][str(tid)]["status"]

    def stop(self):
        """Stop the daemon and remove its folder; safe to call more than once."""
        if self.stopped:
            return
        self.stopped = True
        try:
            try:
                self.pueue("kill", "--all")
            except subprocess.CalledProcessError:
                pass
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
            self.log.close()
        finally:
            remove_own_dir(self.dir)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon()
        cls.addClassCleanup(cls.d.stop)  # also when a subclass's setUpClass fails after this

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

    def load_job_here(self, **env):
        """bin/job as a module, set up as a run of job in this test's environment would be (state, hosts, pueue).
        The environment stays pointed at this test's daemon until the test ends (job reads JOB_PUEUE_CONFIG at each
        pueue call), and every pueue call checks that it goes there, never to this machine's own daemon."""
        saved = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(saved)
        self.addCleanup(restore)
        os.environ.update(self.d.env(**env))
        mod = self.load_job()
        base, cfg = mod._pueue_base, self.d.cfg

        def guarded():
            cmd = base()
            if cmd[-2:] != ["-c", cfg]:
                raise AssertionError("a pueue call outside the test's daemon: %r" % cmd)
            return cmd
        mod._pueue_base = guarded
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
        last = p.stdout.rstrip().splitlines()[-1]
        self.assertEqual(last, "job: SUCCESS %s exit 0" % first.split()[2])

    def test_failure_passes_exit_code(self):
        p = self.job("run", "local", "T-fail", "--poll", "0.3", "--", "echo before; exit 7")
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("job: FAILED local#", p.stdout)
        self.assertIn("exit 7", p.stdout.splitlines()[0])
        tid = p.stdout.split("#", 1)[1].split()[0]
        self.assertEqual(p.stdout.rstrip().splitlines()[-1], "job: FAILED local#%s exit 7" % tid)
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
        st = json.loads(self.d.pueue("status", "--json").stdout)
        cmd = st["tasks"][str(tid)]["command"]
        self.assertNotIn("nice", cmd)
        self.assertNotIn("ionice", cmd)

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

    def test_client_sends_protocol_4_only_for_a_priority_submit(self):
        job = self.load_job()
        self.assertEqual(job._request_protocol("submit", {"normal_priority": True}), 4)
        self.assertEqual(job._request_protocol("submit", {"normal_priority": False}), 3)
        self.assertEqual(job._request_protocol("submit", {}), 3)
        for op in ("probe", "ls", "group", "kill"):
            self.assertEqual(job._request_protocol(op, {"normal_priority": True}), 3)

    def _host_call(self, job, op, req):
        import io
        old = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps(req)), io.StringIO()
        try:
            job.host_main(op)
            return json.loads(sys.stdout.getvalue().strip().splitlines()[-1])
        finally:
            sys.stdin, sys.stdout = old

    def test_old_host_refuses_a_priority_submit(self):
        job = self.load_job()
        job.ACCEPTED_PROTOCOLS = (3,)  # a host that predates the option
        req = {"command": "true", "task": "T-oldhost", "cwd": "/tmp", "normal_priority": True}
        reply = self._host_call(job, "submit", dict(req, protocol=job._request_protocol("submit", req)))
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["error"].startswith("protocol mismatch"), reply)
        # the requests an old client sends still pass the protocol check
        reply = self._host_call(job, "ls", {"protocol": job._request_protocol("ls", {})})
        self.assertNotIn("protocol mismatch", reply.get("error", ""))

    def test_priority_submit_through_the_host_json_path(self):
        p = subprocess.run([sys.executable, JOB, "_host", "submit"], capture_output=True, text=True,
                           input=json.dumps({"protocol": 4, "command": "true", "task": "T-p4",
                                             "cwd": "/tmp", "normal_priority": True}),
                           env=self.d.env())
        reply = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(reply["ok"], reply)
        st = json.loads(self.d.pueue("status", "--json").stdout)
        cmd = st["tasks"][str(reply["data"]["id"])]["command"]
        self.assertTrue(cmd.startswith("bash "), cmd)

    def test_request_without_normal_priority_runs_at_default_priority(self):
        job = self.load_job()
        self.assertEqual(job.PROTOCOL, 3)
        self.assertIn(3, job.ACCEPTED_PROTOCOLS)
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
        ids = [self.submit(c, task="T-multi") for c in ("sleep 1", "sleep 2", "true")]
        p = self.job("wait", "local", *[str(i) for i in ids], "--poll", "0.3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: ALL ENDED local: 3 SUCCESS"), p.stdout)
        self.assertEqual(p.stdout.count("job: SUCCESS local#"), 4)  # three reports and the last line

    def test_several_jobs_end_early_on_a_stall(self):
        self.job("group", "local", "stallpair", "2")
        ids = [self.submit(c, task="T-multi-stall", extra=("--group", "stallpair")) for c in ("sleep 300", "sleep 20")]
        p = self.job("wait", "local", *[str(i) for i in ids], "--poll", "0.3", "--stall", "0.05")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        self.assertIn("0 of 2 jobs ended", p.stdout.splitlines()[0])
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
        self.addCleanup(self.d.stop)  # also when a test fails in setUp

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

    def test_a_job_running_when_the_daemon_restarts_ends_killed_by_restart(self):
        tid = int(self.job("submit", "local", "T-rs", "--", "sleep 60", JOB_SESSION="s-rs").stdout)
        killed = self.submit("sleep 60", task="T-rs-k")
        self.wait_status(tid, "Running")
        self.wait_status(killed, "Running")
        self.assertEqual(self.job("kill", "local", str(killed)).returncode, 0)
        self.d.restart(down_for=1)
        p = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(p.returncode, 208, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: KILLED_BY_RESTART local#%d" % tid), p.stdout)
        self.assertIn("pueue daemon on local stopped", p.stdout)
        self.assertIn("job resume local --group", p.stdout)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[-1], "job: KILLED_BY_RESTART local#%d exit 208" % tid)
        mine = self.job("wait", "--mine", JOB_SESSION="s-rs")
        self.assertEqual(mine.returncode, 0, mine.stdout)  # the wait above reported it
        self.assertIn("no unfinished or unreported job", mine.stdout)
        k = self.job("wait", "local", str(killed), "--poll", "0.3")
        self.assertEqual(k.returncode, 137, k.stdout + k.stderr)
        ls = self.job("ls", "local")
        self.assertRegex(ls.stdout, r"\n\S+\s+%d\s+T-rs\s+KILLED_BY_RESTART" % tid)

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


class SessionGroupTests(Base):
    """A job without --group on a host without admission runs in its session's own group (TASK-007.05)."""

    def groups(self):
        return json.loads(self.d.pueue("status", "--json").stdout)["groups"]

    def task_group(self, tid):
        return json.loads(self.d.pueue("status", "--json").stdout)["tasks"][str(tid)]["group"]

    def test_sessions_do_not_queue_behind_each_other(self):
        p = self.job("submit", "local", "T-sg-a", "--", "sleep 20", JOB_SESSION="sess-a")
        a = int(p.stdout)
        self.wait_status(a, "Running")
        self.assertEqual(self.task_group(a), "session-sess-a")
        r = self.job("run", "local", "T-sg-b", "--poll", "0.3", "--", "echo ran", JOB_SESSION="sess-b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(r.elapsed, 15)
        self.assertNotIn("queued behind", r.stderr)
        self.assertEqual(self.groups()["session-sess-b"]["parallel_tasks"], 8)  # the default group's limit
        self.d.pueue("kill", str(a))

    def test_a_sessions_own_jobs_run_as_many_at_a_time_as_the_default_group(self):
        self.d.pueue("parallel", "1")
        try:
            a = int(self.job("submit", "local", "T-sg-c", "--", "sleep 4", JOB_SESSION="sess-c").stdout)
        finally:
            self.d.pueue("parallel", "8")
        self.assertEqual(self.groups()["session-sess-c"]["parallel_tasks"], 1)
        self.wait_status(a, "Running")
        r = self.job("run", "local", "T-sg-d", "--poll", "0.3", "--", "true", JOB_SESSION="sess-c")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("queued behind 1 running job(s) in group session-sess-c (limit 1)", r.stderr)
        ls = self.job("ls", "local")
        self.assertIn("session-sess-c", ls.stdout)

    def test_an_idle_group_goes_with_its_finished_jobs_once_they_are_old(self):
        tid = int(self.job("submit", "local", "T-sg-o", "--", "true", JOB_SESSION="sess-gone").stdout)
        self.wait_status(tid, "Done")
        mark = os.path.join(self.d.dir, "state", "agent-pueue", "session-groups", "session-sess-gone")
        os.utime(mark, (time.time() - 7200,) * 2)  # no submit used it for two hours
        job = self.load_job_here()
        job.SESSION_GROUP_KEEP_S = 1e9  # its job ended too recently: kept, with the job
        job._session_group(job._pueue_status(), "sess-third")
        self.assertIn("session-sess-gone", self.groups())
        self.assertIn(str(tid), json.loads(self.d.pueue("status", "--json").stdout)["tasks"])
        job.SESSION_GROUP_KEEP_S = 0  # as if its job ended over a day ago
        job._session_group(job._pueue_status(), "sess-third")
        self.assertNotIn("session-sess-gone", self.groups())
        self.assertNotIn(str(tid), json.loads(self.d.pueue("status", "--json").stdout)["tasks"])
        self.assertFalse(os.path.exists(mark))

    def test_a_named_group_and_default_still_work(self):
        tid = self.submit("true", task="T-sg-def", extra=("--group", "default"))
        self.assertEqual(self.task_group(tid), "default")

    def test_an_idle_empty_session_group_is_removed_later(self):
        tid = int(self.job("submit", "local", "T-sg-old", "--", "true", JOB_SESSION="sess-old").stdout)
        self.wait_status(tid, "Done")
        self.assertIn("session-sess-old", self.groups())
        self.d.pueue("remove", str(tid))
        mark = os.path.join(self.d.dir, "state", "agent-pueue", "session-groups", "session-sess-old")
        self.assertTrue(os.path.exists(mark))
        self.submit("true", task="T-sg-new")  # used within the hour: kept
        self.assertIn("session-sess-old", self.groups())
        old = time.time() - 7200
        os.utime(mark, (old, old))
        self.submit("true", task="T-sg-new2")
        self.assertNotIn("session-sess-old", self.groups())
        self.assertFalse(os.path.exists(mark))

    def test_group_name_is_safe_and_distinct(self):
        job = self.load_job()
        a = job._session_group_name("local_1c37ef45-f90f-4cad-8955-445f2a6fb349")
        b = job._session_group_name("local_1c37ef45-f90f-4cad-8955-000000000000")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("session-local_1c37ef45"), a)
        self.assertEqual(job._session_group_name("sess-a"), "session-sess-a")
        self.assertTrue(all(c.isalnum() or c in "_-" for c in job._session_group_name("a b/c")))

    def test_pueue_error_text_is_one_line(self):
        job = self.load_job()
        raw = ("Error: \n   0: \x1b[91mFailed to connect to the daemon\x1b[0m\n\nLocation:\n"
               "   \x1b[35mpueue/src/bin/pueue.rs\x1b[0m:\x1b[35m91\x1b[0m\n\n"
               "Backtrace omitted. Run with RUST_BACKTRACE=1 environment variable to display it.\n"
               "Run with RUST_BACKTRACE=full to include source snippets.\n")
        self.assertEqual(job._pueue_error_text(raw), "Failed to connect to the daemon")


class HelpAndLastLineTests(Base):
    """The last line carries the verdict; help carries the rules agents missed (TASK-007.06)."""

    def test_wrapper_error_last_line_names_the_verdict(self):
        p = self.job("submit", "local", "T-ll", "--group", "nope", "--", "true")
        self.assertEqual(p.returncode, 205)
        self.assertEqual(p.stdout.rstrip().splitlines()[-1], "job: WRAPPER_ERROR exit 205")

    def test_several_jobs_last_line(self):
        self.job("group", "local", "ll2", "2")
        ids = [self.submit("true", task="T-ll2", extra=("--group", "ll2")) for _ in range(2)]
        p = self.job("wait", "local", *[str(i) for i in ids], "--poll", "0.3")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(p.stdout.rstrip().splitlines()[-1], "job: SUCCESS local#%d,%d exit 0" % tuple(ids))

    def test_help_has_the_rules(self):
        h = self.job("--help").stdout
        for text in ("never pipe", "--tail N", "session's own group", "--pilot", "--like", "also removes a queued",
                     "job wait HOST ID --token T"):
            self.assertIn(text, h)
        r = self.job("run", "--help").stdout
        self.assertIn("never pipe", r)
        self.assertIn("--tail", r)


class LogShowResumeTests(Base):
    """job log, job show and job resume (TASK-007.07)."""

    def test_log_lines_grep_full(self):
        tid = self.submit("for i in $(seq 1 300); do echo line $i; done; printf 'bar 1%%\\rbar 100%%\\n'",
                          task="T-log")
        self.wait_status(tid, "Done")
        p = self.job("log", "local", str(tid), "--lines", "2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines(), ["line 300", "bar 100%"])
        g = self.job("log", "local", str(tid), "--grep", "^line 2[0-9]$", "--lines", "3")
        self.assertEqual(g.stdout.splitlines(), ["line 27", "line 28", "line 29"])
        self.assertIn("3 of 10 lines matching", g.stderr)
        f = self.job("log", "local", str(tid), "--full")
        self.assertEqual(f.stdout.count("line "), 300)
        self.assertTrue(f.stdout.startswith("line 1\n"))
        bad = self.job("log", "local", str(tid), "--grep", "(")
        self.assertEqual(bad.returncode, 205)
        other = self.job("log", "local", str(tid), "--token", "0" * 32)
        self.assertEqual(other.returncode, 205)
        self.assertIn("different job", other.stdout)

    def test_show(self):
        sub = self.job("submit", "local", "T-show", "--", "echo 'two words'\nexit 3")
        tid = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Done")
        p = self.job("show", "local", str(tid))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for text in ("task:              T-show", "Done: FAILED exit 3", "token:             " + token,
                     "log:               local:", "  echo 'two words'", "  exit 3", "group:             session-"):
            self.assertIn(text, p.stdout)

    def test_resume_a_paused_group(self):
        self.job("group", "local", "pz", "1")
        self.d.pueue("pause", "--group", "pz")
        tid = self.submit("true", task="T-pz", extra=("--group", "pz"))
        r = self.job("resume", "local", "--group", "pz")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("was Paused, now Running; 1 job(s) waiting", r.stdout)
        self.wait_status(tid, "Done")
        self.assertEqual(self.job("resume", "local", "--group", "nope").returncode, 205)

    def test_an_older_host_says_it_predates_the_op(self):
        job = self.load_job()

        def old(host, op, req, **kw):
            raise job.JobError("gpu1: no reply from job on the host (exit 2): usage: job ... invalid choice: 'log'")
        job.call = old
        with self.assertRaises(job.JobError) as e:
            job._newer_op("gpu1", "log", {})
        self.assertIn("predates job log", str(e.exception))


class MultiWaitTests(Base):
    """A wait on several jobs (TASK-006)."""

    def sub(self, cmd, task="T-mw"):
        p = self.job("submit", "local", task, "--group", "mw", "--", cmd)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return int(p.stdout), p.stderr.split("--token ", 1)[1].split()[0]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.d.pueue("group", "add", "mw")
        cls.d.pueue("parallel", "8", "--group", "mw")

    def test_a_failure_ends_the_wait_at_once(self):
        long1, t1 = self.sub("sleep 60")
        bad, tb = self.sub("sleep 1; exit 3")
        long2, t2 = self.sub("sleep 60")
        p = self.job("wait", "local", str(long1), "%d:%s" % (bad, tb), str(long2), "--poll", "0.3")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertLess(p.elapsed, 30)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "job: FAILED local#%d; 1 of 3 jobs ended; not ended: local#%d local#%d" % (
            bad, long1, long2))
        self.assertIn("keep waiting on the others with: job wait local %d:%s %d:%s" % (long1, t1, long2, t2),
                      p.stdout)
        self.assertEqual(lines[-1], "job: FAILED local#%d exit 3 (1 of 3 jobs ended)" % bad)
        for i in (long1, long2):
            self.d.pueue("kill", str(i))

    def test_a_kill_ends_the_wait_at_once(self):
        a, _ = self.sub("sleep 60")
        b, _ = self.sub("sleep 60")
        self.wait_status(b, "Running")
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(a), str(b), "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        time.sleep(1)
        self.d.pueue("kill", str(b))
        out, err = w.communicate(timeout=30)
        self.assertEqual(w.returncode, 137, out + err)
        self.assertTrue(out.startswith("job: KILLED local#%d; 1 of 2 jobs ended" % b), out)
        self.d.pueue("kill", str(a))

    def test_a_stall_is_not_counted_as_ended(self):
        a, _ = self.sub("sleep 300")
        b, _ = self.sub("true")
        self.wait_status(b, "Done")
        p = self.job("wait", "local", str(a), str(b), "--poll", "0.3", "--stall", "0.05")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        self.assertIn("1 of 2 jobs ended; not ended: local#%d" % a, p.stdout.splitlines()[0])
        self.d.pueue("kill", str(a))

    def test_id_token_pairs(self):
        a, ta = self.sub("true")
        b, tb = self.sub("true")
        self.wait_status(b, "Done")
        p = self.job("wait", "local", "%d:%s" % (a, ta), "%d:%s" % (b, tb), "--poll", "0.3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wrong = self.job("wait", "local", "%d:%s" % (a, tb), "--poll", "0.3")
        self.assertIn("now belongs to a different job", wrong.stdout)
        both = self.job("wait", "local", str(a), str(b), "--token", ta)
        self.assertEqual(both.returncode, 205)
        self.assertIn("ID:TOKEN", both.stdout)
        bad = self.job("wait", "local", "%d:xyz" % a)
        self.assertEqual(bad.returncode, 205)

    def test_a_pilot_time_limit_does_not_end_the_wait_early(self):
        job = self.load_job()
        self.assertIn("PILOT_TIME_LIMIT", job.GOOD)


class KillTests(Base):
    """job kill ends the whole process tree, says what it did, removes queued jobs as KILLED, and kills by task
    (TASK-001)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.d.pueue("group", "add", "kt")
        cls.d.pueue("parallel", "8", "--group", "kt")

    def pid_alive(self, pid):
        try:
            with open("/proc/%d/stat" % pid) as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    def test_kill_ends_children_in_their_own_process_group(self):
        pidfile = os.path.join(self.d.dir, "child.pid")
        # timeout puts itself in its own process group, so pueue's kill of the job's group misses it and its child
        tid = self.submit("timeout 300 sh -c 'echo $$ > %s; exec sleep 300'" % pidfile, task="T-ktree",
                          extra=("--group", "kt"))
        for _ in range(100):
            if os.path.exists(pidfile) and _read(pidfile):
                break
            time.sleep(0.05)
        child = int(_read(pidfile))
        k = self.job("kill", "local", str(tid))
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        self.assertRegex(k.stdout, r"local#%d killed: [1-9][0-9]* process\(es\) of the job were running; none is left"
                         % tid)
        self.assertFalse(self.pid_alive(child), "the job's child in another process group is still running")
        p = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(p.returncode, 137, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: KILLED local#%d" % tid), p.stdout)

    def test_removing_a_queued_job_ends_its_wait_killed(self):
        self.job("group", "local", "kq1", "1")
        a = self.submit("sleep 30", task="T-kq1a", extra=("--group", "kq1"))
        self.wait_status(a, "Running")
        sub = self.job("submit", "local", "T-kq1b", "--group", "kq1", "--", "true")
        b = int(sub.stdout)
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", "%d:%s" % (b, token), "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        time.sleep(1)
        k = self.job("kill", "local", str(b))
        self.assertIn("removed from the queue", k.stdout)
        out, err = w.communicate(timeout=30)
        self.assertEqual(w.returncode, 137, out + err)
        self.assertTrue(out.startswith("job: KILLED local#%d" % b), out)
        self.assertIn("removed from the queue by job kill before it ran", out)
        self.job("kill", "local", str(a))

    def test_kill_by_task_refuses_other_sessions_jobs(self):
        mine = self.job("submit", "local", "T-ktask", "--group", "kt", "--", "sleep 30", JOB_SESSION="s-me")
        theirs = self.job("submit", "local", "T-ktask", "--group", "kt", "--", "sleep 30", JOB_SESSION="s-other")
        a, b = int(mine.stdout), int(theirs.stdout)
        self.wait_status(a, "Running")
        self.wait_status(b, "Running")
        k = self.job("kill", "local", "--task", "T-ktask", JOB_SESSION="s-me")
        self.assertEqual(k.returncode, 205, k.stdout + k.stderr)
        self.assertIn("other sessions", k.stdout)
        self.assertIn("Running", self.d.status(a))
        k = self.job("kill", "local", "--task", "T-ktask", "--all-sessions", JOB_SESSION="s-me")
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        self.assertIn("local#%d killed" % a, k.stdout)
        self.assertIn("local#%d killed" % b, k.stdout)
        none = self.job("kill", "local", "--task", "T-ktask", JOB_SESSION="s-me")
        self.assertIn("no unfinished job of task T-ktask", none.stdout)

    def test_kill_by_task_of_my_own_jobs(self):
        ids = [int(self.job("submit", "local", "T-kown", "--group", "kt", "--", "sleep 30", JOB_SESSION="s-own").stdout)
               for _ in range(2)]
        k = self.job("kill", "local", "--task", "T-kown", JOB_SESSION="s-own")
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        for i in ids:
            self.assertIn("local#%d" % i, k.stdout)

    def test_kill_help_says_queued_and_held(self):
        h = " ".join(self.job("kill", "--help").stdout.split())
        self.assertIn("remove a queued or held one", h)
        self.assertIn("remove a queued or held one", " ".join(self.job("--help").stdout.split()))


class RestartRecoveryTests(Base):
    """job wait --mine, tokens in job ls, kept --check scripts, paused groups (TASK-007.08)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.d.pueue("group", "add", "rr")
        cls.d.pueue("parallel", "8", "--group", "rr")

    def test_wait_mine_waits_on_this_sessions_unfinished_jobs(self):
        a = int(self.job("submit", "local", "T-mine", "--group", "rr", "--", "sleep 2", JOB_SESSION="s-mine").stdout)
        b = int(self.job("submit", "local", "T-mine", "--group", "rr", "--", "sleep 3", JOB_SESSION="s-mine").stdout)
        other = int(self.job("submit", "local", "T-mine", "--group", "rr", "--", "sleep 60",
                             JOB_SESSION="s-notmine").stdout)
        p = self.job("wait", "--mine", "--poll", "0.3", JOB_SESSION="s-mine")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: ALL ENDED local: 2 SUCCESS"), p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "job: SUCCESS local#%d,%d exit 0" % (a, b))
        self.assertNotIn("local#%d" % other, p.stdout)
        none = self.job("wait", "--mine", JOB_SESSION="s-mine")
        self.assertEqual(none.returncode, 0, none.stdout)
        self.assertIn("no unfinished or unreported job of session s-mine", none.stdout)
        self.d.pueue("kill", str(other))

    def test_wait_mine_needs_a_session(self):
        env = self.d.env()
        for k in ("JOB_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(k, None)
        p = subprocess.run([sys.executable, JOB, "wait", "--mine"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("no session id", p.stdout)

    def test_ls_shows_the_token(self):
        sub = self.job("submit", "local", "T-lstok", "--", "true")
        token = sub.stderr.split("--token ", 1)[1].split()[0]
        ls = self.job("ls", "local")
        self.assertIn("TOKEN", ls.stdout.splitlines()[0])
        self.assertIn(token, ls.stdout)

    def test_a_kept_check_runs_on_reattach_after_the_original_is_gone(self):
        chk = os.path.join(self.d.dir, "gone-check.sh")
        with open(chk, "w") as f:
            f.write("#!/bin/sh\nif grep -q ERROR; then echo 'kept check saw ERROR'; exit 1; fi\n")
        os.chmod(chk, 0o755)
        run = subprocess.Popen([sys.executable, JOB, "run", "local", "T-kept", "--group", "rr", "--poll", "0.3",
                                "--check", chk, "--", "sleep 3; echo ERROR late; sleep 60"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env())
        line = run.stderr.readline()
        tid, token = int(line.split("#", 1)[1].split()[0]), line.split("--token ", 1)[1].split()[0]
        run.kill()  # the wait is lost (an app restart); the job runs on
        run.communicate()
        os.remove(chk)  # its temporary folder is gone too
        p = self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("kept check saw ERROR", p.stdout)
        self.assertIn("using the --check script kept with the job", p.stderr)
        self.d.pueue("kill", str(tid))

    def test_a_paused_group_alerts_with_job_resume(self):
        self.job("group", "local", "rrp", "1")
        self.d.pueue("pause", "--group", "rrp")
        tid = self.submit("true", task="T-rrp", extra=("--group", "rrp"))
        p = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("its group rrp on local is paused", p.stdout)
        self.assertIn("job resume local --group rrp", p.stdout)
        self.assertNotIn("pueue start", p.stdout)
        self.job("resume", "local", "--group", "rrp")
        self.assertEqual(self.job("wait", "local", str(tid), "--poll", "0.3").returncode, 0)


class DaemonDeathTests(Base):
    """What a wait says about a job after the pueue daemon stopped or died (review of TASK-007.08). Each test has a
    daemon of its own."""

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        self.d = Daemon()
        self.addCleanup(self.d.stop)

    def test_processes_that_outlive_a_dead_daemon_are_untracked_not_killed_by_restart(self):
        sub = self.job("submit", "local", "T-dead", "--", "sleep 300")
        tid, token = int(sub.stdout), sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Running")
        time.sleep(0.5)
        self.d.restart(down_for=0.5, sig=signal.SIGKILL)  # it dies without stopping its tasks
        job = self.load_job_here()
        alive = sorted(job._job_procs(token))
        self.addCleanup(lambda: [os.kill(p, 9) for p in job._job_procs(token)])
        self.assertTrue(alive, "the job's processes did not outlive the daemon; cannot test")
        p = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(p.returncode, 209, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: UNTRACKED local#%d" % tid), p.stdout)
        self.assertIn("pids %s" % " ".join(map(str, alive)), p.stdout)
        self.assertIn("job kill local %d --token %s" % (tid, token), p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "job: UNTRACKED local#%d exit 209" % tid)
        ls = self.job("ls", "local")
        self.assertRegex(ls.stdout, r"\n\S+\s+%d\s+T-dead\s+UNTRACKED" % tid)
        k = self.job("kill", "local", str(tid), "--token", token)
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        self.assertIn("pueue had lost track of it", k.stdout)
        self.assertEqual(job._job_procs(token), {})
        again = self.job("wait", "local", str(tid), "--poll", "0.3")
        self.assertEqual(again.returncode, 208, again.stdout + again.stderr)

    def test_an_ordinary_kill_before_a_clean_restart_is_killed_not_killed_by_restart(self):
        a = self.submit("sleep 300", task="T-oom")
        self.wait_status(a, "Running")
        self.d.pueue("kill", str(a))  # as systemd-oomd, a scope kill or a kill by hand: not job kill
        self.wait_status(a, "Done")
        time.sleep(6)  # over STOP_SPREAD_S: the daemon kept working after the kill
        b = self.submit("true", task="T-later")
        self.wait_status(b, "Done")
        running = self.submit("sleep 300", task="T-at-stop")
        self.wait_status(running, "Running")
        self.d.restart(down_for=1)
        p = self.job("wait", "local", str(a), "--poll", "0.3")
        self.assertEqual(p.returncode, 137, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: KILLED local#%d" % a), p.stdout)
        r = self.job("wait", "local", str(running), "--poll", "0.3")
        self.assertEqual(r.returncode, 208, r.stdout + r.stderr)

    def test_the_restart_rule_reads_the_stop_from_the_task_times(self):
        job = self.load_job()
        job._daemon_start = lambda proc="/proc": 1_000_000.0
        iso = lambda t: __import__("datetime").datetime.fromtimestamp(t).astimezone().isoformat()

        def task(start, end, result="Killed"):
            return {"result": result, "start": iso(start), "end": iso(end), "token": None}

        def state(*events):  # pueue status with one Done task per (start, end)
            return {"tasks": {str(i): {"id": i, "status": {"Done": {"start": iso(a), "end": iso(b), "result": "Success"}}}
                              for i, (a, b) in enumerate(events)}}
        self.assertEqual(job._killed_by(task(999_000, 999_990), state((999_000, 999_990))), "daemon")  # the last event
        self.assertEqual(job._killed_by(task(999_000, 999_990), state((999_100, 999_993))), "daemon")  # 3 s apart
        self.assertIsNone(job._killed_by(task(999_000, 999_900), state((999_100, 999_990))))  # work went on after it
        self.assertEqual(job._killed_by(task(999_000, 1_000_030), state()), "daemon")  # died; marked at the start
        self.assertIsNone(job._killed_by(task(999_000, 1_000_300), state()))  # long after the start: not the restore
        self.assertIsNone(job._killed_by(task(1_000_100, 1_000_200), state()))  # started under this daemon
        self.assertIsNone(job._killed_by(task(999_000, 999_990, "Failed"), state()))


class ReviewTwoTests(Base):
    """Fixes from the second review of the client changes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.d.pueue("group", "add", "r2")
        cls.d.pueue("parallel", "8", "--group", "r2")

    def test_wait_mine_reports_a_job_that_ended_while_no_one_waited(self):
        tid = int(self.job("submit", "local", "T-unseen", "--group", "r2", "--", "sleep 1; echo oops; exit 3",
                           JOB_SESSION="s-unseen").stdout)
        self.wait_status(tid, "Done")
        p = self.job("wait", "--mine", "--poll", "0.3", JOB_SESSION="s-unseen")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("job: FAILED local#%d" % tid), p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "job: FAILED local#%d exit 3" % tid)
        again = self.job("wait", "--mine", "--poll", "0.3", JOB_SESSION="s-unseen")
        self.assertEqual(again.returncode, 0, again.stdout)  # reported once
        self.assertIn("job: NONE exit 0", again.stdout)

    def test_wait_mine_reports_the_worst_and_still_waits_on_the_rest(self):
        bad = int(self.job("submit", "local", "T-w1", "--group", "r2", "--", "exit 4", JOB_SESSION="s-worst").stdout)
        long = int(self.job("submit", "local", "T-w2", "--group", "r2", "--", "sleep 60", JOB_SESSION="s-worst").stdout)
        self.wait_status(bad, "Done")
        p = self.job("wait", "--mine", "--poll", "0.3", JOB_SESSION="s-worst")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("keep waiting on the others with: job wait local %d:" % long, p.stdout)
        self.d.pueue("kill", str(long))

    def test_wait_mine_names_a_host_it_cannot_list_at_once(self):
        import contextlib
        import io
        job = self.load_job_here(JOB_SESSION="s-far")
        job.HOSTS["far"] = "far.invalid"
        job.LISTED.append("far")
        real = job.call

        def call(host, op, req, timeout=120):
            if host == "far":
                raise job.Unreachable("no route to far")
            return real(host, op, req, timeout=timeout)
        job.call = call
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = job.cmd_wait_mine(job.parse_args(["wait", "--mine"])[1])
        self.assertEqual(code, 202, out.getvalue())
        self.assertIn("far could not be listed (no route to far)", err.getvalue())

    def test_reattach_line_names_every_token_when_a_job_had_already_failed(self):
        def sub(cmd):
            p = self.job("submit", "local", "T-rt", "--group", "r2", "--", cmd)
            return int(p.stdout), p.stderr.split("--token ", 1)[1].split()[0]
        long1, t1 = sub("sleep 60")
        bad, _ = sub("exit 3")
        long2, t2 = sub("sleep 60")
        self.wait_status(bad, "Done")  # already failed when the wait starts: the first round ends it
        p = self.job("wait", "local", str(long1), str(bad), str(long2), "--poll", "0.3")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("keep waiting on the others with: job wait local %d:%s %d:%s" % (long1, t1, long2, t2),
                      p.stdout)
        for i in (long1, long2):
            self.d.pueue("kill", str(i))

    def test_kill_by_task_removes_waiting_jobs_before_killing_running_ones(self):
        self.job("group", "local", "r2one", "1")
        run = int(self.job("submit", "local", "T-order", "--group", "r2one", "--", "sleep 60",
                           JOB_SESSION="s-ord").stdout)
        self.wait_status(run, "Running")
        queued = int(self.job("submit", "local", "T-order", "--group", "r2one", "--", "sleep 60",
                              JOB_SESSION="s-ord").stdout)
        k = self.job("kill", "local", "--task", "T-order", JOB_SESSION="s-ord")
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        self.assertIn("local#%d removed from the queue" % queued, k.stdout)  # it never started
        self.assertIn("local#%d killed" % run, k.stdout)
        self.assertNotIn(str(queued), json.loads(self.d.pueue("status", "--json").stdout)["tasks"])

    def test_kill_with_processes_left_ends_with_a_verdict_line(self):
        import contextlib
        import io
        job = self.load_job_here()
        job.call = lambda host, op, req, timeout=120: {"id": 7, "action": "killed", "processes": 3, "left": [4242]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = job.cmd_kill(job.parse_args(["kill", "local", "7"])[1])
        self.assertEqual(code, 205)
        self.assertEqual(out.getvalue().splitlines()[-1],
                         "job: WRAPPER_ERROR local#7 exit 205 (1 process(es) of the job still alive after SIGKILL)")

    def test_old_argument_orders_still_parse(self):
        sub = self.job("submit", "local", "T-argo", "--group", "r2", "--", "true")
        tid, token = int(sub.stdout), sub.stderr.split("--token ", 1)[1].split()[0]
        self.wait_status(tid, "Done")
        for args in (("wait", "local", "--token", token, str(tid)), ("wait", "local", "--stall", "10", str(tid)),
                     ("wait", "local", str(tid), "--token", token)):
            p = self.job(*args, "--poll", "0.3")
            self.assertEqual(p.returncode, 0, " ".join(args) + ": " + p.stdout + p.stderr)
        k = self.job("kill", "local", "--token", token, str(tid))
        self.assertIn("nothing to kill", k.stdout)  # parsed: the host answered about that job
        self.assertNotIn("unrecognized arguments", k.stdout + k.stderr)

    def test_lines_zero_is_refused(self):
        tid = self.submit("echo a; echo b", task="T-l0", extra=("--group", "r2"))
        self.wait_status(tid, "Done")
        p = self.job("log", "local", str(tid), "--lines", "0")
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("--lines must be at least 1", p.stdout)
        job = self.load_job_here()
        with self.assertRaises(job.JobError):
            job.host_log({"id": tid, "lines": 0})

    def test_a_token_that_is_not_a_job_token_never_names_a_file(self):
        job = self.load_job_here()
        with self.assertRaises(job.JobError):
            job.host_probe({"id": 99999, "token": "../../x"})
        self.assertIsNone(job._kept_check("../../bin/sh"))
        job._keep_check("../escape", "/bin/true")
        self.assertFalse(os.path.exists(os.path.join(job.STATE_DIR, "escape")))
        self.assertIsNone(job._summarize_task({"id": 1, "status": {"Done": {}}, "envs": {"JOB_TOKEN": "../x"}})["token"])


class ParserTests(unittest.TestCase):
    """Forms that worked before wait and kill gained optional positionals, on every Python from 3.8 (run this class
    with an older interpreter too: python3.8 -m unittest tests.test_job.ParserTests)."""

    def parse(self, *argv):
        env = dict(os.environ, JOB_HOSTS_FILE="/nonexistent/hosts", JOB_PUEUE_CONFIG="/nonexistent/pueue.yml")
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            return Base.load_job().parse_args(list(argv))[1]
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_wait_forms(self):
        a = self.parse("wait", "local", "--token", "T", "5")
        self.assertEqual((a.host, a.ids, a.token), ("local", ["5"], "T"))
        a = self.parse("wait", "local", "--stall", "10", "5")
        self.assertEqual((a.host, a.ids, a.stall), ("local", ["5"], 10.0))
        a = self.parse("wait", "local", "5", "--token", "T")
        self.assertEqual((a.host, a.ids, a.token), ("local", ["5"], "T"))
        a = self.parse("wait", "local", "1", "2:abc", "--tail", "3")
        self.assertEqual((a.host, a.ids, a.tail), ("local", ["1", "2:abc"], 3))
        a = self.parse("wait", "--mine", "--poll", "1")
        self.assertEqual((a.host, a.ids, a.mine, a.cmd), (None, [], True, "wait"))

    def test_kill_forms(self):
        a = self.parse("kill", "local", "--token", "T", "5")
        self.assertEqual((a.host, a.id, a.token, a.cmd), ("local", 5, "T", "kill"))
        a = self.parse("kill", "local", "--task", "X")
        self.assertEqual((a.host, a.id, a.task), ("local", None, "X"))

    def test_other_commands_still_parse(self):
        a = self.parse("log", "local", "3", "--lines", "5")
        self.assertEqual((a.cmd, a.id, a.lines), ("log", 3, 5))


if __name__ == "__main__":
    unittest.main()
