"""Tests of `job` against a remote host over ssh.

Skipped unless JOB_TEST_HOST names a host from the hosts file, e.g.:

    JOB_TEST_HOST=gpu1 python3 -m unittest tests.test_remote -v

The host must run Linux and have this version of job in ~/.local/bin
(./install.sh) and pueue in ~/.local/bin or on its PATH.
A throwaway pueued runs on the host in `mktemp -d $JOB_TEST_REMOTE_TMP/agent-XXXXXX`
(default /tmp) with no callback; the folder is removed afterwards.
With JOB_TEST_REAL_DAEMON=1, one more test runs a success and a failure on the
host's real pueued (its callback, if any, fires for both).
"""

import json
import os
import subprocess
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = os.path.join(REPO, "bin", "job")
HOST = os.environ.get("JOB_TEST_HOST")
TMP = os.environ.get("JOB_TEST_REMOTE_TMP", "/tmp")
DEST = None


def _dest():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("jobmod", JOB)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("jobmod", loader))
    loader.exec_module(mod)
    return mod.HOSTS.get(HOST) or HOST


def ssh(script, check=True):
    """Run a bash script on HOST; the script goes over stdin, not argv."""
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", DEST, "bash -s"], input=script,
                       capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise RuntimeError("ssh %s failed (%d): %s" % (HOST, p.returncode, p.stderr))
    return p.stdout


@unittest.skipUnless(HOST, "set JOB_TEST_HOST to run remote tests")
class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global DEST
        DEST = _dest()
        cls.dir = ssh("""
set -e
D=$(mktemp -d %s/agent-XXXXXX)
mkdir -p $D/run
cat > $D/pueue.yml <<EOF
shared:
  pueue_directory: $D/data
  runtime_directory: $D/run
  use_unix_socket: true
  unix_socket_path: $D/run/pueue.socket
daemon:
  callback: null
EOF
export PATH="$HOME/.local/bin:$PATH"
nohup pueued -c $D/pueue.yml > $D/daemon.log 2>&1 < /dev/null &
for i in $(seq 100); do [ -S $D/run/pueue.socket ] && break; sleep 0.1; done
pueue -c $D/pueue.yml parallel 8 > /dev/null
echo $D
""" % TMP).strip().splitlines()[-1]
        assert cls.dir.startswith(TMP + "/agent-"), cls.dir
        cls.cfg = cls.dir + "/pueue.yml"

    @classmethod
    def tearDownClass(cls):
        ssh("""
D=%s
case "$D" in %s/agent-??????) ;; *) echo "refusing to remove $D" >&2; exit 1;; esac
PATH="$HOME/.local/bin:$PATH" pueue -c $D/pueue.yml kill --all >/dev/null 2>&1
kill $(cat $D/run/pueue.pid) 2>/dev/null
sleep 1
rm -rf -- "${D:?}"
""" % (cls.dir, TMP))

    def job(self, *args, real=False, timeout=180, **env):
        e = dict(os.environ)
        e.pop("JOB_REMOTE_PUEUE_CONFIG", None)
        if not real:
            e["JOB_REMOTE_PUEUE_CONFIG"] = self.cfg
        e.update(env)
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, JOB] + list(args), capture_output=True, text=True,
                           env=e, timeout=timeout)
        p.elapsed = time.monotonic() - t0
        return p

    def rstatus(self, tid):
        out = ssh("PATH=$HOME/.local/bin:$PATH pueue -c %s status --json" % self.cfg)
        return json.loads(out)["tasks"][str(tid)]["status"]

    def rpueue(self, *args):
        return ssh("PATH=$HOME/.local/bin:$PATH pueue -c %s %s" % (self.cfg, " ".join(args)))

    def test_success_failure_and_nice(self):
        p = self.job("run", HOST, "T-r-ok", "--poll", "1", "--",
                     "echo nice=$(nice); ionice -p $$; echo path=$PATH; hostname")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("job: SUCCESS %s#" % HOST, p.stdout)
        self.assertIn("nice=19", p.stdout)
        self.assertIn("idle", p.stdout)
        self.assertIn("/.local/bin:", p.stdout)
        p = self.job("run", HOST, "T-r-fail", "--poll", "1", "--", "echo x; exit 9")
        self.assertEqual(p.returncode, 9, p.stdout + p.stderr)

    def test_long_command_goes_by_stdin(self):
        body = "".join("echo line%04d\n" % i for i in range(3000))  # ~40 KB
        f = os.path.join(os.environ.get("TMPDIR", "/tmp"), "job-remote-long-%d.sh" % os.getpid())
        with open(f, "w") as fh:
            fh.write(body + "exit 0\n")
        try:
            p = self.job("run", HOST, "T-r-long", "--poll", "1", "--tail", "2", "--script", f)
        finally:
            os.remove(f)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("line2999", p.stdout)

    def test_killed(self):
        p = self.job("submit", HOST, "T-r-kill", "--", "sleep 300")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tid = int(p.stdout.strip())
        w = subprocess.Popen([sys.executable, JOB, "wait", HOST, str(tid), "--poll", "1"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=dict(os.environ, JOB_REMOTE_PUEUE_CONFIG=self.cfg))
        time.sleep(4)
        k = self.job("kill", HOST, str(tid))
        self.assertEqual(k.returncode, 0, k.stdout + k.stderr)
        out, err = w.communicate(timeout=60)
        self.assertEqual(w.returncode, 137, out + err)

    def test_stall(self):
        p = self.job("run", HOST, "T-r-stall", "--poll", "1", "--stall", "0.1", "--", "echo s; sleep 300")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.assertIn("Running", self.rstatus(tid))
        self.rpueue("kill", str(tid))

    def test_busy_is_not_stall(self):
        p = self.job("run", HOST, "T-r-busy", "--poll", "1", "--stall", "0.1", "--",
                     "timeout 15 sh -c 'while :; do :; done'; echo done")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_check_alert(self):
        chk = os.path.join(os.environ.get("TMPDIR", "/tmp"), "job-remote-check-%d.sh" % os.getpid())
        with open(chk, "w") as f:
            f.write("#!/bin/sh\nif grep -q Traceback; then echo traceback in output; exit 1; fi\n")
        os.chmod(chk, 0o755)
        try:
            p = self.job("run", HOST, "T-r-check", "--poll", "1", "--check", chk, "--",
                         "echo ok; sleep 2; echo Traceback; sleep 300")
        finally:
            os.remove(chk)
        self.assertEqual(p.returncode, 201, p.stdout + p.stderr)
        self.assertIn("traceback in output", p.stdout)
        tid = int(p.stdout.split("#", 1)[1].split()[0])
        self.rpueue("kill", str(tid))

    def test_wait_finished_and_self_match(self):
        words = "job wait %s pueue pgrep -f mag_check.py" % HOST
        p = self.job("submit", HOST, "T-r-self", "--", "echo '%s'; exit 6 # %s" % (words, words))
        tid = int(p.stdout.strip())
        time.sleep(3)
        p = self.job("wait", HOST, str(tid), "--poll", "1")
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        self.assertIn("pgrep -f mag_check.py", p.stdout)

    def test_group_and_ls(self):
        g = self.job("group", HOST, "rq", "1")
        self.assertEqual(g.returncode, 0, g.stdout + g.stderr)
        a = int(self.job("submit", HOST, "T-r-g1", "--group", "rq", "--", "sleep 5").stdout)
        b = int(self.job("submit", HOST, "T-r-g2", "--group", "rq", "--", "true",
                         JOB_SESSION="remote-sess").stdout)
        time.sleep(2)
        self.assertIn("Queued", self.rstatus(b))
        ls = self.job("ls", HOST)
        self.assertIn("T-r-g2", ls.stdout)
        self.assertIn("remote-sess", ls.stdout)
        p = self.job("wait", HOST, str(b), "--poll", "1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Done", self.rstatus(a))

    def test_pipeline_failure(self):
        p = self.job("run", HOST, "T-r-pipe", "--poll", "1", "--", "python3 -c 'import sys; sys.exit(4)' 2>&1 | tail -1")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)

    def test_ls_run_on_the_server_itself(self):
        b = int(self.job("submit", HOST, "T-r-self-ls", "--", "true", JOB_SESSION="self-ls").stdout)
        out = ssh("JOB_REMOTE_PUEUE_CONFIG= JOB_PUEUE_CONFIG=%s ~/.local/bin/job ls %s; echo rc=$?" % (self.cfg, HOST))
        self.assertIn("T-r-self-ls", out)
        self.assertIn("rc=0", out)
        self.assertNotIn("UNREACHABLE", out)
        out = ssh("JOB_PUEUE_CONFIG=%s ~/.local/bin/job wait %s %d --poll 1; echo rc=$?" % (self.cfg, HOST, b))
        self.assertIn("rc=0", out)

    @unittest.skipUnless(os.environ.get("JOB_TEST_REAL_DAEMON"), "JOB_TEST_REAL_DAEMON not set")
    def test_real_daemon(self):
        p = self.job("run", HOST, "TEST-agent-pueue", "--poll", "2", "--", "echo job-test-ok", real=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.job("run", HOST, "TEST-agent-pueue", "--poll", "2", "--", "echo job-test-fail; exit 3", real=True)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
