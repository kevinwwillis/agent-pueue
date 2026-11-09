"""Tests of admission (docs/admission.md): settings, room rules, and the loop releasing real jobs.

The room rules are tested as plain functions on made-up samples. The loop tests submit real jobs to a
throwaway pueued (see test_job.Daemon) and run the loop's cycles in this process against a fake /proc and a
fake GPU list, while jobs run in real systemd user scopes (skipped where those don't work).

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
from test_scope import SCOPES

GB, MiB = 10 ** 9, 1 << 20
CARD = "GPU-aaaaaaaa-0000-0000-0000-000000000001"
CARD2 = "GPU-aaaaaaaa-0000-0000-0000-000000000002"


def load():
    return Base.load_job()


def settings(jobmod, tmp, **over):
    path = os.path.join(tmp, "admission-%d.json" % time.monotonic_ns())
    raw = {"state_dir": os.path.join(tmp, "adm"), "ram_headroom_gb": 1, "cpu_busy_threads_max": 8,
           "placed_cores_max": 12, "gpu_headroom_mb": 500, "outside_window_s": 60, "cpu_window_s": 60}
    raw.update(over)
    with open(path, "w") as f:
        json.dump(raw, f)
    return jobmod._admission_settings(path, nthreads=8)


def sample(t, avail=50 * GB, busy=1.0, psi=0.0, gpus=None, jobs=None, disks=None, iowait=0.0):
    return {"t": t, "busy": busy, "iowait": iowait, "avail": avail, "psi": psi, "disks": disks or {},
            "gpus": gpus or {}, "jobs": jobs or {}, "holds": {}}


def card(used, total=24 * GB, util=0, index=0, name="RTX 3090"):
    return {"index": index, "name": name, "total": total, "used": used, "util": util}


def held(cpus=1, ram=1 * GB, gpus=0, gpu_mem=0, pilot=False, **kw):
    q = {"size": {"cpus": cpus, "ram": ram, "gpus": gpus, "gpu_mem": gpu_mem, "gpu_share": 0, "disk_mb_s": 0},
         "pilot": pilot, "constraints": {}}
    q.update(kw)
    return q


class RoomRules(unittest.TestCase):
    """compute_room and fits on made-up samples."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.j = load()
        cls.tmp = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        cls.s = settings(cls.j, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        import shutil
        if cls.tmp.startswith("/tmp/jobt."):
            shutil.rmtree(cls.tmp)

    def room(self, samples, inflight=(), now=None):
        return self.j.compute_room(self.s, samples, list(inflight), [], now or samples[-1]["t"])

    def test_ram_fit_and_no_fit(self):
        r = self.room([sample(100, avail=11 * GB)])
        self.assertTrue(self.j.fits(self.s, r, held(ram=9 * GB))[0])
        ok, why = self.j.fits(self.s, r, held(ram=11 * GB))
        self.assertFalse(ok)
        self.assertIn("needs 11.0 GB RAM, 10.0 GB free", why)

    def test_outside_ram_counts_at_its_highest_in_the_window(self):
        # someone else's process took 20 GB 30 s ago and let it go: the room stays down for the window
        r = self.room([sample(70, avail=30 * GB), sample(100, avail=50 * GB)])
        self.assertAlmostEqual(r["ram"]["room"], 29 * GB)
        r = self.room([sample(30, avail=30 * GB), sample(100, avail=50 * GB)])  # outside the 60 s window
        self.assertAlmostEqual(r["ram"]["room"], 49 * GB)

    def test_admitted_job_counts_at_the_larger_of_size_and_use(self):
        tok = "a" * 32
        jobs = {tok: {"ram": 6 * GB, "cpu": 0.5, "gpu": {}}}
        inflight = [{"token": tok, "released": 90, "size": {"cpus": 2, "ram": 4 * GB}, "gpus": [],
                     "ram_counted": 4 * GB, "gpu_counted": {}}]
        r = self.room([sample(100, avail=24 * GB, jobs=jobs)], inflight)
        # with no job, 30 GB would be free; minus 1 GB headroom, minus the job at its use (6 GB > size 4 GB)
        self.assertAlmostEqual(r["ram"]["room"], 23 * GB)

    def test_a_finished_job_frees_its_room_at_once(self):
        tok = "b" * 32
        inflight = [{"token": tok, "released": 90, "size": {"cpus": 2, "ram": 20 * GB}, "gpus": [],
                     "ram_counted": 20 * GB, "gpu_counted": {}}]
        s = [sample(100, avail=30 * GB)]
        self.assertFalse(self.j.fits(self.s, self.room(s, inflight), held(ram=15 * GB))[0])
        self.assertTrue(self.j.fits(self.s, self.room(s, []), held(ram=15 * GB))[0])

    def test_cpu_outside_use_and_placed_limit(self):
        r = self.room([sample(t, busy=5.0) for t in (70, 85, 100)])
        self.assertTrue(self.j.fits(self.s, r, held(cpus=3))[0])
        ok, why = self.j.fits(self.s, r, held(cpus=4))
        self.assertFalse(ok)
        self.assertIn("CPU threads, 3.0 free", why)

    def test_older_jobs_count_at_their_measured_cpu(self):
        tok = "c" * 32
        jobs = {tok: {"ram": 0, "cpu": 1.0, "gpu": {}}}
        inflight = [{"token": tok, "released": 0, "size": {"cpus": 6, "ram": 0}, "gpus": [], "ram_counted": 0,
                     "gpu_counted": {}}]
        r = self.room([sample(t, busy=1.0, jobs=jobs) for t in (70, 85, 100)], inflight)
        self.assertAlmostEqual(r["cpu"]["room"], 7.0)  # 8 - outside 0 - measured 1.0, not its size of 6
        self.assertAlmostEqual(r["cpu"]["placed_room"], 6.0)  # sizes still count against placed cores

    def test_gpu_outside_use_counts(self):
        # another user's process holds 20 GB of the card
        r = self.room([sample(t, gpus={CARD: card(20 * GB)}) for t in (90, 100)])
        ok, why = self.j.fits(self.s, r, held(gpus=1, gpu_mem=4 * GB))
        self.assertFalse(ok)
        self.assertIn("GPU 0 has 3.5 GB free", why)
        self.assertTrue(self.j.fits(self.s, r, held(gpus=1, gpu_mem=3 * GB))[0])

    def test_gpu_outside_is_the_lower_of_two_passes(self):
        # a one-sample spike (device and process values read apart) does not close the card
        r = self.room([sample(80, gpus={CARD: card(2 * GB)}), sample(90, gpus={CARD: card(20 * GB)}),
                       sample(100, gpus={CARD: card(2 * GB)})])
        self.assertAlmostEqual(r["gpus"][CARD]["outside"], 2 * GB)

    def test_gpu_best_fit_and_busy_card(self):
        g = {CARD: card(2 * GB, index=0), CARD2: card(14 * GB, index=1)}
        r = self.room([sample(t, gpus=g) for t in (90, 100)])
        ok, got = self.j.fits(self.s, r, held(gpus=1, gpu_mem=6 * GB))
        self.assertTrue(ok)
        self.assertEqual(got["gpus"], [CARD2])  # the card with the least room that still fits
        g = {CARD: card(2 * GB, util=99)}
        r = self.room([sample(t, gpus=g) for t in (90, 100)])
        ok, why = self.j.fits(self.s, r, held(gpus=1, gpu_mem=1 * GB))
        self.assertFalse(ok)
        self.assertIn("GPU 0 is busy", why)

    def test_pilot_takes_all_the_room(self):
        g = {CARD: card(4 * GB)}
        r = self.room([sample(t, avail=21 * GB, gpus=g) for t in (90, 100)])
        ok, got = self.j.fits(self.s, r, held(gpus=1, pilot=True, ram=1 * GB))
        self.assertTrue(ok)
        self.assertAlmostEqual(got["ram_counted"], 20 * GB)
        self.assertAlmostEqual(got["gpu_counted"][CARD], 24 * GB - 0.5 * GB - 4 * GB)

    def test_memory_pressure_stops_release(self):
        r = self.room([sample(100, psi=25.0)])
        ok, why = self.j.fits(self.s, r, held())
        self.assertFalse(ok)
        self.assertIn("memory pressure", why)

    def test_heavy_disk_job_waits_on_a_busy_disk(self):
        s2 = settings(self.j, self.tmp, disk_busy_max_pct=80, iowait_max_pct=20, disks=["/"])
        r = self.j.compute_room(s2, [sample(100, disks={"sda": 95.0, "sdb": 5.0})], [], [], 100)
        heavy = held()
        heavy["size"]["disk_mb_s"] = 200
        self.assertFalse(self.j.fits(s2, r, heavy, "sda")[0])
        self.assertTrue(self.j.fits(s2, r, heavy, "sdb")[0])
        r = self.j.compute_room(s2, [sample(100, disks={"sda": 50.0, "sdb": 5.0}, iowait=40)], [], [], 100)
        self.assertFalse(self.j.fits(s2, r, heavy, "sda")[0])  # high iowait and its disk is the busiest
        self.assertTrue(self.j.fits(s2, r, heavy, "sdb")[0])   # a quiet disk still starts

    def test_cannot_fit_even_when_empty(self):
        cap = {"mem_total": 32 * GB, "threads": 8, "gpus": [{"uuid": CARD, "index": 0, "name": "RTX 2070",
                                                            "total": 8 * GB}]}
        self.assertIn("at most 31.0 GB", self.j.fits_empty(self.s, cap, held(ram=40 * GB)))
        self.assertIn("1 GPU(s) with 9.0 GB", self.j.fits_empty(self.s, cap, held(gpus=1, gpu_mem=9 * GB)))
        self.assertIsNone(self.j.fits_empty(self.s, cap, held(gpus=1, gpu_mem=7 * GB)))


class RoomRulesCycle2(unittest.TestCase):
    """Room rules from the second review."""

    @classmethod
    def setUpClass(cls):
        cls.j = load()
        cls.tmp = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        cls.s = settings(cls.j, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        import shutil
        if cls.tmp.startswith("/tmp/jobt."):
            shutil.rmtree(cls.tmp)

    def test_gpu_use_on_a_card_the_job_was_not_given_still_counts(self):
        tok = "d" * 32
        gp = {CARD: card(10 * GB + 400 * 10 ** 6, index=0), CARD2: card(400 * 10 ** 6, index=1)}
        jobs = {tok: {"ram": GB, "cpu": 1.0, "gpu": {CARD: 10 * GB}}}
        samples = [sample(t, gpus=gp, jobs=jobs) for t in (90, 100)]
        inflight = [{"token": tok, "released": 90, "size": {"cpus": 1, "ram": GB, "gpu_mem": 4 * GB, "gpus": 1},
                     "gpus": [CARD2], "ram_counted": GB, "gpu_counted": {CARD2: 4 * GB}}]
        r = self.j.compute_room(self.s, samples, inflight, [], 100)
        self.assertLessEqual(r["gpus"][CARD]["room"], 24 * GB - 10.4 * GB - 0.5 * GB + 1)
        self.assertAlmostEqual(r["gpus"][CARD2]["room"], 24 * GB - 0.4 * GB - 0.5 * GB - 4 * GB, delta=1e6)

    def test_a_gpu_share_no_card_can_give_is_refused_at_once(self):
        job = held(gpus=1, gpu_mem=GB)
        job["size"]["gpu_share"] = 100.0
        cap = {"mem_total": 64 * GB, "threads": 8, "gpus": [{"uuid": CARD, "index": 0, "name": "RTX 3090",
                                                             "total": 24 * GB}]}
        self.assertIn("of a GPU's time", self.j.fits_empty(self.s, cap, job))
        job["size"]["gpu_share"] = 60.0
        r = self.j.compute_room(self.s, [sample(t, gpus={CARD: card(0)}) for t in (90, 100)],
                                [{"token": "e" * 32, "released": 90, "size": {"cpus": 1, "ram": GB, "gpu_share": 50},
                                  "gpus": [CARD], "ram_counted": GB, "gpu_counted": {CARD: GB}}], [], 100)
        ok, why = self.j.fits(self.s, r, job)
        self.assertFalse(ok)
        self.assertIn("40% of its time left to share, it needs 60%", why)

    def test_a_setting_that_is_not_a_finite_number_is_refused(self):
        p = os.path.join(self.tmp, "nan.json")
        with open(p, "w") as f:
            f.write('{"state_dir": "%s/x", "ram_headroom_gb": NaN}' % self.tmp)
        with self.assertRaises(self.j.JobError):
            self.j._admission_settings(p, nthreads=8)


class Settings(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.j = load()
        self.tmp = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")

    def tearDown(self):
        import shutil
        if self.tmp.startswith("/tmp/jobt."):
            shutil.rmtree(self.tmp)

    def write(self, text):
        p = os.path.join(self.tmp, "a.json")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_missing_file_means_not_an_admission_host(self):
        self.assertIsNone(self.j._admission_settings(os.path.join(self.tmp, "none.json")))

    def test_defaults_and_overrides(self):
        s = self.j._admission_settings(self.write('{"ram_headroom_gb": 16, "small": {"cpus": 2}}'), nthreads=20)
        self.assertEqual(s["ram_headroom_gb"], 16)
        self.assertEqual(s["small"], {"cpus": 2, "ram_gb": 4})
        self.assertEqual(s["cpu_busy_threads_max"], 20)
        self.assertEqual(s["placed_cores_max"], 30)

    def test_warm_up_defaults_to_the_longer_window(self):
        s = self.j._admission_settings(self.write('{"outside_window_s": 300, "cpu_window_s": 600}'))
        self.assertEqual(s["warmup_s"], 600)
        with self.assertRaises(self.j.JobError):
            self.j._admission_settings(self.write('{"warmup_s": 601}'))

    def test_invalid_files_are_refused(self):
        for text, part in (("{", "cannot be read"), ('{"ram_headrom_gb": 1}', "unknown setting ram_headrom_gb"),
                           ('{"ram_hard_factor": 0.5}', "ram_hard_factor"), ('{"disks": ["data"]}', "absolute"),
                           ('{"gpu_env": {"CUDA_VISIBLE_DEVICES": "0"}}', "gpu_env"),
                           ('{"small": {"gpus": 1}}', "small")):
            with self.assertRaises(self.j.JobError) as e:
                self.j._admission_settings(self.write(text))
            self.assertIn(part, str(e.exception))


class ClientTests(unittest.TestCase):
    """auto placement and the client's checks, with the hosts' replies made up."""

    def setUp(self):
        import argparse
        self.j = load()
        self.j.LISTED[:] = ["local", "h1", "h2"]
        for h in ("h1", "h2"):
            self.j.HOSTS[h] = "ssh-" + h
        self.calls = []
        self.args = argparse.Namespace(host="auto", only_hosts=None)
        room = {"cpu": {"room": 8, "placed_room": 8, "iowait": 0, "outside": 0, "committed": 0},
                "ram": {"room": 10 * GB}, "gpus": {}, "disks": {}, "pressure_ok": True, "psi": 0}
        self.snap = lambda **kw: dict({"admission": True, "stale": False, "room": room, "held": [], "paths": [],
                                       "settings": settings(self.j, tempfile.mkdtemp(prefix="jobt.", dir="/tmp"))},
                                      **kw)
        self.refuse = {}

        def submit_to(args, host, command, adm):
            self.calls.append((host, bool(adm.get("fit_now"))))
            if host in self.refuse:
                raise self.j.JobError(self.refuse[host])
            return 7, "t" * 32
        self.j.submit_to = submit_to

    def rooms(self, **by_host):
        self.j._rooms = lambda hosts, timeout=60, paths=(): dict({"local": {"admission": False}}, **by_host)

    def test_auto_tries_the_next_host_when_one_refuses(self):
        self.rooms(h1=self.snap(), h2=self.snap(held=[1, 2]))
        self.refuse["h1"] = "cannot run on h1 even when it is empty: needs 100.0 GB RAM"
        self.assertEqual(self.j.place(self.args, "true", {"ram": 100 * GB})[0], "h2")
        self.assertEqual(self.calls, [("h1", True), ("h2", True)])

    def test_auto_holds_on_the_first_host_that_takes_it_and_never_resends_after_unreachable(self):
        self.rooms(h1=self.snap(), h2=self.snap(held=[1]))
        self.refuse.update(h1="no room on h1 now: needs RAM", h2="no room on h2 now: needs RAM")
        calls = self.calls

        def submit_to(args, host, command, adm):
            calls.append((host, bool(adm.get("fit_now"))))
            if adm.get("fit_now"):
                raise self.j.JobError("no room on %s now" % host)
            raise self.j.Unreachable("%s did not answer" % host)
        self.j.submit_to = submit_to
        with self.assertRaises(self.j.Unreachable):
            self.j.place(self.args, "true", {"ram": GB})
        self.assertEqual(calls, [("h1", True), ("h2", True), ("h1", False)])  # never sent on to h2

    def test_auto_with_a_host_that_is_not_usable(self):
        self.rooms(h1=self.snap(), h2=self.snap())
        with self.assertRaises(self.j.JobError) as e:
            self.j.place(self.args, "true", {"ram": GB, "hosts": ["nope"]})
        self.assertIn("no admission host is available", str(e.exception))

    def test_auto_like_host_id_goes_to_that_host_only(self):
        self.rooms(h1=self.snap(), h2=self.snap())
        self.assertEqual(self.j.place(self.args, "true", {"like": "h2#5"})[0], "h2")
        self.assertEqual(self.calls, [("h2", True)])

    def test_auto_holds_on_a_host_that_is_still_measuring(self):
        self.rooms(h1=self.snap(room=None, warmup={"covered_s": 10, "needed_s": 600}), h2=self.snap(stale=True))
        self.assertEqual(self.j.place(self.args, "true", {"ram": GB})[0], "h1")
        self.assertEqual(self.calls, [("h1", False)])

    def test_like_host_id_on_another_host_is_refused(self):
        import argparse
        args = argparse.Namespace(host="h1", only_hosts=None, pilot=False, small=False, in_containers=False,
                                  restartable=False, like="h2#5", needs_path=None,
                                  **{k: None for k in ("cpus", "ram", "gpus", "gpu_mem", "gpu_share", "gpu_model",
                                                       "data", "stop_at", "max_minutes")})
        with self.assertRaises(self.j.JobError) as e:
            self.j.submit(args, "true")
        self.assertIn("names a job on h2", str(e.exception))

    def test_this_machine_is_asked_once(self):
        self.j.HOSTS["h1"] = None  # h1 is this machine
        self.assertEqual(self.j._placement_hosts(), ["h1", "h2"])

    def test_room_from_a_host_that_predates_admission(self):
        def call(host, op, req, timeout=120):
            raise self.j.JobError("no reply from job on the host (exit 2): usage: job [-h] ... invalid choice: 'room'")
        self.j.call = call
        self.assertEqual(self.j._rooms(["h1"])["h1"], "its job predates admission")

    def test_projected_end_and_ram_trend(self):
        pts = [[100.0 + 10 * i, 5.0 * i, 100.0, (1 + i) * GB] for i in range(5)]  # 0.5 per second, +1 GB per 5
        self.assertAlmostEqual(self.j._projected_end(pts), 140.0 + 80 / 0.5)
        self.assertAlmostEqual(self.j._ram_at_total(pts), 5 * GB + 0.2 * GB * 80)
        self.assertIsNone(self.j._projected_end(pts[:1]))
        self.assertIsNone(self.j._ram_at_total([[0, 0, 100, GB], [1, 1, 100, 2 * GB], [2, 2, 100, 3 * GB]]))  # 2%
        flat = [[i, 10.0 * i, 100.0, GB] for i in range(5)]
        self.assertIsNone(self.j._ram_at_total(flat))

    def fake_ssh(self, script):
        d = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        with open(os.path.join(d, "ssh"), "w") as f:
            f.write("#!/bin/sh\ncat > /dev/null\n" + script + "\n")
        os.chmod(os.path.join(d, "ssh"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + ":" + old
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        import shutil
        self.addCleanup(shutil.rmtree, d)

    def test_a_host_that_gives_no_reply_is_unreachable_not_a_refusal(self):
        self.fake_ssh("echo 'Killed' >&2; exit 137")
        with self.assertRaises(self.j.Unreachable):
            self.j.call("h1", "submit", {"command": "true"})

    def test_a_host_that_refuses_the_command_line_is_a_refusal(self):
        self.fake_ssh("echo 'usage: job [-h] ... invalid choice' >&2; exit 2")
        with self.assertRaises(self.j.JobError) as e:
            self.j.call("h1", "room", {})
        self.assertNotIsInstance(e.exception, self.j.Unreachable)

    def test_sizes_of_nothing_are_refused(self):
        tmp = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, tmp)
        s = settings(self.j, tmp)
        adm = self.j.Admission(s)
        cap = {"mem_total": 64 * GB, "threads": 8, "gpus": []}
        for a, part in (({"cpus": 0, "ram": GB}, "--cpus"), ({"cpus": 1, "ram": 0}, "--ram")):
            with self.assertRaises(self.j.JobError) as e:
                self.j._size_from(s, adm, cap, {"admission": a})
            self.assertIn(part, str(e.exception))
            self.assertNotIn("admission setting", str(e.exception))

    def test_job_use_measures_the_disk_rate(self):
        root = tempfile.mkdtemp(prefix="jobt.", dir="/tmp")
        try:
            scope = os.path.join(root, "scope")
            os.makedirs(os.path.join(scope, "job"))
            os.makedirs(os.path.join(root, "proc", "77"))
            for name, text in (("memory.current", "1000000"), ("memory.stat", "active_file 0\ninactive_file 0"),
                               ("cpu.stat", "usage_usec 0"), ("cgroup.procs", "77")):
                with open(os.path.join(scope, "job", name), "w") as f:
                    f.write(text + "\n")
            m = self.j.Measure(proc=os.path.join(root, "proc"))

            def io(n):
                with open(os.path.join(root, "proc", "77", "io"), "w") as f:
                    f.write("rchar: 1\nread_bytes: %d\nwrite_bytes: %d\n" % (n, n))
            io(0)
            self.assertIsNone(m.job_use(100.0, {"t": scope})["t"]["disk_mb_s"])
            io(50 * 10 ** 6)
            self.assertAlmostEqual(m.job_use(110.0, {"t": scope})["t"]["disk_mb_s"], 10.0)
        finally:
            import shutil
            shutil.rmtree(root)


class FakeHost:
    """A fake /proc (stat, meminfo, pressure, diskstats) and GPU list that a test can change between cycles."""

    def __init__(self, root, threads=8):
        self.root, self.threads = root, threads
        self.proc = os.path.join(root, "proc")
        os.makedirs(os.path.join(self.proc, "pressure"), exist_ok=True)
        self.gpu_file = os.path.join(root, "gpus.json")
        self.ticks = 0
        self.set(avail=50 * GB, busy=0.0, cards=[])

    def set(self, avail=None, busy=None, psi=None, cards=None, procs=None):
        if avail is not None:
            with open(os.path.join(self.proc, "meminfo"), "w") as f:
                f.write("MemTotal: %d kB\nMemAvailable: %d kB\n" % (64 * GB // 1024, avail // 1024))
        if busy is not None:
            self.busy = busy
        with open(os.path.join(self.proc, "pressure", "memory"), "w") as f:
            f.write("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                    "full avg10=%.2f avg60=0.00 avg300=0.00 total=0\n" % (psi or 0.0))
        with open(os.path.join(self.proc, "diskstats"), "w") as f:
            f.write("")
        if cards is not None:
            with open(self.gpu_file, "w") as f:
                json.dump({"cards": cards, "procs": procs or []}, f)
        self.tick()

    def tick(self):
        """Advance the CPU counters by one second at the current busy level."""
        self.ticks += 100
        busy = int(self.busy * 100) * (self.ticks // 100)
        total = self.threads * self.ticks
        lines = ["cpu  %d 0 0 %d 0 0 0 0 0 0" % (busy, total - busy)]
        lines += ["cpu%d 0 0 0 0 0 0 0 0 0 0" % i for i in range(self.threads)]
        with open(os.path.join(self.proc, "stat"), "w") as f:
            f.write("\n".join(lines) + "\n")


@unittest.skipUnless(SCOPES, "systemd-run --user --scope with memory does not work here")
class LoopTests(Base):
    """Real jobs, held and released by the loop."""

    def setUp(self):
        self.j = load()
        self.host = FakeHost(os.path.join(self.d.dir, "fake-%d" % time.monotonic_ns()))
        n = time.monotonic_ns()
        self.cfg = os.path.join(self.d.dir, "admission-%d.json" % n)
        self.state = os.path.join(self.d.dir, "adm-%d" % n)  # each test has its own queue and records
        self.write_settings()
        self.saved = {k: os.environ.get(k) for k in ("JOB_PUEUE_CONFIG", "JOB_TEST_PROC", "JOB_TEST_GPUS")}
        os.environ["JOB_PUEUE_CONFIG"] = self.d.cfg
        self.j.STATE_DIR = os.path.join(self.d.dir, "state", "agent-pueue")
        self.j.ADMISSION_CONFIG = self.cfg

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_settings(self, **over):
        raw = {"state_dir": self.state, "ram_headroom_gb": 1, "cpu_busy_threads_max": 8,
               "placed_cores_max": 12, "gpu_headroom_mb": 500, "sample_interval_s": 1, "pilot": {"max_minutes": 1},
               "fairness_wait_min": 1, "small": {"cpus": 1, "ram_gb": 0.5}, "warmup_s": 0}
        raw.update(over)
        with open(self.cfg, "w") as f:
            json.dump(raw, f)
        self.settings = self.j._admission_settings(self.cfg)

    def env(self):
        return {"JOB_ADMISSION_CONFIG": self.cfg, "JOB_TEST_PROC": self.host.proc, "JOB_TEST_GPUS": self.host.gpu_file}

    def loop(self, clock=time.time):
        m = self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file)
        return self.j.Loop(self.settings, measure=m, clock=clock)

    def submit_sized(self, command, protocol=5, **admission):
        req = {"protocol": protocol, "command": command, "task": "T-adm", "cwd": self.d.dir, "session": "test",
               "from": "test", "env": {}, "admission": admission}
        p = subprocess.run([sys.executable, JOB, "_host", "submit"], input=json.dumps(req), capture_output=True,
                           text=True, env=self.d.env(**self.env()), timeout=60)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def cycles(self, loop, until, n=60, tick=True):
        for _ in range(n):
            if tick:
                self.host.tick()
            loop.cycle()
            if until():
                return True
            time.sleep(0.25)
        return False

    def status(self, tid):
        return next(iter(self.d.status(tid)))

    def wait(self, tid, token):
        return self.job("wait", "local", str(tid), "--token", token, "--poll", "0.3")

    def record(self, token):
        import sqlite3
        con = sqlite3.connect(os.path.join(self.settings["state_dir"], "admission.sqlite"))
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM jobs WHERE token = ?", (token,)).fetchone()
        con.close()
        return dict(r) if r else None

    def test_a_job_that_fits_is_released_and_recorded(self):
        r = self.submit_sized("python3 -c 'b = bytearray(80 * 1024 * 1024); print(\"ran\")'", cpus=1, ram=400 * MiB)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertEqual(self.status(tid), "Stashed")
        loop = self.loop()
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        p = self.wait(tid, tok)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ran", p.stdout)
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        rec = self.record(tok)
        self.assertEqual(rec["how"], "exit")
        self.assertEqual(rec["exit"], 0)
        self.assertEqual(rec["peaks_final"], 1)
        self.assertGreaterEqual(rec["memory_peak"], 80 * MiB)

    def test_a_job_that_does_not_fit_waits_until_room_opens(self):
        self.host.set(avail=3 * GB)
        r = self.submit_sized("echo released", cpus=1, ram=4 * GB)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        loop = self.loop()
        for _ in range(3):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")
        with open(os.path.join(self.settings["state_dir"], "room.json")) as f:
            snap = json.load(f)
        self.assertIn("needs 4.0 GB RAM, 2.0 GB free", snap["held"][0]["reason"])
        self.host.set(avail=8 * GB)
        loop.samples = []  # the old low readings leave the window
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.assertEqual(self.wait(tid, tok).returncode, 0)

    def test_unsized_and_old_protocol_submits_are_refused(self):
        r = self.submit_sized("true")
        self.assertFalse(r["ok"])
        self.assertIn("needs exactly one size", r["error"])
        p = self.job("submit", "local", "T-old", "--", "true", **self.env())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("protocol mismatch", p.stdout + p.stderr)

    def test_a_job_too_big_for_the_host_is_refused_at_once(self):
        r = self.submit_sized("true", cpus=1, ram=100 * GB)
        self.assertFalse(r["ok"])
        self.assertIn("even when it is empty", r["error"])

    def test_gpu_is_assigned_by_uuid_at_release(self):
        self.host.set(cards=[{"index": 0, "uuid": CARD, "name": "RTX 3090", "total": 24 * GB, "free": 20 * GB}])
        r = self.submit_sized('echo "cvd=$CUDA_VISIBLE_DEVICES"', cpus=1, ram=200 * MiB, gpus=1, gpu_mem=4 * GB)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        loop = self.loop()
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.assertIn("cvd=%s" % CARD, self.wait(tid, tok).stdout)

    def test_fit_now_refuses_without_room(self):
        self.host.set(avail=3 * GB)
        loop = self.loop()
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        r = self.submit_sized("true", cpus=1, ram=4 * GB, fit_now=True)
        self.assertFalse(r["ok"])
        self.assertIn("no room", r["error"])
        r = self.submit_sized("true", cpus=1, ram=1 * GB, fit_now=True)
        self.assertTrue(r["ok"], r)

    def test_pilot_is_stopped_at_its_time_limit_and_sizes_the_next_run(self):
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("python3 -c 'b = bytearray(60 * 1024 * 1024); import time; time.sleep(300)'",
                              pilot=True)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB, n=20)
        self.cycles(loop, lambda: loop.peaks[tok]["n"] >= 3, n=20)  # measured often enough to size from
        now[0] += 61  # past the pilot's one-minute limit
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        rec = self.record(tok)
        self.assertEqual(rec["how"], "pilot time limit")
        self.assertIn("stopped this pilot at its time limit, as planned", self.wait(tid, tok).stdout)
        self.assertGreaterEqual(rec["peak_ram"], 60 * MiB)
        r2 = self.submit_sized("echo sized", like=tok)
        self.assertTrue(r2["ok"], r2)
        with open(os.path.join(self.settings["state_dir"], "queue", r2["data"]["token"] + ".json")) as f:
            q = json.load(f)
        self.assertEqual(q["size_source"], "like")
        self.assertGreaterEqual(q["size"]["ram"], 60 * MiB * 1.25)

    def test_like_refuses_a_run_killed_for_memory(self):
        loop = self.loop()
        r = self.submit_sized("python3 -c 'b = bytearray(400 * 1024 * 1024)'", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        self.assertEqual(self.record(tok)["how"], "memory limit")
        r2 = self.submit_sized("true", like=tok)
        self.assertFalse(r2["ok"])
        self.assertIn("lower bound", r2["error"])

    def test_paused_group_alerts_and_holds(self):
        self.d.pueue("group", "add", "padm")
        self.d.pueue("pause", "--group", "padm")
        req_group = {"protocol": 5, "command": "true", "task": "T-adm", "cwd": self.d.dir, "session": "test",
                     "from": "test", "env": {}, "group": "padm", "admission": {"cpus": 1, "ram": 100 * MiB}}
        p = subprocess.run([sys.executable, JOB, "_host", "submit"], input=json.dumps(req_group),
                           capture_output=True, text=True, env=self.d.env(**self.env()), timeout=60)
        r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(r["ok"], r)
        loop = self.loop()
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(r["data"]["id"]), "Stashed")
        with open(os.path.join(self.settings["state_dir"], "alerts", r["data"]["token"] + ".jsonl")) as f:
            alerts = [json.loads(x) for x in f]
        self.assertEqual([a["kind"] for a in alerts], ["group_paused"])
        self.d.pueue("start", "--group", "padm")

    def test_older_job_short_of_room_blocks_younger_after_the_fairness_wait(self):
        now = [time.time()]
        self.host.set(avail=12 * GB)
        loop = self.loop(clock=lambda: now[0])
        first = self.submit_sized("sleep 60", cpus=1, ram=6 * GB)  # runs, and is counted at 6 GB
        self.assertTrue(self.cycles(loop, lambda: self.status(first["data"]["id"]) == "Running"))
        big = self.submit_sized("echo big", cpus=1, ram=8 * GB)    # 5 GB free: short, but fits once first ends
        self.assertTrue(big["ok"], big)
        now[0] += 120  # past the one-minute fairness wait
        small = self.submit_sized("echo small", cpus=1, ram=2 * GB)  # fits, but takes RAM the big one needs
        for _ in range(3):
            self.host.tick()
            loop.cycle()
        try:
            self.assertEqual(self.status(small["data"]["id"]), "Stashed")
            with open(os.path.join(self.settings["state_dir"], "room.json")) as f:
                reasons = {h["token"]: h["reason"] for h in json.load(f)["held"]}
            self.assertIn("older job", reasons[small["data"]["token"]])
        finally:
            self.job("kill", "local", str(first["data"]["id"]))

    def test_a_job_that_cannot_fit_even_alone_does_not_block_younger_ones(self):
        now = [time.time()]
        self.host.set(avail=6 * GB)  # outside use leaves 5 GB
        loop = self.loop(clock=lambda: now[0])
        big = self.submit_sized("echo big", cpus=1, ram=8 * GB)
        now[0] += 120
        small = self.submit_sized("echo small", cpus=1, ram=2 * GB)
        self.assertTrue(self.cycles(loop, lambda: self.status(small["data"]["id"]) == "Done"))
        self.assertEqual(self.status(big["data"]["id"]), "Stashed")

    def test_a_removed_held_job_is_recorded_and_forgotten(self):
        now = [time.time()]
        self.host.set(avail=1 * GB)
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("true", cpus=1, ram=4 * GB)
        self.d.pueue("remove", str(r["data"]["id"]))
        now[0] += 200
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.record(r["data"]["token"])["how"], "removed")
        self.assertFalse(os.path.exists(os.path.join(self.settings["state_dir"], "queue",
                                                     r["data"]["token"] + ".json")))

    # -- the client ------------------------------------------------------------------------------------------

    def test_sizes_parse(self):
        self.assertEqual(self.j._parse_size("20G"), 20 * 10 ** 9)
        self.assertEqual(self.j._parse_size("512Mi"), 512 * MiB)
        self.assertEqual(self.j._parse_size("1.5T"), 15 * 10 ** 11)
        self.assertEqual(self.j._parse_size("4096"), 4096)
        with self.assertRaises(Exception):
            self.j._parse_size("lots")

    def test_sized_submit_is_held_shown_and_released(self):
        self.host.set(avail=3 * GB)
        loop = self.loop()
        p = self.job("submit", "local", "T-cli", "--cpus", "1", "--ram", "4G", "--", "echo", "cli-ran", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tid = int(p.stdout.strip())
        self.host.tick()
        loop.cycle()
        room = self.job("room", "local", **self.env())
        self.assertIn("held", room.stdout)
        self.assertIn("needs 4.0 GB RAM, 2.0 GB free", room.stdout)
        ls = self.job("ls", "local", **self.env())
        row = [x for x in ls.stdout.splitlines() if x.split()[1:2] == [str(tid)]]
        self.assertTrue(row and "Held" in row[0] and "needs 4.0 GB RAM" in row[0], ls.stdout)
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.d.env(**self.env()))
        time.sleep(1.5)
        self.host.set(avail=8 * GB)
        loop.samples = []
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        out, err = w.communicate(timeout=60)
        self.assertEqual(w.returncode, 0, out + err)
        self.assertIn("held: needs 4.0 GB RAM", err)
        self.assertIn("cli-ran", out)

    def test_an_admission_alert_ends_the_wait(self):
        self.d.pueue("group", "add", "palert")
        self.host.set(avail=2 * GB)
        p = self.job("submit", "local", "T-alert", "--group", "palert", "--cpus", "1", "--ram", "4G", "--", "true",
                     **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tid = int(p.stdout.strip())
        loop = self.loop()
        self.host.tick()
        loop.cycle()  # held for room; the loop is running
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.d.env(**self.env()))
        time.sleep(1.5)
        self.assertIsNone(w.poll(), "a held job is not a verdict")
        self.d.pueue("pause", "--group", "palert")
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        try:
            out, err = w.communicate(timeout=60)
        finally:
            self.d.pueue("start", "--group", "palert")
        self.assertEqual(w.returncode, 201, out + err)
        self.assertIn("job: ALERT", out)
        self.assertIn("its pueue group palert is paused", out)

    def test_auto_places_on_a_host_with_room_or_holds(self):
        loop = self.loop()
        self.host.tick()
        loop.cycle()  # a fresh heartbeat and room snapshot
        sub = subprocess.Popen([sys.executable, JOB, "run", "auto", "T-auto", "--cpus", "1", "--ram", "200M",
                                "--poll", "0.3", "--", "echo", "auto-ran"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=self.d.env(**self.env()))
        self.assertTrue(self.cycles(loop, lambda: sub.poll() is not None, n=80))
        out, err = sub.communicate(timeout=30)
        self.assertEqual(sub.returncode, 0, out + err)
        self.assertIn("auto placed the job on local", err)
        self.assertIn("auto-ran", out)
        self.host.set(avail=2 * GB)
        loop.samples = []
        self.host.tick()
        loop.cycle()
        p = self.job("submit", "auto", "T-auto2", "--cpus", "1", "--ram", "4G", "--", "true", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("held on local", p.stderr)

    # -- holds ---------------------------------------------------------------------------------------------

    def hold(self, *args):
        return self.job("hold", "local", *args, **self.env())

    def model_card(self, model_used=0):
        """One 24 GB card; a model server process (pid 4242, cgroup /system.slice/model.service) uses model_used."""
        os.makedirs(os.path.join(self.host.proc, "4242"), exist_ok=True)
        with open(os.path.join(self.host.proc, "4242", "cgroup"), "w") as f:
            f.write("0::/system.slice/model.service\n")
        used = int(0.4 * GB) + model_used
        self.host.set(cards=[{"index": 0, "uuid": CARD, "name": "RTX 3090", "total": 24 * GB, "free": 24 * GB - used}],
                      procs=[{"uuid": CARD, "pid": 4242, "used": model_used}] if model_used else [])

    def test_a_hold_is_granted_only_with_room_and_keeps_jobs_out_until_released(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "30G", "--name", "too-big")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("GPU 0 on", p.stdout + p.stderr)
        p = self.hold("--gpu", "0", "--mem", "10G", "--name", "model-server", "--cgroup", "/system.slice/model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        hid = p.stdout.strip()
        r = self.submit_sized("echo gpu-job", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=15 * GB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")  # 24 - 0.5 headroom - 0.4 driver - 10 held = 13.1 GB
        room = self.job("room", "local", **self.env())
        self.assertIn("hold %s (model-server): 10.0 GB on GPU 0" % hid, room.stdout)
        self.assertEqual(self.job("release", "local", hid, **self.env()).returncode, 0)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.assertIn("gpu-job", self.wait(tid, tok).stdout)

    def test_a_holds_own_use_is_not_counted_twice(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", CARD, "--mem", "8G", "--name", "model", "--cgroup", "/system.slice/model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.model_card(model_used=6 * GB)  # the model loads into its hold
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        g = loop.room["gpus"][CARD]
        self.assertAlmostEqual(g["outside"], 0.4 * GB, delta=1e6)  # only the driver's reserve
        self.assertAlmostEqual(g["room"], 24 * GB - 0.5 * GB - 0.4 * GB - 8 * GB, delta=1e6)

    def test_a_hold_ends_when_its_time_to_live_runs_out(self):
        self.model_card()
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "4G", "--name", "short", "--ttl", "5")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.host.tick()
        loop.cycle()
        self.assertEqual(len(loop.holds), 1)
        now[0] += 301
        self.host.tick()
        loop.cycle()
        self.assertEqual(loop.holds, [])

    def test_a_hold_ends_when_its_process_exits(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        d = os.path.join(self.host.proc, "5151")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("5151 (model server) S 1 5151 5151 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 777 0 0\n")
        p = self.hold("--gpu", "0", "--mem", "4G", "--name", "served", "--while-pid", "5151")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.host.tick()
        loop.cycle()
        self.assertEqual(len(loop.holds), 1)
        with open(os.path.join(d, "stat"), "w") as f:  # the pid reused by another process: a new start time
            f.write("5151 (other) S 1 5151 5151 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 999 0 0\n")
        self.host.tick()
        loop.cycle()
        self.assertEqual(loop.holds, [])

    # -- review cycle 1 ----------------------------------------------------------------------------------------

    def test_stop_at_stops_a_running_job_and_drops_a_held_one(self):
        loop = self.loop()
        soon = time.time() + 3
        r = self.submit_sized("sleep 60", cpus=1, ram=100 * MiB, stop_at=soon)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done", n=80))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        self.assertEqual(self.record(tok)["how"], "stop-at")
        big = self.submit_sized("true", cpus=1, ram=500 * GB, stop_at=time.time() - 1)
        self.assertFalse(big["ok"])  # cannot run even when empty: refused at once, not held
        held = self.submit_sized("true", cpus=1, ram=48 * GB, stop_at=time.time() + 1)
        self.host.set(avail=10 * GB)
        time.sleep(1.2)
        self.assertTrue(self.cycles(loop, lambda: (self.record(held["data"]["token"]) or {}).get("how")
                                    == "dropped at stop-at"))

    def test_stop_at_needs_an_offset(self):
        r = self.submit_sized("true", cpus=1, ram=100 * MiB, stop_at="2026-11-10T08:00")
        self.assertFalse(r["ok"])
        self.assertIn("UTC offset", r["error"])

    def test_a_pilot_takes_cpus_and_gpus_but_not_ram(self):
        self.host.set(cards=[{"index": 0, "uuid": CARD, "name": "RTX 3090", "total": 24 * GB, "free": 20 * GB}])
        for kw in ({"gpus": 1}, {"cpus": 4}):
            r = self.submit_sized("true", pilot=True, **kw)
            self.assertTrue(r["ok"], r)
            with open(os.path.join(self.state, "queue", r["data"]["token"] + ".json")) as f:
                q = json.load(f)
            self.assertEqual(q["size"]["gpus"], kw.get("gpus", 0))
            self.assertEqual(q["size"]["cpus"], kw.get("cpus", 1))
        r = self.submit_sized("true", pilot=True, ram=GB)
        self.assertFalse(r["ok"])
        self.assertIn("not --ram", r["error"])

    def test_max_minutes_is_a_pilots_limit(self):
        r = self.submit_sized("sleep 30", pilot=True, max_minutes=5)
        with open(os.path.join(self.state, "queue", r["data"]["token"] + ".json")) as f:
            self.assertEqual(json.load(f)["constraints"]["max_minutes"], 5)
        r = self.submit_sized("sleep 30", cpus=1, ram=GB, max_minutes=5)
        self.assertFalse(r["ok"])
        self.assertIn("--stop-at", r["error"])

    def test_leftover_processes_of_a_task_killed_behind_jobs_back_are_stopped(self):
        loop = self.loop()
        r = self.submit_sized("setsid sleep 300 & sleep 300", cpus=1, ram=200 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        time.sleep(1)
        self.d.pueue("kill", str(tid))
        try:
            self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=40))
            left = loop.m.job_use(time.time(), loop.m.scopes()).get(tok)
            self.assertFalse(left and left["populated"], left)
            self.assertEqual(loop.room["ram"]["committed"] if "committed" in loop.room["ram"] else 0, 0)
        finally:
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", "agent-pueue-job-%s.scope" % tok],
                           env=dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid()), capture_output=True)

    def test_peaks_survive_a_loop_restart(self):
        loop = self.loop()
        r = self.submit_sized("python3 -c 'import time; b = bytearray(120 * 1024 * 1024); time.sleep(3); del b; "
                              "time.sleep(5)'", cpus=1, ram=400 * MiB)
        tok = r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 100 * MiB, n=40))
        first = self.record(tok)["peak_ram"]
        time.sleep(3)
        loop2 = self.loop()
        self.assertTrue(self.cycles(loop2, lambda: (self.record(tok) or {}).get("how") is not None, n=60))
        self.assertGreaterEqual(self.record(tok)["peak_ram"], first)

    def test_like_refuses_a_run_measured_too_few_times(self):
        loop = self.loop()
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        tok = r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None))
        r2 = self.submit_sized("true", like=tok)
        self.assertFalse(r2["ok"])
        self.assertIn("too few to size from", r2["error"])

    def test_the_first_cycle_sees_outside_cpu_use(self):
        self.host.set(busy=8.0)  # every thread busy with someone else's work
        loop = self.loop()
        r = self.submit_sized("true", cpus=4, ram=100 * MiB)
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.status(r["data"]["id"]), "Stashed")

    def test_warm_up_holds_jobs_until_the_windows_are_measured_and_survives_a_restart(self):
        self.write_settings(warmup_s=30, outside_window_s=30, cpu_window_s=30)
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        tid = r["data"]["id"]
        for _ in range(3):
            now[0] += 5
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")
        with open(os.path.join(self.state, "room.json")) as f:
            self.assertEqual(json.load(f)["warmup"]["needed_s"], 30)
        self.assertIn("measuring", self.job("room", "local", **self.env()).stdout)
        loop2 = self.loop(clock=lambda: now[0])  # a restart keeps the measured history
        for _ in range(3):
            now[0] += 5
            self.host.tick()
            loop2.cycle()
        self.assertEqual(self.status(tid), "Stashed")  # 30 s covered only now
        now[0] += 5
        self.host.tick()
        loop2.cycle()
        self.assertTrue(self.cycles(loop2, lambda: self.status(tid) == "Done", tick=True))
        now[0] += 3600  # a long gap: the history no longer counts
        loop3 = self.loop(clock=lambda: now[0])
        self.host.tick()
        loop3.cycle()
        self.assertIsNone(loop3.room)

    # -- review cycle 1, second part --------------------------------------------------------------------------

    def test_a_submit_while_the_loop_releases_does_not_deadlock(self):
        import sqlite3
        import threading
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"), timeout=30)
        out = {}

        def sub():
            out["r"] = self.submit_sized("true", cpus=1, ram=100 * MiB)
        fd = loop.adm.lock("queue.lock")  # a submit holds the lock ...
        th = threading.Thread(target=sub)
        th.start()
        time.sleep(1)
        os.close(fd)
        con.execute("INSERT INTO samples VALUES (1, 'x', 'y', 1)")  # ... while the loop has a write open
        loop.release(con, time.time(), loop.adm.queue(), {}, {"groups": {}, "tasks": {}})  # must commit first
        con.close()
        th.join(60)
        self.assertTrue(out["r"]["ok"], out["r"])

    def test_the_snapshot_counts_jobs_released_in_the_same_cycle(self):
        self.host.set(avail=6 * GB)  # room: 6 - 1 headroom = 5 GB
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        a = self.submit_sized("sleep 20", cpus=1, ram=4 * GB)
        self.host.tick()
        loop.cycle()
        self.assertNotEqual(self.status(a["data"]["id"]), "Stashed")
        try:
            with open(os.path.join(self.state, "room.json")) as f:
                self.assertLess(json.load(f)["room"]["ram"]["room"], 1.1 * GB)
            b = self.submit_sized("true", cpus=1, ram=4 * GB, fit_now=True)
            self.assertFalse(b["ok"])
        finally:
            self.job("kill", "local", str(a["data"]["id"]))

    def test_a_pilots_time_counts_from_its_start(self):
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("sleep 120", pilot=True)
        tid, tok = r["data"]["id"], r["data"]["token"]
        qpath = os.path.join(self.state, "queue", tok + ".json")

        def queued():
            with open(qpath) as f:
                return json.load(f)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        self.assertTrue(self.cycles(loop, lambda: queued().get("started"), n=10))
        q = queued()
        q["released"] -= 3600  # released an hour before it started (it waited for a group slot)
        with open(qpath, "w") as f:
            json.dump(q, f)
        self.cycles(loop, lambda: False, n=3)
        self.assertEqual(self.status(tid), "Running")
        now[0] += 61  # past the one-minute limit, counted from its start
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))

    def test_a_job_marked_released_but_never_enqueued_is_held_again(self):
        loop = self.loop()
        r = self.submit_sized("echo again", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        q = os.path.join(self.state, "queue", tok + ".json")
        with open(q) as f:
            data = json.load(f)
        data.update(state="released", released=time.time(), gpus=[], ram_counted=data["size"]["ram"])
        with open(q, "w") as f:  # what a loop stopped between saving and pueue enqueue leaves
            json.dump(data, f)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.assertIn("again", self.wait(tid, tok).stdout)

    def test_a_job_removed_before_it_ran_is_recorded_as_removed(self):
        loop = self.loop()
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        qpath = os.path.join(self.state, "queue", tok + ".json")
        with open(qpath) as f:
            q = json.load(f)
        q.update(state="released", released=time.time(), gpus=[], ram_counted=q["size"]["ram"])
        with open(qpath, "w") as f:  # released, then removed from pueue before it started
            json.dump(q, f)
        self.d.pueue("remove", str(tid))
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.record(tok)["how"], "removed")

    def test_an_explicit_size_needs_ram(self):
        r = self.submit_sized("true", cpus=2)
        self.assertFalse(r["ok"])
        self.assertIn("needs --ram", r["error"])

    def test_needs_path(self):
        r = self.submit_sized("true", cpus=1, ram=GB, needs_path=["/no/such/path"])
        self.assertFalse(r["ok"])
        self.assertIn("/no/such/path", r["error"])
        self.assertTrue(self.submit_sized("true", cpus=1, ram=GB, needs_path=[self.d.dir])["ok"])

    def test_a_container_job_counts_at_zero_after_the_window(self):
        loop = self.loop()
        q = {"token": "c" * 32, "released": 0, "started": 1000.0, "size": {"cpus": 4, "ram": 8 * GB},
             "constraints": {"in_containers": True}, "ram_counted": 8 * GB, "gpu_counted": {"g": GB}}
        self.assertEqual(loop._counted(q, 1000.0 + 599)["ram_counted"], 8 * GB)
        c = loop._counted(q, 1000.0 + 600)
        self.assertEqual((c["ram_counted"], c["size"]["cpus"], c["gpu_counted"]), (0, 0.0, {}))

    def test_the_scope_probe_passes_here(self):
        self.assertIsNone(self.j._scope_probe(dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())))

    def test_progress_gives_a_projected_end_and_an_early_ram_alert(self):
        loop = self.loop()
        cmd = ("python3 -c 'import os, time\nb = []\nfor i in range(1, 9):\n    b.append(bytearray(12 * 1024 * 1024))\n"
               "    open(os.environ[\"JOB_PROGRESS\"], \"a\").write(\"%d 100\\n\" % i)\n    time.sleep(0.6)\n"
               "time.sleep(30)'")
        r = self.submit_sized(cmd, cpus=1, ram=100 * MiB)  # limit 150 MiB; on course for about 1.2 GB at 100
        tid, tok = r["data"]["id"], r["data"]["token"]
        alerts = os.path.join(self.state, "alerts", tok + ".jsonl")
        try:
            self.assertTrue(self.cycles(loop, lambda: os.path.exists(alerts), n=80), "no RAM-trend alert")
            with open(alerts) as f:
                self.assertIn("on course for", f.read())
            with open(os.path.join(self.state, "room.json")) as f:
                j = json.load(f)["jobs"][0]
            self.assertIsNotNone(j["projected_end"])
            self.assertEqual(j["progress"][1], 100)
            self.assertIn("of 100, ends about", self.job("ls", "local", **self.env()).stdout)
        finally:
            self.job("kill", "local", str(tid))

    def test_history_lists_records_with_their_tokens(self):
        loop = self.loop()
        r = self.submit_sized("echo hist", cpus=1, ram=100 * MiB)
        tok = r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None))
        p = self.job("history", "local", "--task", "T-adm", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(tok, p.stdout)
        self.assertIn("explicit: 1 CPU, 0.1 GB", p.stdout)
        p = self.job("history", "local", "--json", "--task", "nothing-like-this", **self.env())
        self.assertEqual(json.loads(p.stdout)["local"]["jobs"], [])

    # -- review cycle 2 ----------------------------------------------------------------------------------------

    def test_a_hold_granted_during_a_cycle_keeps_that_cycles_release_out(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        r = self.submit_sized("echo gpu-job", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=15 * GB)
        tid = r["data"]["id"]
        orig, got = loop.sample, {}

        def sample_then_hold(*a, **k):
            out = orig(*a, **k)
            got["hold"] = self.hold("--gpu", "0", "--mem", "15G", "--name", "svc", "--cgroup",
                                    "/system.slice/model.service")
            return out
        loop.sample = sample_then_hold
        self.host.tick()
        loop.cycle()
        self.assertEqual(got["hold"].returncode, 0, got["hold"].stderr)
        self.assertEqual(self.status(tid), "Stashed")  # 15 GB hold + 15 GB job do not fit a 24 GB card

    def test_a_hold_after_a_release_sees_the_released_job(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        r = self.submit_sized("sleep 20", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=15 * GB)
        self.host.tick()
        loop.cycle()
        try:
            self.assertNotEqual(self.status(r["data"]["id"]), "Stashed")
            p = self.hold("--gpu", "0", "--mem", "15G", "--name", "late")
            self.assertNotEqual(p.returncode, 0)  # the job got the room first
        finally:
            self.job("kill", "local", str(r["data"]["id"]))

    def test_an_enqueue_that_fails_after_pueue_acted_keeps_the_job_counted_and_recorded(self):
        loop = self.loop()
        r = self.submit_sized("sleep 2; echo ran-once", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        real = self.j._pueue

        def flaky(args, **kw):
            p = real(args, **kw)
            if args[:1] == ["enqueue"]:
                raise self.j.JobError("pueue enqueue did not answer within 60 s")
            return p
        self.j._pueue = flaky
        try:
            self.host.tick()
            loop.cycle()
        finally:
            self.j._pueue = real
        self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=60))
        self.assertEqual(self.record(tok)["how"], "exit")
        self.assertIn("ran-once", self.wait(tid, tok).stdout)

    def test_a_held_task_started_by_hand_is_counted_and_recorded(self):
        self.host.set(avail=2 * GB)  # 1 GB room: the job does not fit
        loop = self.loop()
        r = self.submit_sized("sleep 1", cpus=1, ram=int(1.5 * GB))
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.host.tick()
        loop.cycle()
        self.d.pueue("enqueue", str(tid))  # a person releases it
        self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=60))
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue", tok + ".json")))

    def test_a_retry_after_pueue_add_failed_late_runs_the_job(self):
        tok = "ab" * 16
        req = {"protocol": 5, "command": "echo ran-after-retry", "task": "T-adm", "cwd": self.d.dir, "session": "t",
               "from": "t", "env": {}, "token": tok, "admission": {"cpus": 1, "ram": 100 * MiB}}
        real = self.j._pueue

        def late(args, **kw):
            p = real(args, **kw)
            if args[:1] == ["add"]:
                raise self.j.JobError("pueue add did not answer within 60 s")  # it did add the task
            return p
        self.j._pueue = late
        try:
            with self.assertRaises(self.j.JobError):
                self.j.host_submit(req)
        finally:
            self.j._pueue = real
        r = self.j.host_submit(req)  # the client's retry, same token
        self.assertFalse(r.get("existing"))
        loop = self.loop()
        self.assertTrue(self.cycles(loop, lambda: self.status(r["id"]) == "Done"))
        self.assertIn("ran-after-retry", self.wait(r["id"], tok).stdout)
        stashed = [t for t in json.loads(self.d.pueue("status", "--json").stdout)["tasks"].values()
                   if (t.get("envs") or {}).get("JOB_TOKEN") == tok and "Stashed" in t["status"]]
        self.assertEqual(stashed, [])

    def test_a_hold_on_a_cgroup_ends_when_the_cgroup_goes(self):
        import uuid as _uuid
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())
        unit = "agent-pueue-probe-%s.scope" % _uuid.uuid4().hex[:12]
        svc = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit, "sleep", "30"],
                               env=env)
        try:
            for _ in range(50):
                cg = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", unit], env=env,
                                    capture_output=True, text=True).stdout.strip()
                if cg:
                    break
                time.sleep(0.1)
            self.model_card()
            loop = self.loop()
            self.host.tick()
            loop.cycle()
            p = self.hold("--gpu", "0", "--mem", "2G", "--name", "svc", "--cgroup", cg)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.host.tick()
            loop.cycle()
            self.assertEqual(len(loop.holds), 1)
            svc.kill()
            svc.wait()
            subprocess.run(["systemctl", "--user", "stop", unit], env=env, capture_output=True)
            self.assertTrue(self.cycles(loop, lambda: loop.holds == [], n=20))
        finally:
            if svc.poll() is None:
                svc.kill()
            subprocess.run(["systemctl", "--user", "reset-failed", unit], env=env, capture_output=True)

    def test_admission_status(self):
        p = self.job("admission", "local", "status", **self.env())
        self.assertIn("settings valid", p.stdout)
        self.assertIn("admission loop service", p.stdout)

    def test_invalid_settings_refuse_every_submit(self):
        with open(self.cfg, "w") as f:
            f.write('{"ram_headroom": 1}')
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        self.assertFalse(r["ok"])
        self.assertIn("unknown setting ram_headroom", r["error"])


if __name__ == "__main__":
    unittest.main()
