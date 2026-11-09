"""Tests of jobs run in their own systemd user scope (admission, TASK-003.02).

Real throwaway pueued (see test_job.Daemon) and real user scopes. Skipped where
`systemd-run --user --scope` does not work (no user manager, no delegation).

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import time
import unittest

from test_job import JOB, Base

MiB = 1 << 20


def _scopes_work():
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())
    try:
        p = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=64M",
                            "-p", "MemorySwapMax=0", "-p", "OOMPolicy=kill", "--", "true"],
                           env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


SCOPES = _scopes_work()


@unittest.skipUnless(SCOPES, "systemd-run --user --scope does not work here")
class ScopeTests(Base):

    def host_submit(self, command, memory_max=256 * MiB, gpus=None, gpu_env=None, env=None,
                    protocol=5, proc_env=None, scope=True):
        req = {"protocol": protocol, "command": command, "task": "T-scope", "cwd": self.d.dir,
               "session": "test", "from": "test", "env": env or {}}
        if scope:
            req["scope"] = {"memory_max": memory_max, "gpus": gpus, "gpu_env": gpu_env or {},
                            "oom_score_adj": 500}
        p = subprocess.run([sys.executable, JOB, "_host", "submit"], input=json.dumps(req),
                           capture_output=True, text=True, env=self.d.env(**(proc_env or {})), timeout=60)
        reply = json.loads(p.stdout.strip().splitlines()[-1])
        return reply

    def submitted(self, *args, **kw):
        reply = self.host_submit(*args, **kw)
        self.assertTrue(reply["ok"], reply)
        return reply["data"]["id"], reply["data"]["token"]

    def wait(self, tid, token, *extra):
        return self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3", *extra)

    def results(self, token, ext):
        return os.path.join(self.d.dir, "state", "agent-pueue", "results", token + ext)

    def token_alive(self, token):
        jobmod = self.load_job()
        ticks, nprocs, _ = jobmod._proc_tree(token)
        return nprocs

    # -- memory limit ------------------------------------------------------------

    def test_over_memory_limit_stops_the_whole_job(self):
        # a background worker and a process that grows past the 100 MiB limit
        tid, token = self.submitted(
            "sleep 300 & python3 -c 'b = bytearray(400 * 1024 * 1024); import time; time.sleep(30)'; wait",
            memory_max=100 * MiB)
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 137, p.stdout + p.stderr)
        self.assertIn("job: FAILED local#%d" % tid, p.stdout)
        self.assertRegex(p.stdout, r"(went over its RAM limit of 0\.10 GB|killed for memory)")
        self.assertIn("the whole job was stopped", p.stdout)
        time.sleep(1)
        self.assertEqual(self.token_alive(token), 0, "a process of the job survived the memory kill")
        with open(self.results(token, ".exit")) as f:
            self.assertEqual(f.read().strip(), "137")

    def test_clean_exit_records_final_peaks(self):
        tid, token = self.submitted("python3 -c 'b = bytearray(60 * 1024 * 1024); print(len(b))'",
                                    memory_max=512 * MiB)
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("job: SUCCESS", p.stdout)
        jobmod = self.load_job()
        jobmod.STATE_DIR = os.path.join(self.d.dir, "state", "agent-pueue")
        res = jobmod._scope_result(token)
        self.assertEqual(res["how"], "exit")
        self.assertGreaterEqual(res["memory_peak"], 60 * MiB)
        self.assertEqual(res["memory_events"].get("oom_kill"), 0)
        self.assertIsNotNone(res["cpu_usage_usec"])

    def test_scope_limits_are_in_effect(self):
        tid, token = self.submitted(
            'cg="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; '
            'echo "max=$(cat $cg/memory.max) swap=$(cat $cg/memory.swap.max) group=$(cat $cg/memory.oom.group) '
            'adj=$(cat /proc/self/oom_score_adj) unit=$(basename $cg)"',
            memory_max=200 * MiB)
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("max=%d swap=0 group=1 adj=500 unit=agent-pueue-job-%s.scope" % (200 * MiB, token), p.stdout)

    # -- failures of the wrapper itself -----------------------------------------

    def test_systemd_run_failure_is_a_wrapper_error(self):
        marker = os.path.join(self.d.dir, "wrapper-error-ran")
        tid, token = self.submitted("touch %s" % marker, proc_env={"JOB_SYSTEMD_RUN": "false"})
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("job: WRAPPER_ERROR", p.stdout)
        self.assertIn("could not be started", p.stdout)
        self.assertFalse(os.path.exists(marker), "the job ran although its scope never started")

    def test_scope_needs_protocol_5(self):
        reply = self.host_submit("true", protocol=4)
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["error"].startswith("protocol mismatch"), reply)

    def test_bad_scope_request_is_refused(self):
        reply = self.host_submit("true", memory_max=10)
        self.assertFalse(reply["ok"])
        self.assertIn("memory_max", reply["error"])

    # -- GPUs ----------------------------------------------------------------------

    def test_gpu_assignment_by_uuid(self):
        tid, token = self.submitted(
            'echo "cvd=$CUDA_VISIBLE_DEVICES order=$CUDA_DEVICE_ORDER uuids=$JOB_GPU_UUIDS tf=$TF_FORCE_GPU_ALLOW_GROWTH '
            'xla=$XLA_PYTHON_CLIENT_PREALLOCATE"',
            gpus=["GPU-11111111-2222-3333-4444-555555555555"],
            gpu_env={"TF_FORCE_GPU_ALLOW_GROWTH": "true", "XLA_PYTHON_CLIENT_PREALLOCATE": "false"},
            env={"XLA_PYTHON_CLIENT_PREALLOCATE": "true"})  # the job's own --env wins over the default
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("cvd=GPU-11111111-2222-3333-4444-555555555555 order=PCI_BUS_ID "
                      "uuids=GPU-11111111-2222-3333-4444-555555555555 tf=true xla=true", p.stdout)

    def test_no_gpu_asked_hides_every_gpu(self):
        tid, token = self.submitted('echo "cvd=[${CUDA_VISIBLE_DEVICES-unset}]"', gpus=[])
        p = self.wait(tid, token)
        self.assertIn("cvd=[]", p.stdout)

    def test_cuda_visible_devices_from_env_is_refused(self):
        reply = self.host_submit("true", gpus=[], env={"CUDA_VISIBLE_DEVICES": "1"})
        self.assertFalse(reply["ok"])
        self.assertIn("CUDA_VISIBLE_DEVICES", reply["error"])

    # -- kill and stall ------------------------------------------------------------

    def test_kill_stops_detached_children(self):
        tid, token = self.submitted("setsid sleep 300 & sleep 300")
        self.wait_status(tid, "Running")
        time.sleep(1)
        self.assertGreaterEqual(self.token_alive(token), 2)
        p = self.job("kill", "local", str(tid), "--token", token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for _ in range(50):
            if self.token_alive(token) == 0:
                break
            time.sleep(0.2)
        self.assertEqual(self.token_alive(token), 0, "the detached child outlived job kill")

    def test_stall_detection_finds_the_job_in_its_scope(self):
        tid, token = self.submitted("sleep 120")
        p = self.wait(tid, token, "--stall", "0.05")
        self.assertEqual(p.returncode, 200, p.stdout + p.stderr)
        self.assertIn("its processes used", p.stdout)  # found by token inside the scope, not "no live process"
        self.job("kill", "local", str(tid), "--token", token)

    def test_unscoped_submit_is_unchanged(self):
        tid, token = self.submitted('cat /proc/self/cgroup', scope=False, protocol=3)
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("agent-pueue-job-", p.stdout)


if __name__ == "__main__":
    unittest.main()
