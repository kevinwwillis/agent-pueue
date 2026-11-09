"""Tests of jobs run in their own systemd user scope (admission, TASK-003.02).

Real throwaway pueued (see test_job.Daemon) and real user scopes. Skipped where
`systemd-run --user --scope` does not work (no user manager, no delegation).

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from test_job import JOB, Base

MiB = 1 << 20


def _scopes_work():
    """True when a delegated user scope gets the memory controller, as the shim needs."""
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())
    try:
        p = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "-p", "Delegate=yes", "--", "sh", "-c",
                            'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/cgroup.controllers"'],
                           env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "memory" in p.stdout.split()


SCOPES = _scopes_work()


def _own_cgroup_entries():
    with open("/proc/self/cgroup") as f:
        cg = f.read().split("::", 1)[1].strip()
    return sorted(os.listdir("/sys/fs/cgroup" + cg))


@unittest.skipUnless(SCOPES, "systemd-run --user --scope does not work here")
class ScopeTests(Base):

    def host_submit(self, command, memory_max=256 * MiB, gpus=None, gpu_env=None, env=None,
                    protocol=5, proc_env=None, scope=True, extra=None):
        req = {"protocol": protocol, "command": command, "task": "T-scope", "cwd": self.d.dir,
               "session": "test", "from": "test", "env": env or {}}
        if scope:
            req["scope"] = {"memory_max": memory_max, "gpus": gpus, "gpu_env": gpu_env or {},
                            "oom_score_adj": 500}
        req.update(extra or {})
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

    def unit_loaded(self, token):
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())
        p = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState", "--value",
                            "agent-pueue-job-%s.scope" % token], env=env, capture_output=True, text=True)
        return p.stdout.strip() == "loaded"

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
        self.assertIn("note: it went over its RAM limit of 0.10 GB; the whole job was stopped", p.stdout)
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
        self.assertGreaterEqual(res["memory_peak_incl_cache"], 60 * MiB)
        self.assertEqual(res["memory_events"].get("oom_kill"), 0)
        self.assertIsNotNone(res["cpu_usage_usec"])

    def test_scope_limits_are_in_effect(self):
        tid, token = self.submitted(
            'cg="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; '
            'echo "max=$(cat $cg/memory.max) swap=$(cat $cg/memory.swap.max) group=$(cat $cg/memory.oom.group) '
            'adj=$(cat /proc/self/oom_score_adj) nice=$(cut -d" " -f19 /proc/self/stat) at=$(basename $(dirname $cg))/$(basename $cg)"',
            memory_max=200 * MiB)
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("max=%d swap=0 group=1 adj=500 nice=19 at=agent-pueue-job-%s.scope/job" % (200 * MiB, token),
                      p.stdout)

    def test_normal_priority_scoped_job_runs_at_nice_0(self):
        reply = self.host_submit("cut -d' ' -f19 /proc/self/stat", extra={"normal_priority": True})
        self.assertTrue(reply["ok"], reply)
        p = self.wait(reply["data"]["id"], reply["data"]["token"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"(?m)^\s*0$")

    def test_host_out_of_memory_is_told_apart_from_the_jobs_limit(self):
        jobmod = self.load_job()
        jobmod.STATE_DIR = os.path.join(self.d.dir, "state", "agent-pueue")
        res_dir = os.path.join(jobmod.STATE_DIR, "results")
        os.makedirs(res_dir, exist_ok=True)
        tok = "f" * 32
        with open(os.path.join(res_dir, tok + ".scope"), "w") as f:
            f.write("rc=137\nmemory_max=1073741824\nstarted=1\nResult=success\n")
        with open(os.path.join(res_dir, tok + ".final"), "w") as f:
            f.write("rc=137\nleftover=0\nmemory.peak=50000000\nmemory.events.oom=0\n"
                    "memory.events.oom_kill=3\nmemory.events.oom_group_kill=1\n")
        res = jobmod._scope_result(tok)
        self.assertEqual(res["how"], "host_oom")
        v, c, note = jobmod.scope_verdict("FAILED", 137, res)
        self.assertIn("the host ran out of memory", note)
        self.assertIn("under its RAM limit of 1.07 GB", note)

    # -- failures of the wrapper itself -----------------------------------------

    def fake_home(self, systemd_run):
        """A HOME whose ~/.local/bin/systemd-run (first on the wrapper's PATH) is the given script."""
        home = tempfile.mkdtemp(dir=self.d.dir)
        os.makedirs(os.path.join(home, ".local", "bin"))
        path = os.path.join(home, ".local", "bin", "systemd-run")
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + systemd_run + "\n")
        os.chmod(path, 0o755)
        return home

    def test_systemd_run_failure_is_a_wrapper_error(self):
        marker = os.path.join(self.d.dir, "wrapper-error-ran")
        tid, token = self.submitted("touch %s" % marker, proc_env={"HOME": self.fake_home("exit 1")})
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("job: WRAPPER_ERROR", p.stdout)
        self.assertIn("could not be started", p.stdout)
        self.assertFalse(os.path.exists(marker), "the job ran although its scope never started")

    def test_shim_outside_its_own_scope_refuses_to_run(self):
        # a systemd-run that runs the shim without making a scope: the shim must not touch the cgroup it is in
        marker = os.path.join(self.d.dir, "outside-scope-ran")
        before = _own_cgroup_entries()
        tid, token = self.submitted("touch %s" % marker,
                                    proc_env={"HOME": self.fake_home('while [ "$1" != -- ]; do shift; done; shift; exec "$@"')})
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("memory limit could not be set up (not running in its own scope)", p.stdout)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(before, _own_cgroup_entries(), "the shim changed a cgroup that is not its own scope")

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

    def test_gpu_variables_in_gpu_env_are_refused(self):
        reply = self.host_submit("true", gpus=[], gpu_env={"CUDA_VISIBLE_DEVICES": "0"})
        self.assertFalse(reply["ok"])
        self.assertIn("may not set CUDA_VISIBLE_DEVICES", reply["error"])

    def test_scope_without_gpus_hides_every_gpu(self):
        tid, token = self.submitted('echo "cvd=[${CUDA_VISIBLE_DEVICES-unset}]"', gpus=None)
        p = self.wait(tid, token)
        self.assertIn("cvd=[]", p.stdout)

    def test_mig_device_names_are_accepted(self):
        reply = self.host_submit("true", gpus=["MIG-GPU-11111111-2222-3333-4444-555555555555/1/0"])
        self.assertTrue(reply["ok"], reply)
        self.wait(reply["data"]["id"], reply["data"]["token"])

    # -- kill and stall ------------------------------------------------------------

    def test_kill_stops_detached_children(self):
        tid, token = self.submitted("setsid sleep 300 & python3 -c 'b = bytearray(50 * 1024 * 1024); import time; time.sleep(300)'")
        self.wait_status(tid, "Running")
        time.sleep(1.5)
        self.assertGreaterEqual(self.token_alive(token), 2)
        p = self.job("kill", "local", str(tid), "--token", token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for _ in range(50):
            if self.token_alive(token) == 0:
                break
            time.sleep(0.2)
        self.assertEqual(self.token_alive(token), 0, "the detached child outlived job kill")
        jobmod = self.load_job()
        jobmod.STATE_DIR = os.path.join(self.d.dir, "state", "agent-pueue")
        res = jobmod._scope_result(token)
        self.assertEqual(res["how"], "killed")
        self.assertGreaterEqual(res["memory_peak_incl_cache"], 50 * MiB)  # its use up to the kill is kept
        self.assertFalse(self.unit_loaded(token))

    def test_kill_ends_a_term_ignoring_detached_child_at_once(self):
        tid, token = self.submitted("setsid bash -c 'trap \"\" TERM; sleep 300' & sleep 300")
        self.wait_status(tid, "Running")
        time.sleep(1)
        t0 = time.monotonic()
        p = self.job("kill", "local", str(tid), "--token", token)
        took = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(took, 10, "job kill took %.1f s" % took)
        time.sleep(1)
        self.assertEqual(self.token_alive(token), 0, "the TERM-ignoring child outlived job kill")
        self.assertFalse(self.unit_loaded(token))

    def test_processes_left_after_the_job_ends_are_stopped(self):
        tid, token = self.submitted("setsid sleep 300 & echo done")
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("note: 1 process(es) it left running were stopped when it ended", p.stdout)
        time.sleep(0.5)
        self.assertEqual(self.token_alive(token), 0)
        self.assertFalse(self.unit_loaded(token))

    def test_restart_in_place_runs_again(self):
        # a leftover process of the first run must not block the second (same token, same unit name)
        tid, token = self.submitted("setsid sleep 100 & sleep 1; echo run")
        self.assertEqual(self.wait(tid, token).returncode, 0)
        self.d.pueue("restart", str(tid), "--in-place")
        self.wait_status(tid, "Running")
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("job: SUCCESS", p.stdout)

    def test_client_sends_protocol_5_only_for_a_scoped_submit(self):
        jobmod = self.load_job()
        self.assertEqual(jobmod._request_protocol("submit", {"scope": {"memory_max": 1 << 30}}), 5)
        self.assertEqual(jobmod._request_protocol("submit", {"command": "true"}), 3)
        self.assertEqual(jobmod._request_protocol("probe", {"scope": {}}), 3)

    def test_removed_pueue_entry_keeps_the_scope_verdict(self):
        tid, token = self.submitted("python3 -c 'b = bytearray(300 * 1024 * 1024)'", memory_max=100 * MiB)
        self.wait_status(tid, "Done")
        self.d.pueue("remove", str(tid))
        p = self.wait(tid, token)
        self.assertEqual(p.returncode, 137, p.stdout + p.stderr)
        self.assertIn("went over its RAM limit of 0.10 GB", p.stdout)
        self.assertIn("pueue entry was removed", p.stdout)

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
