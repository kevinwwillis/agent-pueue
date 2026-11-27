"""Tests of admission (docs/admission.md): settings, room rules, and the loop releasing real jobs.

The room rules are tested as plain functions on made-up samples. The loop tests submit real jobs to a
throwaway pueued (see test_job.Daemon) and run the loop's cycles in this process against a fake /proc and a
fake GPU list, while jobs run in real systemd user scopes (skipped where those don't work).

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import fcntl
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_job import JOB, Base, own_tmpdir, remove_own_dir
from test_scope import SCOPES

GB, MiB, GiB = 10 ** 9, 1 << 20, 1 << 30
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
        cls.j = load()
        cls.tmp = own_tmpdir()
        cls.addClassCleanup(remove_own_dir, cls.tmp)
        cls.s = settings(cls.j, cls.tmp)

    def room(self, samples, inflight=(), now=None):
        return self.j.compute_room(self.s, samples, list(inflight), [], now or samples[-1]["t"])

    def test_ram_fit_and_no_fit(self):
        r = self.room([sample(100, avail=11 * GB)])  # 10 GB of room; a job counts at its limit, 1.5 times its size
        ok, got = self.j.fits(self.s, r, held(ram=6 * GB))
        self.assertTrue(ok)
        self.assertEqual(got["ram_counted"], self.j.ram_limit(self.s, 6 * GB))
        self.assertAlmostEqual(got["ram_counted"], 9 * GB, delta=4096)
        ok, why = self.j.fits(self.s, r, held(ram=7 * GB))
        self.assertFalse(ok)
        self.assertIn("needs 10.5 GB RAM (its limit, 1.5 times its size), 10.0 GB free", why)

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
        # a job is counted at its limit: a size that fits with a limit that does not could never be released
        self.assertIn("needs 37.5 GB RAM (its limit, 1.5 times its size); this host has at most 31.0 GB",
                      self.j.fits_empty(self.s, cap, held(ram=25 * GB)))
        self.assertIsNone(self.j.fits_empty(self.s, cap, held(ram=20 * GB)))
        self.assertIsNone(self.j.fits_empty(self.s, cap, held(ram=25 * GB, pilot=True)))  # counted at the room

    def test_like_cuts_its_margin_so_the_limit_fits_the_host(self):
        class Adm:
            def db(self):
                return sqlite3.connect(":memory:")

            def find(self, con, ref):
                return {"token": "t" * 32, "how": "exit", "exit": 0, "peak_samples": 100, "peak_ram": 25 * GB,
                        "peak_gpu_mem": 0, "peak_cpu": 1.0, "gpus": "[]", "peak_disk_mb_s": 0.0}
        cap = {"mem_total": 32 * GB, "threads": 8, "gpus": []}
        size = self.j._size_from(self.s, Adm(), cap, {"admission": {"like": "t" * 32}})[0]
        limit = self.j.ram_limit(self.s, size["ram"])
        self.assertLessEqual(limit, 31 * GB)
        self.assertGreaterEqual(limit, 25 * GB)  # it still covers the peak
        self.assertIsNone(self.j.fits_empty(self.s, cap, held(ram=size["ram"])))


class RoomRulesCycle2(unittest.TestCase):
    """Room rules from the second review."""

    @classmethod
    def setUpClass(cls):
        cls.j = load()
        cls.tmp = own_tmpdir()
        cls.addClassCleanup(remove_own_dir, cls.tmp)
        cls.s = settings(cls.j, cls.tmp)

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
        self.j = load()
        self.tmp = own_tmpdir()
        self.addCleanup(remove_own_dir, self.tmp)

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
                           ('{"ram_hard_factor": 0.5}', "ram_hard_factor"), ('{"ram_hard_factor": 1e300}', "ram_hard_factor"),
                           ('{"disks": ["data"]}', "absolute"),
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
        tmp = own_tmpdir()
        self.addCleanup(remove_own_dir, tmp)
        self.snap = lambda **kw: dict({"admission": True, "stale": False, "room": room, "held": [], "paths": [],
                                       "settings": settings(self.j, tmp)}, **kw)
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
        d = own_tmpdir()
        self.addCleanup(remove_own_dir, d)
        with open(os.path.join(d, "ssh"), "w") as f:
            f.write("#!/bin/sh\ncat > /dev/null\n" + script + "\n")
        os.chmod(os.path.join(d, "ssh"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + ":" + old
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))

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
        tmp = own_tmpdir()
        self.addCleanup(remove_own_dir, tmp)
        s = settings(self.j, tmp)
        adm = self.j.Admission(s)
        cap = {"mem_total": 64 * GB, "threads": 8, "gpus": []}
        for a, part in (({"cpus": 0, "ram": GB}, "--cpus"), ({"cpus": 1, "ram": 0}, "--ram")):
            with self.assertRaises(self.j.JobError) as e:
                self.j._size_from(s, adm, cap, {"admission": a})
            self.assertIn(part, str(e.exception))
            self.assertNotIn("admission setting", str(e.exception))

    def test_job_use_measures_the_disk_rate(self):
        root = own_tmpdir()
        self.addCleanup(remove_own_dir, root)
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

    def test_a_ram_hold_on_a_host_that_predates_it_is_released_and_refused(self):
        import argparse
        calls = []

        def old_host(host, op, req, timeout=120):
            calls.append(req)
            if req.get("release"):
                return {"id": req["release"], "action": "released"}
            return {"id": "abc123", "action": "granted", "gpu": 0, "uuid": CARD}  # no "ram": it was ignored
        self.j.call = old_host
        a = argparse.Namespace(host="h1", renew=None, name="svc", gpu="0", mem=GB, ram=GB, while_pid=None, unit=None,
                               cgroup=None, ttl=None)
        with self.assertRaises(self.j.JobError) as e:
            self.j.cmd_hold(a)
        self.assertIn("predates RAM holds", str(e.exception))
        self.assertEqual(calls[-1], {"release": "abc123"})

    def test_history_size_text(self):
        hs = self.j._history_size
        pilot = {"cpus": 1, "ram": 4 * GB, "gpus": 2, "gpu_mem": 0}
        self.assertEqual(hs({"pilot": 0, "size": {"cpus": 2, "ram": 8 * GB, "gpus": 1, "gpu_mem": 3 * GB}}),
                         "2 CPU, 8.0 GB, 1 GPU 3.0 GB")
        self.assertEqual(hs({"pilot": 1, "size": pilot}), "1 CPU, 2 GPU, to be counted at the free room at its release")
        released = {"pilot": 1, "size": pilot, "released": 1.0, "ram_counted": 50 * GB, "memory_max": 50 * GB,
                    "gpu_counted": {CARD: 10 * GB, CARD2: 20 * GB}}
        self.assertEqual(hs(released), "1 CPU, 50.0 GB, 2 GPUs with 10.0 GB and 20.0 GB, the free room at its release")
        self.assertEqual(hs(dict(released, gpu_counted={CARD: 10 * GB})),
                         "1 CPU, 50.0 GB, 1 GPU with 10.0 GB, the free room at its release")
        self.assertEqual(hs(dict(released, gpu_counted=None)), "1 CPU, 50.0 GB, 2 GPU, the free room at its release")
        by_hand = {"pilot": 1, "size": pilot, "released": 1.0, "ram_counted": 4 * GB, "memory_max": None,
                   "gpu_counted": {}}
        self.assertEqual(hs(by_hand), "1 CPU, 2 GPU, not released by the loop")
        self.assertEqual(hs(dict(by_hand, pilot=0, size={"cpus": 1, "ram": 4 * GB, "gpus": 0, "gpu_mem": 0})),
                         "1 CPU, 4.0 GB, not released by the loop")
        old = {"pilot": 1, "size": pilot, "released": 1.0, "started": 2.0}  # a record from before these fields
        self.assertEqual(hs(old), "1 CPU, 2 GPU, room counted not recorded")
        self.assertEqual(hs({"pilot": 1, "size": pilot, "how": "removed"}), "1 CPU, 2 GPU, never released")
        self.assertEqual(hs({"pilot": 0, "size": ["not", "a", "size"]}), "0 CPU, 0.0 GB")


class HoldMeasure(unittest.TestCase):
    """A hold's own use and the GPU process list, from a fake /proc, cgroup tree and driver output."""

    def setUp(self):
        self.j = load()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.proc, self.cg = os.path.join(self.root, "proc"), os.path.join(self.root, "cgroup")
        os.makedirs(self.proc)
        os.makedirs(self.cg)
        self.m = self.j.Measure(proc=self.proc, cgroup_root=self.cg, gpu_file=os.path.join(self.root, "none.json"))

    def process(self, pid, cgroup, pss=None, rss=None):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write("0::%s\n" % cgroup)
        if pss is not None:
            with open(os.path.join(d, "smaps_rollup"), "w") as f:
                f.write("Rss: %d kB\nPss_Anon: %d kB\nPss_File: 99 kB\nPss_Shmem: 0 kB\n" % (pss // 512, pss // 1024))
        if rss is not None:
            with open(os.path.join(d, "status"), "w") as f:
                f.write("Name:\tsvc\nRssAnon:\t%d kB\nRssFile:\t99 kB\nRssShmem:\t0 kB\n" % (rss // 1024))

    def cgroup(self, path, use):
        d = os.path.join(self.cg, path.lstrip("/"))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "memory.current"), "w") as f:
            f.write("%d\n" % use)
        with open(os.path.join(d, "memory.stat"), "w") as f:
            f.write("anon %d\nactive_file 0\ninactive_file 0\n" % use)

    def own(self, holds):
        loop = self.j.Loop.__new__(self.j.Loop)
        loop.m = self.m
        return loop.hold_ram(holds, {h["id"]: [h["cgroup"]] for h in holds if h.get("cgroup")},
                             {h["id"]: set(h["pids"]) for h in holds if h.get("pids")})

    def test_pss_is_summed_when_every_process_can_be_read(self):
        self.process(11, "/a", pss=2 * GiB, rss=5 * GiB)
        self.process(12, "/a", pss=1 * GiB, rss=5 * GiB)
        self.assertEqual(self.m.pids_ram([11, 12]), 3 * GiB)

    def test_a_process_that_cannot_be_read_gives_a_lower_bound(self):
        self.process(11, "/a", pss=1 * GiB, rss=4 * GiB)
        self.process(12, "/a", rss=3 * GiB)  # another user's process: no smaps_rollup
        self.assertEqual(self.m.pids_ram([11, 12]), 4 * GiB)  # the biggest single process, not 4 + 3
        self.process(13, "/a", pss=6 * GiB, rss=6 * GiB)
        self.assertEqual(self.m.pids_ram([11, 12, 13]), 7 * GiB)  # the readable ones' sum, 1 + 6, is larger

    def test_two_holds_on_one_cgroup_count_it_once(self):
        self.cgroup("/system.slice/model.service", 30 * GiB)
        own = self.own([{"id": "a", "start": 1, "ram": 1, "cgroup": "/system.slice/model.service"},
                        {"id": "b", "start": 2, "ram": 1, "cgroup": "/system.slice/model.service"}])
        self.assertEqual(own, {"a": 30 * GiB, "b": 0})

    def test_nested_cgroups_count_once_in_either_order(self):
        self.cgroup("/s/model.service", 30 * GiB)
        self.cgroup("/s/model.service/worker", 10 * GiB)
        outer = {"id": "o", "ram": 1, "cgroup": "/s/model.service"}
        inner = {"id": "i", "ram": 1, "cgroup": "/s/model.service/worker"}
        self.assertEqual(self.own([dict(outer, start=1), dict(inner, start=2)]), {"o": 30 * GiB, "i": 0})
        self.assertEqual(self.own([dict(outer, start=2), dict(inner, start=1)]), {"o": 20 * GiB, "i": 10 * GiB})

    def test_a_process_hold_and_a_cgroup_hold_on_it_count_it_once(self):
        self.cgroup("/s/model.service", 30 * GiB)
        self.process(11, "/s/model.service", pss=8 * GiB)
        cg = {"id": "c", "ram": 1, "cgroup": "/s/model.service"}
        pid = {"id": "p", "ram": 1, "pids": [11]}
        self.assertEqual(self.own([dict(cg, start=1), dict(pid, start=2)]), {"c": 30 * GiB, "p": 0})
        self.assertEqual(self.own([dict(cg, start=2), dict(pid, start=1)]), {"c": 22 * GiB, "p": 8 * GiB})
        self.assertEqual(self.own([dict(pid, start=1), dict(pid, id="q", start=2)]), {"p": 8 * GiB, "q": 0})

    def test_a_gpu_process_without_its_memory_is_still_on_the_card(self):
        class Done:
            def __init__(self, rc, out):
                self.returncode, self.stdout = rc, out
        apps = {"rc": 0}

        def run(args, **kw):
            if args[1].startswith("--query-gpu"):
                return Done(0, "0, %s, RTX 2070, 8192, 4096, 10\n" % CARD)
            return Done(apps["rc"], "%s, 11, [N/A]\n%s, 12, 300\n" % (CARD, CARD) if apps["rc"] == 0 else "")
        real = self.j.subprocess.run
        self.j.subprocess.run = run
        try:
            c = self.j._nvidia_smi_cards()[0]
            self.assertEqual(c["procs"], {11: 0, 12: 300 << 20})
            self.assertTrue(c.get("procs_ok", True))
            apps["rc"] = 9  # the process query failed
            c = self.j._nvidia_smi_cards()[0]
            self.assertEqual((c["procs"], c["procs_ok"]), ({}, False))
        finally:
            self.j.subprocess.run = real

    def test_a_unit_lookup_that_times_out_is_unknown_not_gone(self):
        def run(args, **kw):
            raise subprocess.TimeoutExpired(args, 5)
        real = self.j.subprocess.run
        self.j.subprocess.run = run
        try:
            self.assertIsNone(self.j._hold_cgroups({"unit": "model.service"}))
        finally:
            self.j.subprocess.run = real

    def test_nvml_keeps_a_process_whose_memory_is_not_available(self):
        n = self.j._Nvml.__new__(self.j._Nvml)

        def fn(h, count, arr):
            arr[0].pid, arr[0].usedGpuMemory = 11, 5 << 20
            arr[1].pid, arr[1].usedGpuMemory = 12, (1 << 64) - 1  # NVML_VALUE_NOT_AVAILABLE
            count._obj.value = 2
            return 0
        self.assertEqual(n._procs(fn, None), {11: 5 << 20, 12: 0})


class RecordsFile(unittest.TestCase):
    """The records database, opened by the loop and by host operations at the same time."""

    def test_two_processes_upgrading_an_old_records_file_both_succeed(self):
        j = load()
        old = [f for f in j.RECORD_FIELDS if f not in ("ram_counted", "gpu_counted", "memory_max")]
        opener = ("import importlib.machinery, importlib.util, os, sys\n"
                  "loader = importlib.machinery.SourceFileLoader('jobmod', sys.argv[1])\n"
                  "spec = importlib.util.spec_from_loader('jobmod', loader)\n"
                  "m = importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
                  "a = m.Admission.__new__(m.Admission); a.path = lambda *p: os.path.join(sys.argv[2], *p)\n"
                  "open(os.path.join(sys.argv[2], 'ready-%d' % os.getpid()), 'w').close()\n"
                  "while not os.path.exists(os.path.join(sys.argv[2], 'go')): pass\n"
                  "a.db().close()\n")
        for _ in range(8):
            d = tempfile.mkdtemp()
            try:
                c = sqlite3.connect(os.path.join(d, "admission.sqlite"))
                c.execute("CREATE TABLE jobs (%s, PRIMARY KEY (token))" % ", ".join(old))
                c.execute("CREATE TABLE holds (id PRIMARY KEY, name, card, size, start, ended, how)")  # before ram
                c.commit()
                c.close()
                ps = [subprocess.Popen([sys.executable, "-c", opener, JOB, d], stderr=subprocess.PIPE, text=True)
                      for _ in range(2)]
                deadline = time.monotonic() + 60
                while len([f for f in os.listdir(d) if f.startswith("ready-")]) < 2:  # both loaded and waiting
                    self.assertLess(time.monotonic(), deadline, "the openers did not start")
                    time.sleep(0.01)
                open(os.path.join(d, "go"), "w").close()
                for p in ps:
                    _, err = p.communicate(timeout=60)
                    self.assertEqual(p.returncode, 0, err)
                c = sqlite3.connect(os.path.join(d, "admission.sqlite"))
                have = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
                c.close()
                self.assertEqual(have, set(j.RECORD_FIELDS))
                c = sqlite3.connect(os.path.join(d, "admission.sqlite"))
                have = {r[1] for r in c.execute("PRAGMA table_info(holds)")}
                c.close()
                self.assertEqual(have, set(j.HOLD_FIELDS))
            finally:
                shutil.rmtree(d)


class ResizeRules(unittest.TestCase):
    """TASK-007.01: the oversize ALERT's suggestion, the resize floor and how a resize shows (no daemon)."""

    @classmethod
    def setUpClass(cls):
        cls.j = load()
        cls.tmp = own_tmpdir()
        cls.addClassCleanup(remove_own_dir, cls.tmp)
        cls.s = settings(cls.j, cls.tmp, ram_hard_factor=1.25)

    def test_new_settings_have_defaults_and_are_checked(self):
        self.assertEqual((self.s["oversize_after_min"], self.s["oversize_fraction"], self.s["resize_margin"]),
                         (30, 0.5, 1.25))
        for bad in ({"oversize_fraction": 0}, {"oversize_fraction": 1.5}, {"resize_margin": 0.9},
                    {"oversize_after_min": 0.5}):
            with self.assertRaises(self.j.JobError):
                settings(self.j, self.tmp, **bad)

    def test_the_suggestion_is_the_like_size_and_covers_the_floor(self):
        # the audit's job: 64 GB asked, 26.5 GB used; --like would give 26.5 x 1.25 = 33.1 GB
        got = self.j._resize_suggestion(self.s, 26.5 * GB, 20 * GB)
        self.assertEqual(self.j._size_arg(got), "33.2G")
        self.assertGreaterEqual(self.j.ram_limit(self.s, self.j._parse_size("33.2G")),
                                self.j._resize_floor(self.s, 26.5 * GB, 20 * GB))
        # a job using more now than its peak times the margin is never offered a limit below its use
        self.assertGreaterEqual(self.j.ram_limit(self.s, self.j._resize_suggestion(self.s, 10 * GB, 40 * GB)), 40 * GB)

    def test_the_exit_137_hint_is_only_for_a_size_that_was_not_measured(self):
        r = self.j.compute_room(self.s, [sample(100, avail=5 * GB)], [], [], 100)
        ok, why = self.j.fits(self.s, r, held(ram=8 * GB))
        self.assertFalse(ok)
        self.assertNotIn("exit 137", why)  # the reason itself, which younger jobs embed, has no hint
        lack = self.j._lack(self.s, r, held(ram=8 * GB))
        hint = self.j._ram_hint(held(ram=8 * GB, size_source="explicit"), lack)
        self.assertIn("ends in exit 137", hint)
        self.assertIn("--pilot, then --like", hint)
        self.assertIsNone(self.j._ram_hint(held(ram=8 * GB, size_source="like"), lack))  # sized from a measured run
        self.assertIsNone(self.j._ram_hint(held(ram=8 * GB, size_source="explicit"), {"cpu": 1.0}))  # not short of RAM
        self.assertIsNone(self.j._ram_hint(held(pilot=True), {"ram": GB}))

    def test_a_pilot_needs_its_ram_floor(self):
        self.assertEqual(self.s["pilot"]["ram_floor_gb"], 16)
        r = self.j.compute_room(self.s, [sample(100, avail=11 * GB)], [], [], 100)  # 10 GB of room
        ok, why = self.j.fits(self.s, r, held(pilot=True))
        self.assertFalse(ok)
        self.assertIn("a pilot needs at least 16.0 GB of RAM room (pilot.ram_floor_gb), 10.0 GB free", why)
        r = self.j.compute_room(self.s, [sample(100, avail=17 * GB)], [], [], 100)
        self.assertTrue(self.j.fits(self.s, r, held(pilot=True))[0])
        cap = {"mem_total": 12 * GB, "threads": 8, "gpus": []}
        self.assertIn("a pilot needs at least 16.0 GB", self.j.fits_empty(self.s, cap, held(pilot=True)))

    def test_a_pilot_stopped_at_its_time_limit_has_its_own_verdict(self):
        v = self.j.scope_verdict("KILLED", 137, {"how": "killed", "stopped_by": "pilot time limit"})
        self.assertEqual(v[:2], ("PILOT_TIME_LIMIT", 207))
        self.assertNotIn(207, [c for k, c in self.j.EXIT.items() if k != "PILOT_TIME_LIMIT"])
        self.assertEqual(self.j.scope_verdict("KILLED", 137, {"how": "killed", "stopped_by": "stop-at"})[:2],
                         ("KILLED", 137))

    def test_a_start_estimate_from_projected_ends(self):
        est = self.j.start_estimate
        running = [(2000.0, {"ram": 3 * GB, "cpu": 2}, "T-b"), (1000.0, {"ram": 3 * GB, "cpu": 1}, "T-a")]
        self.assertEqual(est({"ram": 2 * GB}, running), ((1000.0, "T-a"), None))
        self.assertEqual(est({"ram": 5 * GB, "cpu": 1}, running), ((2000.0, "T-b"), None))
        got, why = est({"ram": 5 * GB}, running + [(None, {"ram": GB}, "T-c")])
        self.assertEqual(got, (2000.0, "T-b"))  # enough frees before the job without a projected end matters
        got, why = est({"ram": 7 * GB}, running + [(None, {"ram": GB}, "T-c")])
        self.assertIsNone(got)
        self.assertIn("no projected end", why)
        self.assertIn("not held by running jobs", est({"ram": 10 * GB}, running)[1])
        self.assertIn("busy disk", est({"disk:sda": 0.0}, running)[1])
        gpu = [(500.0, {"gpu:a": 4 * GB}, "T-a"), (300.0, {"gpu:b": 4 * GB}, "T-b")]
        self.assertEqual(est({"gpu:a": 2 * GB, "gpu:b": GB}, gpu, 1, 0)[0], (300.0, "T-b"))  # either card will do
        self.assertEqual(est({"gpu:a": 2 * GB, "gpu:b": GB}, gpu, 2, 0)[0], (500.0, "T-a"))  # it needs both

    def test_a_busy_gpu_does_not_hide_an_estimate_for_another_card(self):
        est = self.j.start_estimate
        gpu = [(300.0, {"gpu:b": 4 * GB}, "T-b")]
        self.assertEqual(est({"gpu:a": 0.0, "gpu:b": GB}, gpu, 1, 0)[0], (300.0, "T-b"))
        got, why = est({"gpu:a": 0.0, "gpu:b": GB}, gpu, 2, 0)
        self.assertIsNone(got)
        self.assertIn("1 of the GPUs it could use is busy", why)

    def test_a_pilot_short_only_of_cpu_gets_a_cpu_estimate(self):
        r = self.j.compute_room(self.s, [sample(100, avail=40 * GB, busy=7.5)], [], [], 100)
        lack = self.j._lack(self.s, r, held(cpus=2, pilot=True))
        self.assertEqual(set(lack), {"cpu"})  # it has its RAM floor, so it lacks no RAM
        got, why = self.j.start_estimate(lack, [(900.0, {"ram": GB, "cpu": 2}, "T-a")])
        self.assertEqual(got, (900.0, "T-a"), why)

    def test_a_pilot_lacks_the_ram_it_is_short_of_its_floor(self):
        r = self.j.compute_room(self.s, [sample(100, avail=11 * GB)], [], [], 100)  # 10 GB of room, floor 16 GB
        self.assertAlmostEqual(self.j._lack(self.s, r, held(pilot=True))["ram"], 6 * GB, delta=1e6)

    def test_an_estimate_past_its_projected_end_says_any_time_now(self):
        r = self.j.compute_room(self.s, [sample(100, avail=5 * GB)], [], [], 100)
        q = held(ram=8 * GB)
        evil = "T-\x1b[31mred\x07" + "x" * 200
        text = self.j.Loop._estimate_text(None, q, r, {"ram": GB}, [(1000.0, {"ram": 2 * GB}, evil)], 5000.0)
        self.assertIn("past its projected end", text)
        self.assertIn("any time now", text)
        self.assertNotIn("should free about", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("T-[31mred" + "x" * 48 + "...", text)  # cut to 60 characters
        text = self.j.Loop._estimate_text(None, q, r, {"ram": GB}, [(9000.0, {"ram": 2 * GB}, "T-a")], 5000.0)
        self.assertIn("should free about", text)

    def test_the_suggestion_always_parses_to_an_accepted_limit(self):
        # a floor of 5.125 GB wants 4.1 GB at 1.25x, and "4.1G" parses to 4099999999 bytes, one under
        for kernel_peak in range(10 ** 9, 6 * 10 ** 9, 7777777):
            n = self.j._resize_suggestion(self.s, GB, 0, kernel_peak)
            self.assertGreaterEqual(self.j.ram_limit(self.s, self.j._parse_size(self.j._size_arg(n))),
                                    self.j._resize_floor(self.s, GB, 0, kernel_peak), kernel_peak)
        self.assertGreaterEqual(self.j._resize_floor(self.s, GB, 0, 5 * GB), 5 * GB)  # memory.peak counts

    def test_a_held_resize_restarts_the_fairness_measure(self):
        adm = self.j.Admission(self.s)
        q = {"token": "c" * 32, "size": {"cpus": 1, "ram": 8 * GB}, "fair_base": {"ram": GB}, "fair_base_at": 100.0,
             "fair_passed": {"d" * 32: {"ram": 1}}, "state": "held"}
        adm.write_json(adm.resize_path(q["token"]), {"size": {"cpus": 1, "ram": 2 * GB},
                                                     "resized": [{"t": 50.0}]})
        self.assertEqual(adm.overlay(dict(q))["fair_base"], {"ram": GB})  # a resize older than the measure
        adm.write_json(adm.resize_path(q["token"]), {"size": {"cpus": 1, "ram": 2 * GB},
                                                     "resized": [{"t": 50.0}, {"t": 200.0}]})
        got = adm.overlay(dict(q))
        self.assertNotIn("fair_base", got)
        self.assertNotIn("fair_passed", got)
        os.remove(adm.resize_path(q["token"]))

    def test_soft_limits_in_a_resize_file_do_not_grow_the_record(self):
        # TASK-021: a resize file that carried the loop's soft limits doubled them on every read of the queue file
        adm = self.j.Admission(self.s)
        soft = {"t": 10.0, "state": "released", "auto": "shrink", "soft_limit": {"from": 5 * GB, "to": GB}}
        grow = {"t": 20.0, "state": "released", "auto": "grow", "from": {"cpus": 1, "ram": GB}, "to": {"cpus": 1, "ram": 2 * GB}}
        q = {"token": "e" * 32, "size": {"cpus": 1, "ram": GB}, "state": "released", "resized": [soft, soft, soft]}
        adm.write_json(adm.resize_path(q["token"]), {"size": {"cpus": 1, "ram": 2 * GB}, "resized": [soft, grow]})
        for _ in range(5):
            q = adm.overlay(dict(q))
        self.assertEqual(q["resized"], [soft, grow])
        self.assertEqual(q["size"]["ram"], 2 * GB)
        self.assertEqual(self.j._no_soft([soft, grow]), [grow])
        os.remove(adm.resize_path(q["token"]))
        con = adm.db()
        adm.record(con, {"token": q["token"], "task": "T-021", "resized": [soft] * 4 + [grow]})
        con.commit()
        got = json.loads(con.execute("SELECT resized FROM jobs WHERE token = ?", (q["token"],)).fetchone()[0])
        con.close()
        self.assertEqual(got, [soft, grow])  # a grown record shrinks back when it is written

    def test_like_last_prefers_the_newest_run_that_exited_0(self):
        adm = self.j.Admission(self.s)
        cap = {"mem_total": 64 * GB, "threads": 8, "gpus": []}
        con = adm.db()
        rows = [("1" * 32, "T-last", "exit", 0, 1000.0, 2 * GB), ("2" * 32, "T-last", "pilot time limit", None, 2000.0,
                                                                   GB // 2)]
        for tok, task, how, ex, ended, peak in rows:
            con.execute("INSERT INTO jobs (token, task, how, exit, ended, peak_ram, peak_cpu, peak_samples, gpus) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1.0, 10, '[]')", (tok, task, how, ex, ended, peak))
        con.execute("INSERT INTO jobs (token, task, how, exit, ended, peak_ram, peak_cpu, peak_samples, gpus) "
                    "VALUES (?, 'T-pilot-only', 'pilot time limit', NULL, 3000.0, ?, 1.0, 10, '[]')", ("3" * 32, GB))
        con.commit()
        con.close()
        size, source, _, like_from = self.j._size_from(self.s, adm, cap, {"task": "T-last", "admission": {"like": "last"}})
        self.assertEqual(like_from, "1" * 32)  # the older complete run, not the newer pilot
        self.assertEqual(size["ram"], int(2 * GB * 1.25))
        like_from = self.j._size_from(self.s, adm, cap, {"task": "T-pilot-only", "admission": {"like": "last"}})[3]
        self.assertEqual(like_from, "3" * 32)  # no run exited 0: the pilot
        self.assertIn("pilot stopped at its time limit", self.j._like_source_text(adm, "3" * 32))

    def test_what_a_held_job_lacks(self):
        r = self.j.compute_room(self.s, [sample(100, avail=5 * GB, busy=7.0, gpus={CARD: card(20 * GB)})], [], [], 100)
        lack = self.j._lack(self.s, r, held(cpus=3, ram=4 * GB, gpus=1, gpu_mem=6 * GB))
        self.assertAlmostEqual(lack["ram"], self.j.ram_limit(self.s, 4 * GB) - 4 * GB, delta=1e6)
        self.assertAlmostEqual(lack["cpu"], 2.0)
        self.assertAlmostEqual(lack["gpu:" + CARD], 6 * GB - 3.5 * GB, delta=1e6)
        self.assertEqual(self.j._lack(self.s, r, held(cpus=1, ram=GB)), {})
        self.assertEqual(settings(self.j, self.tmp)["fairness_small_fraction"], 0.1)

    def test_history_shows_the_size_it_ended_with_and_that_it_was_resized(self):
        j = {"pilot": 0, "size": {"cpus": 4, "ram": 32 * GB, "gpus": 0, "gpu_mem": 0},
             "resized": [{"t": 1.0, "state": "released", "from": {"cpus": 8, "ram": 64 * GB},
                          "to": {"cpus": 4, "ram": 32 * GB}}]}
        self.assertEqual(self.j._history_size(j), "4 CPU, 32.0 GB, resized from 8 CPU, 64.0 GB")

    def test_an_oversize_alert_gives_the_exact_command(self):
        w = self.j.Waiter("h1", 14, token="f" * 32)
        w.alert_cursor = 0
        alert = {"kind": "oversize", "message": "oversized: ...", "resize": {"ram": "33.2G"}}
        self.j.call = lambda host, op, req, timeout=120: {"found": True, "status": "Running", "token": "f" * 32,
                                                          "alert_count": 1, "alerts": [alert], "log_size": 0}
        o = w.step()
        self.assertEqual(o["verdict"], "ALERT")
        self.assertIn("job resize h1 14 --token %s --ram 33.2G" % ("f" * 32), o["note"])

    def test_resize_on_a_host_that_predates_it(self):
        import argparse

        def old_host(host, op, req, timeout=120):
            raise self.j.JobError("h1: no reply from job on the host (exit 2): usage: job ... invalid choice")
        self.j.call = old_host
        with self.assertRaises(self.j.JobError) as e:
            self.j.cmd_resize(argparse.Namespace(host="h1", id=3, token=None, ram=GB, cpus=None))
        self.assertIn("predates job resize", str(e.exception))


class FakeHost:
    """A fake /proc (stat, meminfo, pressure, vmstat, diskstats) and GPU list that a test can change between
    cycles."""

    def __init__(self, root, threads=8):
        self.root, self.threads = root, threads
        self.proc = os.path.join(root, "proc")
        os.makedirs(os.path.join(self.proc, "pressure"), exist_ok=True)
        self.gpu_file = os.path.join(root, "gpus.json")
        self.cgroups = os.path.join(root, "cgroup")
        os.makedirs(self.cgroups, exist_ok=True)
        self.ticks = 0
        self.fake_psi = {}  # {cgroup dir: [full10, full60, total]}: a stalling cgroup's stall time grows each tick
        self.set(avail=50 * GB, busy=0.0, cards=[], allocstall=0)

    def set(self, avail=None, busy=None, psi=None, cards=None, procs=None, allocstall=None):
        if avail is not None:
            with open(os.path.join(self.proc, "meminfo"), "w") as f:
                f.write("MemTotal: %d kB\nMemAvailable: %d kB\n" % (64 * GB // 1024, avail // 1024))
        if busy is not None:
            self.busy = busy
        if allocstall is not None:  # global direct reclaim so far: unchanged, the host is not in global reclaim
            with open(os.path.join(self.proc, "vmstat"), "w") as f:
                f.write("nr_free_pages 1000\nallocstall_normal 0\nallocstall_movable %d\n" % allocstall)
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
        """Advance the CPU counters by one second at the current busy level, and the stall time of each stalling
        fake cgroup."""
        for d, v in self.fake_psi.items():
            v[2] += int(v[0] * 10000)
            with open(os.path.join(d, "memory.pressure"), "w") as f:
                f.write("some avg10=%.2f avg60=%.2f avg300=0.00 total=%d\nfull avg10=%.2f avg60=%.2f avg300=0.00 "
                        "total=%d\n" % (v[0], v[1], v[2], v[0], v[1], v[2]))
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

    def loop(self, clock=time.time, psi_root=None):
        # memory pressure is read from a fake cgroup tree (empty unless a test fills it, so the fake /proc figure is
        # used), never this machine's: its real pressure would make the tests depend on what else runs here
        m = self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file, psi_root=psi_root or self.host.cgroups)
        return self.j.Loop(self.settings, measure=m, clock=clock)

    def submit_sized(self, command, protocol=5, group=None, **admission):
        req = {"protocol": protocol, "command": command, "task": "T-adm", "cwd": self.d.dir, "session": "test",
               "from": "test", "env": {}, "admission": admission}
        if group:
            req["group"] = group
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
        self.assertIn("needs 6.0 GB RAM (its limit, 1.5 times its size), 2.0 GB free", snap["held"][0]["reason"])
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
        w = self.wait(tid, tok)
        self.assertIn("stopped this pilot at its time limit, as planned", w.stdout)
        self.assertEqual(w.returncode, 207, w.stdout)  # its own verdict, not 137 (an out-of-memory kill)
        self.assertIn("job: PILOT_TIME_LIMIT local#%d" % tid, w.stdout)
        self.assertGreaterEqual(rec["peak_ram"], 60 * MiB)
        r2 = self.submit_sized("echo sized", like=tok)
        self.assertTrue(r2["ok"], r2)
        with open(os.path.join(self.settings["state_dir"], "queue", r2["data"]["token"] + ".json")) as f:
            q = json.load(f)
        self.assertEqual(q["size_source"], "like")
        self.assertGreaterEqual(q["size"]["ram"], 60 * MiB * 1.25)
        # --like last: the last good run of the same task id here, named with its peaks
        r3 = self.submit_sized("echo sized-last", like="last")
        self.assertTrue(r3["ok"], r3)
        self.assertIn("no run of T-adm here exited 0, so it is sized from the newest pilot that reached its time limit, #%d of session "
                      "test (pilot stopped at its time limit" % tid, r3["data"]["sized"])
        self.assertIn("peaks", r3["data"]["sized"])
        with open(os.path.join(self.settings["state_dir"], "queue", r3["data"]["token"] + ".json")) as f:
            self.assertEqual(json.load(f)["like_from"], tok)
        for r in (r2, r3):
            self.d.pueue("remove", str(r["data"]["id"]))

    def test_like_last_without_a_good_run_is_refused(self):
        r = self.submit_sized("true", like="last")
        self.assertFalse(r["ok"], r)
        self.assertIn("--like last: no run of T-adm", r["error"])

    def test_a_pilot_waits_for_its_ram_floor(self):
        self.write_settings(pilot={"max_minutes": 1, "ram_floor_gb": 8})
        self.host.set(avail=6 * GB)  # 5 GB of room, under the 8 GB floor
        loop = self.loop()
        r = self.submit_sized("echo pilot-ran", pilot=True)
        tid = r["data"]["id"]
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")
        with open(os.path.join(self.state, "room.json")) as f:
            self.assertIn("a pilot needs at least 8.0 GB of RAM room", json.load(f)["held"][0]["reason"])
        self.host.set(avail=10 * GB)
        loop.samples = []
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))

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
        self.assertIn("job resume", alerts[0]["message"])
        self.assertNotIn("pueue start", alerts[0]["message"])
        self.assertEqual(alerts[0]["resume"], {"group": "padm"})
        w = self.j.Waiter("local", r["data"]["id"], token=r["data"]["token"])
        w.alert_cursor = 0
        self.j.call = lambda h, op, req, **kw: {"found": True, "status": "Stashed", "id": r["data"]["id"],
                                                 "alert_count": 1, "alerts": alerts, "token": r["data"]["token"]}
        out = w.step()
        self.assertEqual(out["verdict"], "ALERT")
        self.assertIn("job resume local --group padm", out["note"])
        self.d.pueue("start", "--group", "padm")

    def test_older_job_short_of_room_blocks_younger_after_the_fairness_wait(self):
        now = [time.time()]
        self.host.set(avail=12 * GB)
        loop = self.loop(clock=lambda: now[0])
        first = self.submit_sized("sleep 60", cpus=1, ram=4 * GB)  # runs, and is counted at its 6 GB limit
        self.assertTrue(self.cycles(loop, lambda: self.status(first["data"]["id"]) == "Running"))
        big = self.submit_sized("echo big", cpus=1, ram=4 * GB)    # 5 GB free: short, but fits once first ends
        self.assertTrue(big["ok"], big)
        now[0] += 120  # past the one-minute fairness wait
        small = self.submit_sized("echo small", cpus=1, ram=2 * GB)  # fits (3 GB), but takes RAM the big one needs
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

    def test_a_job_whose_limit_could_never_fit_is_refused_at_submit(self):
        r = self.submit_sized("true", cpus=1, ram=50 * GB)  # 64 GB host, 1 GB headroom: its 75 GB limit never fits
        self.assertFalse(r["ok"], r)
        self.assertIn("needs 75.0 GB RAM (its limit, 1.5 times its size)", r["error"])
        r = self.submit_sized("true", cpus=1, ram=40 * GB)  # a 60 GB limit fits an empty host
        self.assertTrue(r["ok"], r)
        self.d.pueue("remove", str(r["data"]["id"]))

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
        self.assertIn("needs 6.0 GB RAM (its limit, 1.5 times its size), 2.0 GB free", room.stdout)
        ls = self.job("ls", "local", **self.env())
        row = [x for x in ls.stdout.splitlines() if x.split()[1:2] == [str(tid)]]
        self.assertTrue(row and "Held" in row[0] and "needs 6.0 GB RAM" in row[0], ls.stdout)
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--poll", "0.3"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.d.env(**self.env()))
        time.sleep(1.5)
        self.host.set(avail=8 * GB)
        loop.samples = []
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        out, err = w.communicate(timeout=60)
        self.assertEqual(w.returncode, 0, out + err)
        self.assertIn("held: needs 6.0 GB RAM", err)
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
        self.assertIn("its pueue group palert on local is paused; start it with: job resume local --group palert", out)

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

    def test_a_ram_hold_keeps_jobs_out_of_its_ram(self):
        loop = self.loop()  # 50 GB available, 1 GB headroom: 49 GB of room
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "40G", "--name", "model-server", "--cgroup", "/system.slice/model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        hid = p.stdout.strip()
        p = self.hold("--ram", "20G", "--name", "second")
        self.assertNotEqual(p.returncode, 0)  # granted since the snapshot: 9 GB left
        self.assertIn("RAM free to hold", p.stdout + p.stderr)
        r = self.submit_sized("echo ram-job", cpus=1, ram=15 * GB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")
        self.assertAlmostEqual(loop.room["ram"]["room"], 9 * GB, delta=MiB)
        room = self.job("room", "local", **self.env()).stdout
        self.assertIn("(40.0 GB held for services)", room)
        self.assertIn("hold %s (model-server): 40.0 GB of RAM" % hid, room)
        self.assertEqual(self.job("release", "local", hid, **self.env()).returncode, 0)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
        self.assertIn("ram-job", self.wait(tid, tok).stdout)

    def test_a_ram_holds_own_use_is_not_counted_twice(self):
        cg = os.path.join(self.host.root, "cgroup")
        svc = os.path.join(cg, "system.slice", "model.service")
        os.makedirs(svc)

        def service_uses(n):
            with open(os.path.join(svc, "memory.current"), "w") as f:
                f.write("%d\n" % (n + GB))  # 1 GB of it file cache, which does not count
            with open(os.path.join(svc, "memory.stat"), "w") as f:
                f.write("anon %d\nactive_file %d\ninactive_file 0\n" % (n, GB))
        service_uses(0)
        m = self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file, cgroup_root=cg)
        loop = self.j.Loop(self.settings, measure=m)
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "40G", "--name", "model", "--cgroup", "/system.slice/model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        service_uses(30 * GB)  # the model loads into its hold: 30 GB less available
        self.host.set(avail=20 * GB)
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertAlmostEqual(loop.room["ram"]["room"], (20 + 30 - 1 - 40) * GB, delta=2 * MiB)
        service_uses(45 * GB)  # past its hold: counted at its use
        self.host.set(avail=5 * GB)
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertAlmostEqual(loop.room["ram"]["room"], (5 + 45 - 1 - 45) * GB, delta=2 * MiB)

    def test_a_hold_alone_on_its_card_may_use_the_headroom(self):
        self.model_card()  # 24 GB, 0.4 GB driver, 0.5 GB headroom: 23.1 GB of room for jobs
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "23.5G", "--name", "alone")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.job("release", "local", p.stdout.strip(), **self.env()).returncode, 0)
        r = self.submit_sized("sleep 30", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=1 * GB)
        tid = r["data"]["id"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.host.tick()
            loop.cycle()
            p = self.hold("--gpu", "0", "--mem", "22.5G", "--name", "beside-a-job")  # 22.1 GB free with the headroom kept
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("GPU 0 on", p.stdout + p.stderr)
            self.assertIn("(counted at %s, its limit)" % self.j._gbs(self.j.ram_limit(self.settings, 100 * MiB)),
                          self.job("room", "local", **self.env()).stdout)
        finally:
            self.job("kill", "local", str(tid))

    def test_a_hold_without_room_exits_206_and_admission_unavailable_exits_205(self):
        self.model_card()
        p = self.hold("--gpu", "0", "--mem", "2G", "--name", "early")  # no snapshot yet: the loop has not run
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("WRAPPER_ERROR", p.stdout)
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        for args in (["--gpu", "0", "--mem", "30G"], ["--ram", "60G"]):
            p = self.hold("--name", "too-big", *args)
            self.assertEqual(p.returncode, 206, p.stdout + p.stderr)
            self.assertIn("NO_ROOM", p.stdout)

    def test_a_ram_only_hold_does_not_hide_its_services_gpu_use(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "20G", "--name", "model", "--cgroup", "/system.slice/model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.model_card(model_used=10 * GB)  # the service also loads 10 GB onto the card
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertAlmostEqual(loop.room["gpus"][CARD]["room"], 24 * GB - 0.5 * GB - 10.4 * GB, delta=1e6)

    def test_a_hold_beside_a_job_released_this_cycle_keeps_the_headroom(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        r = self.submit_sized("sleep 30", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=1 * GB)
        tid = r["data"]["id"]
        try:
            self.host.tick()
            loop.cycle()  # releases it; the snapshot is written in the same cycle
            self.assertNotEqual(self.status(tid), "Stashed")
            p = self.hold("--gpu", "0", "--mem", "22.5G", "--name", "beside-a-job")  # 22.1 GB with the headroom kept
            self.assertEqual(p.returncode, 206, p.stdout + p.stderr)
        finally:
            self.job("kill", "local", str(tid))

    def test_the_headroom_stays_for_another_hold_or_another_gpu_user(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "5G", "--name", "first")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.hold("--gpu", "0", "--mem", "18.5G", "--name", "second")  # 18.1 GB left with the headroom kept
        self.assertEqual(p.returncode, 206, p.stdout + p.stderr)
        self.model_card(model_used=2 * GB)  # a process no hold or job names uses the card
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        for h in list(loop.holds):
            self.job("release", "local", h["id"], **self.env())
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "21.3G", "--name", "beside-a-user")  # 21.1 GB with the headroom kept
        self.assertEqual(p.returncode, 206, p.stdout + p.stderr)

    def test_jobs_after_a_lone_hold_keep_the_headroom(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--gpu", "0", "--mem", "23.5G", "--name", "fills-the-card")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = self.submit_sized("echo late", cpus=1, ram=100 * MiB, gpus=1, gpu_mem=int(0.05 * GB))
        tid = r["data"]["id"]
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(tid), "Stashed")

    def test_a_ram_hold_on_a_process_ends_with_it(self):
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        svc = subprocess.Popen(["sleep", "60"])
        try:
            os.makedirs(os.path.join(self.host.proc, str(svc.pid)), exist_ok=True)
            with open(os.path.join(self.host.proc, str(svc.pid), "stat"), "w") as f:
                with open("/proc/%d/stat" % svc.pid) as real:
                    f.write(real.read())
            p = self.hold("--ram", "30G", "--name", "svc", "--while-pid", str(svc.pid))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(os.path.join(self.host.proc, str(svc.pid), "smaps_rollup"), "w") as f:
                f.write("Pss_Anon: %d kB\nPss_Shmem: 0 kB\n" % (10 * GB // 1024))  # the service loads 10 GB
            self.host.set(avail=40 * GB)
            self.host.tick()
            loop.cycle()
            self.assertEqual(loop.samples[-1]["hold_ram"], {p.stdout.strip(): 10 * GB // 1024 * 1024})
            self.assertAlmostEqual(loop.room["ram"]["room"], 19 * GB, delta=2 * MiB)  # 40 + its 10 - 1 - 30
            self.host.set(avail=50 * GB)
            os.remove(os.path.join(self.host.proc, str(svc.pid), "stat"))  # the process has exited
            for _ in range(2):
                self.host.tick()
                loop.cycle()
            self.assertEqual(loop.holds, [])
            self.assertAlmostEqual(loop.room["ram"]["room"], 49 * GB, delta=2 * MiB)
        finally:
            svc.kill()
            svc.wait()

    def fake_process(self, pid, start):
        d = os.path.join(self.host.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (svc) S 1 %d %d 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 %d 0 0\n" % (pid, pid, pid, start))
        return d

    def test_a_reload_right_after_its_process_exits_is_not_refused_against_its_own_dead_hold(self):
        loop = self.loop()
        self.host.set(avail=50 * GB)
        self.host.tick()
        loop.cycle()
        d = self.fake_process(6161, 100)
        first = self.hold("--ram", "30G", "--name", "svc", "--while-pid", "6161")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.host.tick()
        loop.cycle()  # the snapshot now counts the hold: 49 - 30 = 19 GB left
        self.assertAlmostEqual(loop.room["ram"]["room"], 19 * GB, delta=2 * MiB)
        os.remove(os.path.join(d, "stat"))  # the service exits and reloads at once, before the loop's next cycle
        self.fake_process(6162, 200)
        again = self.hold("--ram", "30G", "--name", "svc", "--while-pid", "6162")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        holds = os.listdir(os.path.join(self.state, "holds"))
        self.assertEqual(holds, [again.stdout.strip() + ".json"])  # the dead hold was ended, not left to the loop
        con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"))
        how = con.execute("SELECT how FROM holds WHERE id = ?", (first.stdout.strip(),)).fetchone()[0]
        con.close()
        self.assertEqual(how, "process 6161 exited")
        big = self.hold("--ram", "25G", "--name", "other")  # the live hold still counts: 19 GB is left
        self.assertEqual(big.returncode, 206, big.stdout + big.stderr)

    def test_a_reload_after_the_loop_itself_ended_the_dead_hold_is_not_refused(self):
        loop = self.loop()
        self.host.set(avail=50 * GB)
        self.host.tick()
        loop.cycle()
        d = self.fake_process(6181, 100)
        first = self.hold("--ram", "30G", "--name", "svc", "--while-pid", "6181")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.host.tick()
        loop.cycle()
        self.assertAlmostEqual(loop.room["ram"]["room"], 19 * GB, delta=2 * MiB)
        os.remove(os.path.join(d, "stat"))  # the service exits; the loop's next cycle ends its hold
        self.host.tick()
        loop.cycle()
        self.assertEqual(loop.holds, [])
        self.assertAlmostEqual(loop.room["ram"]["room"], 49 * GB, delta=2 * MiB)  # what it counted came back
        self.fake_process(6182, 200)
        again = self.hold("--ram", "30G", "--name", "svc", "--while-pid", "6182")  # the reload, right after
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_the_grant_asks_systemd_outside_the_queue_lock(self):
        with open(JOB) as f:
            src = f.read()
        body = src[src.index("def host_hold(req):"):src.index("def _gone_hold_cgroups(adm):")]
        self.assertLess(body.index("_gone_hold_cgroups(adm)"), body.index('adm.lock("queue.lock")'))
        dead = src[src.index("def _end_dead_holds("):src.index("def _holds(adm):")]
        self.assertNotIn("_hold_cgroups(", dead)

    def test_a_gpu_hold_whose_process_exited_is_credited_back_on_its_card(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        d = self.fake_process(6171, 100)
        first = self.hold("--gpu", "0", "--mem", "18G", "--name", "model", "--while-pid", "6171")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.host.tick()
        loop.cycle()
        refused = self.hold("--gpu", "0", "--mem", "18G", "--name", "model2")
        self.assertEqual(refused.returncode, 206, refused.stdout + refused.stderr)
        os.remove(os.path.join(d, "stat"))
        again = self.hold("--gpu", "0", "--mem", "18G", "--name", "model")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_a_hold_granted_during_a_cycle_counts_as_a_hold_on_its_card(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        orig, got = loop.sample, {}

        def sample_then_hold(*a, **k):
            out = orig(*a, **k)
            got["first"] = self.hold("--gpu", "0", "--mem", "5G", "--name", "first")
            return out
        loop.sample = sample_then_hold
        self.host.tick()
        loop.cycle()
        self.assertEqual(got["first"].returncode, 0, got["first"].stderr)
        p = self.hold("--gpu", "0", "--mem", "18.5G", "--name", "second")  # 18.1 GB left with the headroom kept
        self.assertEqual(p.returncode, 206, p.stdout + p.stderr)

    def test_two_ram_holds_on_one_service_count_its_use_once(self):
        cg = os.path.join(self.host.root, "cgroup")
        svc = os.path.join(cg, "system.slice", "model.service")
        os.makedirs(svc)

        def service_uses(n):
            with open(os.path.join(svc, "memory.current"), "w") as f:
                f.write("%d\n" % n)
            with open(os.path.join(svc, "memory.stat"), "w") as f:
                f.write("anon %d\nactive_file 0\ninactive_file 0\n" % n)
        service_uses(0)
        loop = self.j.Loop(self.settings, measure=self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file,
                                                                   cgroup_root=cg))
        self.host.tick()
        loop.cycle()
        for name in ("old", "again"):  # the service registers again before its old hold ends
            p = self.hold("--ram", "20G", "--name", name, "--cgroup", "/system.slice/model.service")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        service_uses(15 * GB)
        self.host.set(avail=35 * GB)
        loop.samples = []
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        self.assertAlmostEqual(loop.room["ram"]["room"], (35 + 15 - 1 - 40) * GB, delta=2 * MiB)

    def test_a_ram_hold_waits_for_a_loop_running_this_job(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        adm = self.j.Admission(self.settings)
        adm.write_json(adm.path("heartbeat"), dict(adm.read_json(adm.path("heartbeat")), version="0.5.0"))
        p = self.hold("--ram", "1G", "--name", "svc")
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)  # the old loop would not count it
        self.assertIn("runs job 0.5.0", p.stdout)
        p = self.hold("--gpu", "0", "--mem", "1G", "--name", "gpu-only")  # an old loop counts GPU holds
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "1G", "--name", "svc")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_snapshot_from_before_other_users_were_counted_gives_no_headroom(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        adm = self.j.Admission(self.settings)
        snap = adm.read_json(adm.path("room.json"))
        for g in snap["room"]["gpus"].values():
            g.pop("others")
        adm.write_json(adm.path("room.json"), snap)
        p = self.hold("--gpu", "0", "--mem", "23.5G", "--name", "alone")  # 23.1 GB with the headroom kept
        self.assertEqual(p.returncode, 206, p.stdout + p.stderr)

    def test_a_card_whose_processes_cannot_be_read_gives_no_headroom(self):
        used = int(0.4 * GB)
        self.host.set(cards=[{"index": 0, "uuid": CARD, "name": "RTX 3090", "total": 24 * GB, "free": 24 * GB - used,
                              "procs_ok": False}])
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        self.assertEqual(loop.room["gpus"][CARD]["others"], 1)
        p = self.hold("--gpu", "0", "--mem", "23.5G", "--name", "alone")
        self.assertEqual(p.returncode, 206, p.stdout + p.stderr)

    def test_a_job_released_before_limits_were_recorded_counts_at_the_limit_it_runs_under(self):
        loop = self.loop()
        q = {"token": "t" * 32, "released": time.time(), "size": {"cpus": 1.0, "ram": 4 * GB, "gpus": 0, "gpu_mem": 0},
             "gpus": [], "ram_counted": 4 * GB, "gpu_counted": {}, "memory_max": 6 * GB}
        self.assertEqual(loop._counted(q, time.time())["ram_counted"], 6 * GB)
        q.pop("memory_max")  # released by a loop that did not record it
        assign = loop.adm.assign_path(q["token"])
        with open(assign, "w") as f:
            f.write("JOB_MEMORY_MAX=%d\nCUDA_VISIBLE_DEVICES=\nJOB_GPU_UUIDS=\n" % (7 * GB))  # set at an older factor
        self.assertEqual(loop._counted(q, time.time())["ram_counted"], 7 * GB)
        os.remove(assign)
        self.assertEqual(loop._counted(q, time.time())["ram_counted"], self.j.ram_limit(self.settings, 4 * GB))
        q["pilot"] = True  # a pilot with no record of its limit: counted at what it was counted at
        self.assertEqual(loop._counted(q, time.time())["ram_counted"], 4 * GB)

    def test_a_unit_hold_whose_lookup_fails_is_kept(self):
        cg = os.path.join(self.host.root, "cgroup")
        os.makedirs(os.path.join(cg, "system.slice", "model.service"))
        loop = self.j.Loop(self.settings, measure=self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file,
                                                                   cgroup_root=cg))
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "4G", "--name", "svc", "--unit", "model.service")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        real, answer = self.j._hold_cgroups, {"cgs": ["/system.slice/model.service"]}
        self.j._hold_cgroups = lambda h: answer["cgs"]
        try:
            self.host.tick()
            loop.cycle()  # seen
            answer["cgs"] = None  # systemd could not be asked
            for _ in range(2):
                self.host.tick()
                loop.cycle()
            self.assertEqual([h["name"] for h in loop.holds], ["svc"])
            answer["cgs"] = []  # the unit has no cgroup: it is gone
            for _ in range(2):
                self.host.tick()
                loop.cycle()
            self.assertEqual(loop.holds, [])
        finally:
            self.j._hold_cgroups = real

    def test_the_loop_restarts_into_changed_settings(self):
        adm = self.j.Admission(self.settings)

        def ram_room():
            snap = adm.read_json(adm.path("room.json")) or {}
            return ((snap.get("room") or {}).get("ram") or {}).get("room")

        def until(ok, what):
            deadline = time.monotonic() + 60
            while not ok():
                self.assertLess(time.monotonic(), deadline, what)
                self.host.tick()
                time.sleep(0.2)
        p = subprocess.Popen([sys.executable, JOB, "_admit"], env=self.d.env(**self.env()), stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
        try:
            until(lambda: ram_room() is not None and abs(ram_room() - 49 * GB) < 10 * MiB, "no room with 1 GB headroom")
            self.write_settings(ram_headroom_gb=11)
            until(lambda: abs(ram_room() - 39 * GB) < 10 * MiB, "the loop did not take up the new headroom")
            with open(self.cfg, "w") as f:
                f.write("{ not json")
            beat = adm.read_json(adm.path("heartbeat"))["t"]
            until(lambda: adm.read_json(adm.path("heartbeat"))["t"] > beat + 1, "the loop stopped on bad settings")
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            _, err = p.communicate(timeout=30)
        self.assertIn("the changed settings are not valid", err)

    def test_a_hold_cgroup_must_be_absolute(self):
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.hold("--ram", "1G", "--name", "svc", "--cgroup", "system.slice/model.service")
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("starting with /", p.stdout)

    def test_hold_options_need_gpu_with_mem_or_ram(self):
        self.model_card()
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        for args in (["--name", "x"], ["--name", "x", "--gpu", "0"], ["--name", "x", "--mem", "1G"], ["--ram", "1G"]):
            p = self.hold(*args)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertIn("needs --name", p.stdout + p.stderr)
        p = self.hold("--name", "both", "--gpu", "0", "--mem", "2G", "--ram", "2G")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("GPU 0 RAM 2.0 GB", p.stderr)

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
        held = self.submit_sized("true", cpus=1, ram=40 * GB, stop_at=time.time() + 1)  # a 60 GB limit: fits when empty
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
        a = self.submit_sized("sleep 20", cpus=1, ram=3 * GB)  # counted at its 4.5 GB limit
        self.host.tick()
        loop.cycle()
        self.assertNotEqual(self.status(a["data"]["id"]), "Stashed")
        try:
            with open(os.path.join(self.state, "room.json")) as f:
                self.assertLess(json.load(f)["room"]["ram"]["room"], 0.6 * GB)
            b = self.submit_sized("true", cpus=1, ram=3 * GB, fit_now=True)
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
        self.assertEqual(loop._counted(q, 1000.0 + 599)["ram_counted"], self.j.ram_limit(self.settings, 8 * GB))  # its limit
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
        rec = self.record(tok)
        self.assertEqual(rec["ram_counted"], self.j.ram_limit(self.settings, 100 * MiB))  # a job counts at its limit
        self.assertEqual(rec["memory_max"], self.j._whole_pages(max(int(100 * MiB * 1.5), self.j.SCOPE_MIN_MEMORY)))
        p = self.job("history", "local", "--json", "--task", "nothing-like-this", **self.env())
        self.assertEqual(json.loads(p.stdout)["local"]["jobs"], [])

    def test_the_first_cycle_after_a_start_has_disk_readings(self):
        # without them a disk-heavy job could be released past a busy disk in the first cycle after a restart
        self.write_settings(disks=["/"])
        with open(os.path.join(self.host.proc, "diskstats"), "w") as f:
            f.write("   8       0 sda 1 0 0 0 1 0 0 0 0 1000 1000 0 0 0 0 0 0\n")
        m = self.j.Measure(proc=self.host.proc, gpu_file=self.host.gpu_file)
        m.disk_of = lambda path: "sda"
        loop = self.j.Loop(self.settings, measure=m)
        self.host.tick()
        loop.cycle()
        self.assertIn("sda", loop.samples[-1]["disks"])

    def test_history_shows_what_a_pilot_was_counted_at(self):
        self.model_card()
        loop = self.loop()
        r = self.submit_sized("sleep 60", pilot=True, gpus=1)
        self.assertTrue(r["ok"], r)
        tid, tok = r["data"]["id"], r["data"]["token"]
        p = self.job("history", "local", "--task", "T-adm", **self.env())
        self.assertIn("pilot: 1 CPU, 1 GPU, to be counted at the free room at its release", p.stdout)
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            rec = self.record(tok)
            # the fake host has 50 GB available and a 1 GB reserve; the card 24 GB, 0.4 GB in use, 0.5 GB reserve
            self.assertAlmostEqual(rec["ram_counted"], 49 * GB, delta=MiB)
            with open(os.path.join(self.state, "assign", tok + ".env")) as f:
                applied = dict(line.strip().split("=", 1) for line in f if "=" in line)
            self.assertEqual(rec["memory_max"], int(applied["JOB_MEMORY_MAX"]))  # the limit the job was given
            gpu = json.loads(rec["gpu_counted"])
            self.assertEqual(list(gpu), [CARD])
            self.assertAlmostEqual(gpu[CARD], 24 * GB - int(0.4 * GB) - 500 * 10 ** 6, delta=MiB)
            p = self.job("history", "local", "--task", "T-adm", **self.env())
            self.assertIn("pilot: 1 CPU, %s, 1 GPU with %s, the free room at its release" % (
                self.j._gbs(rec["ram_counted"]), self.j._gbs(gpu[CARD])), p.stdout)
            p = self.job("history", "local", "--json", "--task", "T-adm", **self.env())
            j = [x for x in json.loads(p.stdout)["local"]["jobs"] if x["token"] == tok][0]
            self.assertEqual(j["memory_max"], rec["memory_max"])
            self.assertEqual(j["gpu_counted"], gpu)
            # a record write lost at release (or a release by a loop before these fields) is filled in next cycle
            con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"))
            con.execute("UPDATE jobs SET ram_counted = NULL, gpu_counted = NULL, memory_max = NULL WHERE token = ?",
                        (tok,))
            con.commit()
            con.close()
            self.host.tick()
            loop.cycle()
            self.assertEqual(self.record(tok)["memory_max"], rec["memory_max"])
            self.assertEqual(self.record(tok)["gpu_counted"], rec["gpu_counted"])
        finally:
            self.job("kill", "local", str(tid))

    def test_a_job_held_again_loses_its_release_in_the_record(self):
        self.host.set(avail=2 * GB)  # 1 GB room: the job does not fit, so it stays held once held again
        loop = self.loop()
        r = self.submit_sized("sleep 1", cpus=1, ram=int(1.5 * GB))
        tid, tok = r["data"]["id"], r["data"]["token"]
        qpath = os.path.join(self.state, "queue", tok + ".json")
        with open(qpath) as f:
            q = json.load(f)
        q.update(state="released", released=time.time(), gpus=[], ram_counted=q["size"]["ram"], gpu_counted={},
                 memory_max=3 * GB)
        with open(qpath, "w") as f:  # what a loop stopped between saving and pueue enqueue leaves
            json.dump(q, f)
        con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"))
        con.execute("UPDATE jobs SET released = ?, ram_counted = ?, memory_max = ? WHERE token = ?",
                    (q["released"], q["ram_counted"], 3 * GB, tok))
        con.commit()
        con.close()
        self.host.tick()
        loop.cycle()
        rec = self.record(tok)
        self.assertEqual(self.status(tid), "Stashed")
        self.assertIsNone(rec["released"])
        self.assertIsNone(rec["memory_max"])
        self.assertIsNone(rec["ram_counted"])
        p = self.job("history", "local", "--task", "T-adm", **self.env())
        self.assertIn(" held ", p.stdout)
        self.d.pueue("enqueue", str(tid))  # a person starts it: with no assignment its wrapper ends it at once
        self.assertTrue(self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=60))
        rec = self.record(tok)
        self.assertEqual(rec["exit"], 205)
        self.assertIsNone(rec["memory_max"])
        self.assertEqual(rec["ram_counted"], int(1.5 * GB))
        p = self.job("history", "local", "--task", "T-adm", **self.env())
        self.assertIn("explicit: 1 CPU, 1.5 GB, not released by the loop", p.stdout)

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

    # -- TASK-007.01: oversized jobs and job resize ------------------------------------------------------------

    def queue_file(self, tok):
        with open(os.path.join(self.state, "queue", tok + ".json")) as f:
            return json.load(f)

    def test_an_oversized_running_job_alerts_and_shrinks(self):
        self.write_settings(oversize_after_min=1, auto_resize=False)
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("python3 -c 'b = bytearray(60 * 1024 * 1024); import time; time.sleep(300)'",
                              cpus=2, ram=400 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB
                                        and loop.peaks[tok]["n"] >= 3, n=40))
            w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--token", tok, "--poll", "0.3"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=self.d.env(**self.env()))
            time.sleep(1.5)
            now[0] += 61  # past oversize_after_min
            self.host.tick()
            loop.cycle()
            out, err = w.communicate(timeout=60)
            self.assertEqual(w.returncode, 201, out + err)
            self.assertIn("oversized", out)
            m = __import__("re").search(r"exact command: job resize local %d --token %s --ram (\S+)\)" % (tid, tok), out)
            self.assertTrue(m, out)
            suggest = m.group(1)
            room = self.job("room", "local", **self.env()).stdout
            self.assertIn("oversized: its RAM peaked at", room)
            self.assertIn("job resize local %d --token %s --ram %s" % (tid, tok, suggest), room)
            # refused: growing, and a limit under the measured peak times resize_margin
            p = self.job("resize", "local", str(tid), "--ram", "500Mi", **self.env())
            self.assertEqual(p.returncode, 205, p.stdout)
            self.assertIn("can only shrink", p.stdout)
            p = self.job("resize", "local", str(tid), "--ram", "40Mi", **self.env())
            self.assertIn("the smallest size accepted", p.stdout)
            p = self.job("resize", "local", str(tid), "--token", "0" * 32, "--ram", suggest, **self.env())
            self.assertIn("belongs to a different job", p.stdout)
            # the suggested size is accepted: the limit is lowered at once, the count from the next cycle
            p = self.job("resize", "local", str(tid), "--token", tok, "--ram", suggest, "--cpus", "1", **self.env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            new = self.j._parse_size(suggest)
            limit = self.j.ram_limit(self.settings, new)
            with open(os.path.join(loop.m.scopes()[tok], "job", "memory.max")) as f:
                self.assertEqual(int(f.read()), limit)
            self.host.tick()
            loop.cycle()
            with open(os.path.join(self.state, "room.json")) as f:
                j = [x for x in json.load(f)["jobs"] if x["token"] == tok][0]
            self.assertEqual(j["ram_counted"], limit)
            self.assertEqual(j["size"]["ram"], new)
            self.assertEqual(j["size"]["cpus"], 1)
            self.assertEqual(self.queue_file(tok)["memory_max"], limit)  # the loop keeps it in the queue file too
        finally:
            self.job("kill", "local", str(tid))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        rec = self.record(tok)
        self.assertEqual(rec["memory_max"], limit)
        self.assertEqual(json.loads(rec["size"])["ram"], new)
        h = self.job("history", "local", "--token", tok, **self.env()).stdout
        self.assertIn("resized from 2 CPU, 0.4 GB", h)
        self.assertFalse(os.path.exists(os.path.join(self.state, "resize", tok + ".json")))

    def test_a_held_job_is_resized_in_place(self):
        self.host.set(avail=3 * GB)
        loop = self.loop()
        r = self.submit_sized("echo resized-held", cpus=1, ram=4 * GB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        submitted = self.queue_file(tok)["submitted"]
        con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"))
        con.execute("INSERT INTO jobs (token, task, pueue_id, ended, peak_ram, peak_samples) VALUES (?, ?, ?, ?, ?, ?)",
                    ("e" * 32, "T-adm", 99, time.time() - 600, 1.5 * GB, 50))
        con.commit()
        con.close()
        try:
            p = self.job("resize", "local", str(tid), "--ram", "1G", **self.env())
            self.assertEqual(p.returncode, 205, p.stdout)
            self.assertIn("below the RAM peak of 1.5 GB of an earlier run of T-adm here (#99, token %s" % ("e" * 32),
                          p.stdout)
            p = self.job("resize", "local", str(tid), "--ram", "100G", **self.env())
            self.assertIn("even when it is empty", p.stdout)
            p = self.job("resize", "local", str(tid), "--ram", "1.6G", **self.env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("keeps its place", p.stdout)
            self.host.set(avail=4 * GB)  # 3 GB room: the old size (6 GB limit) would not fit, the new (2.4 GB) does
            loop.samples = []
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))
            self.assertEqual(self.wait(tid, tok).returncode, 0)
            self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
            rec = self.record(tok)
            self.assertEqual(rec["submitted"], submitted)  # its place in the queue did not change
            self.assertEqual(rec["memory_max"], self.j.ram_limit(self.settings, int(1.6 * GB)))
            self.assertEqual(json.loads(rec["resized"])[0]["from"]["ram"], 4 * GB)
        finally:
            if self.status(tid) != "Done":
                self.d.pueue("remove", str(tid))

    def test_a_pilot_is_not_resized(self):
        self.host.set(avail=3 * GB)
        r = self.submit_sized("true", pilot=True)
        self.assertTrue(r["ok"], r)
        try:
            p = self.job("resize", "local", str(r["data"]["id"]), "--ram", "1G", **self.env())
            self.assertEqual(p.returncode, 205, p.stdout)
            self.assertIn("is a pilot", p.stdout)
        finally:
            self.d.pueue("remove", str(r["data"]["id"]))

    # -- TASK-018: jobs reserve only what they use --------------------------------------------------------------

    def _limit(self, loop, tok):
        with open(os.path.join(loop.m.scopes()[tok], "job", "memory.max")) as f:
            return int(f.read())

    def test_max_minutes_over_the_ceiling_is_refused(self):
        self.write_settings(pilot={"max_minutes": 1, "max_minutes_ceiling": 10})
        r = self.submit_sized("true", pilot=True, max_minutes=11)
        self.assertFalse(r["ok"])
        self.assertIn("ceiling for a pilot, 10 minutes", r["error"])
        r = self.submit_sized("true", pilot=True, max_minutes=10)
        self.assertTrue(r["ok"], r)
        self.d.pueue("remove", str(r["data"]["id"]))

    def test_a_pilot_queued_with_a_longer_limit_is_stopped_at_the_ceiling(self):
        self.write_settings(pilot={"max_minutes": 1, "max_minutes_ceiling": 2})
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("sleep 300", pilot=True)
        tid, tok = r["data"]["id"], r["data"]["token"]
        qpath = os.path.join(self.state, "queue", tok + ".json")
        with open(qpath) as f:
            q = json.load(f)
        q["constraints"]["max_minutes"] = 1080  # submitted before the ceiling existed
        with open(qpath, "w") as f:
            json.dump(q, f)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        self.assertTrue(self.cycles(loop, lambda: self.queue_file(tok).get("started"), n=10))
        now[0] += 61
        self.cycles(loop, lambda: False, n=3)
        self.assertEqual(self.status(tid), "Running")  # under the ceiling still
        now[0] += 60
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done"))

    def test_an_oversized_running_job_is_counted_at_its_use_by_the_loop(self):
        self.write_settings(oversize_after_min=1)
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("python3 -c 'b = bytearray(60 * 1024 * 1024); import time; time.sleep(300)'",
                              cpus=2, ram=400 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB
                                        and loop.peaks[tok]["n"] >= 3, n=40))
            before = self._limit(loop, tok)
            now[0] += 61  # past oversize_after_min
            self.host.tick()
            loop.cycle()
            self.assertEqual(self._limit(loop, tok), before)  # its hard limit is kept
            with open(os.path.join(loop.m.scopes()[tok], "job", "memory.high")) as f:
                high = int(f.read())
            self.assertLess(high, 0.8 * before)
            self.assertGreaterEqual(high, 60 * MiB * self.settings["resize_margin"])
            self.host.tick()
            loop.cycle()
            with open(os.path.join(self.state, "room.json")) as f:
                j = [x for x in json.load(f)["jobs"] if x["token"] == tok][0]
            self.assertEqual(j["ram_counted"], high)
            self.assertIsNone(j.get("oversize"))
            q = self.queue_file(tok)
            self.assertEqual(q["memory_high"], high)
            self.assertEqual(q["resized"][-1]["auto"], "shrink")
            self.assertFalse(os.path.exists(os.path.join(self.state, "alerts", tok + ".jsonl")))  # nothing asked
        finally:
            self.job("kill", "local", str(tid))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        self.assertEqual(self.record(tok)["ram_counted"], high)
        h = self.job("history", "local", "--token", tok, **self.env()).stdout
        self.assertIn("by the loop (its use)", h)

    def test_a_job_cut_to_its_use_that_needs_more_is_slowed_and_grown_not_stopped(self):
        self.write_settings(oversize_after_min=1)
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        flag = os.path.join(self.d.dir, "phase2-%d" % time.monotonic_ns())
        r = self.submit_sized("python3 -c 'import os, time\nb = bytearray(60 * 1024 * 1024)\n"
                              "while not os.path.exists(\"%s\"): time.sleep(0.2)\n"
                              "c = bytearray(250 * 1024 * 1024); c[::4096] = b\"x\" * len(c[::4096])\n"
                              "time.sleep(300)'" % flag, cpus=1, ram=400 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB
                                        and loop.peaks[tok]["n"] >= 3, n=40))
            now[0] += 61
            self.host.tick()
            loop.cycle()
            high = self.queue_file(tok)["memory_high"]
            self.assertLess(high, 250 * MiB)
            open(flag, "w").close()  # its second phase needs more than its soft limit
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 300 * MiB, n=120))
            self.assertEqual(self.status(tid), "Running")
            self.assertGreater(self.queue_file(tok).get("memory_high") or 10 ** 12, high)
        finally:
            self.job("kill", "local", str(tid))

    def test_a_pilot_is_counted_at_its_use_after_its_measuring_window(self):
        self.write_settings(pilot={"max_minutes": 30, "shrink_after_min": 1, "shrink_margin": 2.0})
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized("python3 -c 'b = bytearray(60 * 1024 * 1024); import time; time.sleep(300)'",
                              pilot=True)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB
                                        and loop.peaks[tok]["n"] >= 3, n=40))
            before = self._limit(loop, tok)
            self.assertGreater(before, 10 * GB)  # all the room at its release
            now[0] += 61
            self.host.tick()
            loop.cycle()
            self.assertEqual(self._limit(loop, tok), before)  # its hard limit is kept: a pilot is slowed, not stopped
            with open(os.path.join(loop.m.scopes()[tok], "job", "memory.high")) as f:
                high = int(f.read())
            peak = loop.peaks[tok]["ram"]
            self.assertLess(high, 1 * GB)
            self.assertGreaterEqual(high, 2.0 * peak * 0.99)
            self.host.tick()
            loop.cycle()
            with open(os.path.join(self.state, "room.json")) as f:
                j = [x for x in json.load(f)["jobs"] if x["token"] == tok][0]
            self.assertEqual(j["ram_counted"], high)
        finally:
            self.job("kill", "local", str(tid))
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        h = self.job("history", "local", "--token", tok, **self.env()).stdout
        self.assertIn("cut by the loop from", h)

    def test_a_pilot_over_its_soft_limit_is_counted_at_its_use_and_grown(self):
        self.write_settings(pilot={"max_minutes": 30, "shrink_after_min": 1, "shrink_margin": 2.0})
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        flag = os.path.join(self.d.dir, "grow-%d" % time.monotonic_ns())
        r = self.submit_sized("python3 -c 'import os, time\nb = bytearray(60 * 1024 * 1024)\n"
                              "while not os.path.exists(\"%s\"): time.sleep(0.2)\n"
                              "c = bytearray(300 * 1024 * 1024); c[::4096] = b\"x\" * len(c[::4096])\n"
                              "time.sleep(300)'" % flag, pilot=True)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 60 * MiB
                                        and loop.peaks[tok]["n"] >= 3, n=40))
            now[0] += 61
            self.host.tick()
            loop.cycle()
            high = self.queue_file(tok)["memory_high"]
            self.assertLess(high, 300 * MiB)
            open(flag, "w").close()  # it now loads far more than its soft limit: slowed, never stopped
            self.assertTrue(self.cycles(loop, lambda: self.queue_file(tok).get("memory_high", 0) > high, n=40))
            self.assertEqual(self.status(tid), "Running")
            self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= 300 * MiB, n=80))
            self.assertEqual(self.status(tid), "Running")
        finally:
            self.job("kill", "local", str(tid))

    def test_a_job_pressing_its_limit_grows_when_the_host_has_room(self):
        loop = self.loop()
        size = 300 * MiB  # limit 450 MiB; it uses about 430 MiB, over grow_at_fraction of it
        r = self.submit_sized("python3 -c 'b = bytearray(415 * 1024 * 1024); b[::4096] = b\"x\" * len(b[::4096]); "
                              "import time; time.sleep(300)'", cpus=1, ram=size)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            first = self.j.ram_limit(self.settings, size)
            self.assertTrue(self.cycles(loop, lambda: self._limit(loop, tok) > first, n=40))
            grown = self._limit(loop, tok)
            self.assertGreaterEqual(grown, int(first * self.settings["grow_factor"]) - 4096)
            q = self.queue_file(tok)
            self.assertEqual(q["memory_max"], grown)
            self.assertEqual(q["resized"][-1]["auto"], "grow")
            self.host.tick()
            loop.cycle()
            self.assertEqual(self._limit(loop, tok), grown)  # not shrunk back, nor grown again
        finally:
            self.job("kill", "local", str(tid))

    def test_a_grow_does_not_hold_the_records_while_it_waits_for_the_queue_lock(self):
        # review 2026-11-17: the loop took queue.lock with its record transaction open, so a submit holding the lock
        # and writing a record failed with "database is locked" after the sqlite timeout
        loop = self.loop()
        size = 300 * MiB
        r = self.submit_sized("python3 -c 'b = bytearray(415 * 1024 * 1024); b[::4096] = b\"x\" * len(b[::4096]); "
                              "import time; time.sleep(300)'", cpus=1, ram=size)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.addCleanup(lambda: self.job("kill", "local", str(tid)))
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        real, seen = self.j.Loop._apply_resize, {}
        db = os.path.join(self.state, "admission.sqlite")
        code = ("import fcntl, os, sqlite3, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('locked', flush=True)\n"
                "c = sqlite3.connect(sys.argv[2], timeout=5)\n"
                "try:\n    c.execute('UPDATE jobs SET task = task WHERE token = ?', (sys.argv[3],)); c.commit(); "
                "print('ok')\nexcept Exception as e:\n    print('failed', e)\n")

        def wrapped(selfloop, con, q, old, new, how, do):
            p = subprocess.Popen([sys.executable, "-c", code, os.path.join(self.state, "queue.lock"), db, tok],
                                 stdout=subprocess.PIPE, text=True)
            p.stdout.readline()
            t = time.time()
            out = real(selfloop, con, q, old, new, how, do)
            seen.update(wait=time.time() - t, other=p.stdout.read().strip())
            return out
        self.j.Loop._apply_resize = wrapped
        try:
            self.assertTrue(self.cycles(loop, lambda: "other" in seen, n=40))
        finally:
            self.j.Loop._apply_resize = real
        self.assertEqual(seen["other"], "ok")
        self.assertLess(seen["wait"], 4)

    def test_a_job_pressing_its_limit_is_not_grown_without_room(self):
        loop = self.loop()
        size = 300 * MiB
        r = self.submit_sized("python3 -c 'b = bytearray(415 * 1024 * 1024); b[::4096] = b\"x\" * len(b[::4096]); "
                              "import time; time.sleep(300)'", cpus=1, ram=size)
        tid, tok = r["data"]["id"], r["data"]["token"]
        try:
            self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
            self.host.set(avail=1 * GB)  # under the 1 GB headroom once the job's own use is set aside
            first = self.j.ram_limit(self.settings, size)
            self.assertTrue(self.cycles(loop, lambda: self.queue_file(tok).get("grow_blocked"), n=20))
            self.assertEqual(self._limit(loop, tok), first)
        finally:
            self.job("kill", "local", str(tid))

    # -- TASK-007.04: held messages ----------------------------------------------------------------------------

    def test_the_first_held_line_gives_the_real_reason(self):
        self.host.set(avail=3 * GB)
        r = self.submit_sized("true", cpus=1, ram=4 * GB)  # no loop has run here: no heartbeat, no snapshot
        self.assertIn("the admission loop on this host is not running", r["data"]["held_reason"])
        self.d.pueue("remove", str(r["data"]["id"]))
        adm = self.j.Admission(self.settings)
        adm.write_json(adm.path("heartbeat"), {"t": time.time()})  # a loop that runs, before its first snapshot
        r = self.submit_sized("true", cpus=1, ram=4 * GB)
        self.assertIn("the admission loop is measuring this host", r["data"]["held_reason"])
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        p = self.job("submit", "local", "T-held", "--cpus", "1", "--ram", "4G", "--", "true", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tid = int(p.stdout.strip())
        self.assertIn("held: needs 6.0 GB RAM (its limit, 1.5 times its size)", p.stderr)
        ls = self.job("ls", "local", **self.env()).stdout
        self.assertNotIn("not looked at", ls)
        for x in (r["data"]["id"], tid):
            self.d.pueue("remove", str(x))

    # -- TASK-007.02: small younger jobs pass the fairness rule ------------------------------------------------

    def test_a_small_younger_job_passes_an_older_one_that_waits(self):
        now = [time.time()]
        self.host.set(avail=12 * GB)
        loop = self.loop(clock=lambda: now[0])
        first = self.submit_sized("sleep 60", cpus=1, ram=4 * GB)  # runs, counted at its 6 GB limit: 5 GB left
        self.assertTrue(self.cycles(loop, lambda: self.status(first["data"]["id"]) == "Running"))
        big = self.submit_sized("echo big", cpus=1, ram=4 * GB)    # lacks 1 GB of RAM
        now[0] += 120  # past the one-minute fairness wait
        # each tiny job is counted at 60 MB, under a tenth of what the oldest lacks; two of them are not
        tiny = [self.submit_sized("sleep 30", cpus=0.1, ram=40 * 10 ** 6) for _ in range(2)]
        try:
            for _ in range(3):
                self.host.tick()
                loop.cycle()
            self.assertEqual(self.status(big["data"]["id"]), "Stashed")
            self.assertIn(self.status(tiny[0]["data"]["id"]), ("Running", "Done", "Queued"))
            self.assertEqual(self.status(tiny[1]["data"]["id"]), "Stashed")
            with open(os.path.join(self.state, "room.json")) as f:
                reasons = {h["token"]: h["reason"] for h in json.load(f)["held"]}
            why = reasons[tiny[1]["data"]["token"]]
            # TASK-007.04: it names the older job and what that job needs, and says no start can be estimated
            self.assertIn("waiting behind an older job, T-adm (token %s), that needs: needs 6.0 GB RAM"
                          % big["data"]["token"], why)
            self.assertIn("start estimate: none known: a running job that holds that room has no projected end", why)
            self.assertNotIn("exit 137", why)  # the older job's hint is its own, said once
        finally:
            for r in [first] + tiny:
                self.job("kill", "local", str(r["data"]["id"]))
            self.d.pueue("remove", str(big["data"]["id"]))

    # -- review 1 of TASK-007.01-04 ------------------------------------------------------------------------------

    def running_job(self, command="python3 -c 'b = bytearray(60 * 1024 * 1024); import time; time.sleep(300)'",
                    min_peak=60 * MiB, **over):
        """A job sized 2 CPU, 400 MiB, running and measured at least 3 times, with a loop on a settable clock."""
        self.write_settings(oversize_after_min=1, **over)
        now = [time.time()]
        loop = self.loop(clock=lambda: now[0])
        r = self.submit_sized(command, cpus=2, ram=400 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.addCleanup(lambda: self.job("kill", "local", str(tid)))
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Running"))
        self.assertTrue(self.cycles(loop, lambda: (loop.peaks.get(tok) or {}).get("ram", 0) >= min_peak
                                    and loop.peaks[tok]["n"] >= 3, n=40))
        return loop, tid, tok, now

    def drop(self, tid):
        """Remove a test's held task, if it is still there."""
        subprocess.run(["pueue", "-c", self.d.cfg, "remove", str(tid)], capture_output=True)

    def memory_max(self, loop, tok):
        with open(os.path.join(loop.m.scopes()[tok], "job", "memory.max")) as f:
            return int(f.read())

    def counted(self, tok):
        with open(os.path.join(self.state, "room.json")) as f:
            return [x for x in json.load(f)["jobs"] if x["token"] == tok][0]["ram_counted"]

    def test_an_out_of_memory_verdict_names_the_limit_it_ended_under(self):
        loop, tid, tok, now = self.running_job(
            "python3 -c 'import time; b = bytearray(60 * 1024 * 1024); time.sleep(12); "
            "c = bytearray(300 * 1024 * 1024); c[::4096] = b\"x\" * len(c[::4096]); time.sleep(100)'")
        p = self.job("resize", "local", str(tid), "--ram", "100Mi", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        limit = self.j.ram_limit(self.settings, 100 * MiB)
        self.assertTrue(self.cycles(loop, lambda: self.status(tid) == "Done", n=120))
        p = self.wait(tid, tok)
        self.assertIn("over its RAM limit of %s" % self.j._gb(limit), p.stdout)
        self.assertNotIn(self.j._gb(self.j.ram_limit(self.settings, 400 * MiB)), p.stdout)
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        final = self.j._read_kv(os.path.join(self.j.STATE_DIR, "results", tok + ".final"))
        self.assertLess(abs(int(final["memory.max"]) - limit), 65536)  # the shim read the limit at its end
        self.assertEqual(self.record(tok)["memory_max"], int(final["memory.max"]))
        self.assertEqual(self.record(tok)["how"], "memory limit")

    def test_a_cpus_only_resize_never_raises_the_ram_limit(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        self.host.tick()
        loop.cycle()
        counted = self.counted(tok)
        self.write_settings(oversize_after_min=1, ram_hard_factor=2.0)  # changed since the job was released
        p = self.job("resize", "local", str(tid), "--cpus", "1", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.memory_max(loop, tok), was)
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.counted(tok), counted)
        # a --ram whose limit by the new factor is above the limit it runs under is refused
        p = self.job("resize", "local", str(tid), "--ram", "350Mi", **self.env())
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("never raised", p.stdout)
        self.assertEqual(self.memory_max(loop, tok), was)

    def test_a_running_resize_whose_scope_is_not_found_is_refused_and_changes_nothing(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        self.host.tick()
        loop.cycle()
        counted = self.counted(tok)
        env = dict(self.env(), JOB_TEST_CGROUP="/nonexistent")
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **env)
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("not among this user's job scopes", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.state, "resize", tok + ".json")))
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.counted(tok), counted)
        self.assertEqual(self.memory_max(loop, tok), was)
        # and a recorded count below the cgroup's real limit (as that resize left it) counts at the real limit
        q = self.queue_file(tok)
        q.update(memory_max=150 * MiB, ram_counted=150 * MiB)
        with open(os.path.join(self.state, "queue", tok + ".json"), "w") as f:
            json.dump(q, f)
        self.host.tick()
        loop.cycle()
        self.assertEqual(self.memory_max(loop, tok), was)
        self.assertGreaterEqual(self.counted(tok), was)

    def test_the_running_floor_counts_the_kernels_peak_and_cpus_their_measured_use(self):
        loop, tid, tok, now = self.running_job(
            "python3 -c 'b = bytearray(200 * 1024 * 1024); b[::4096] = b\"x\" * len(b[::4096]); del b; "
            "c = bytearray(20 * 1024 * 1024); import time; time.sleep(300)'", min_peak=15 * MiB)
        # the spike came and went between samples: the record knows only the use now
        con = sqlite3.connect(os.path.join(self.state, "admission.sqlite"))
        con.execute("UPDATE jobs SET peak_ram = ?, peak_cpu = 1.5 WHERE token = ?", (30 * MiB, tok))
        con.commit()
        con.close()
        was = self.memory_max(loop, tok)
        p = self.job("resize", "local", str(tid), "--ram", "80Mi", **self.env())  # limit 120 MiB, over 1.25 x 30 MiB
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("memory.peak", p.stdout)
        self.assertEqual(self.memory_max(loop, tok), was)
        p = self.job("resize", "local", str(tid), "--cpus", "1", **self.env())
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("under its measured CPU use, 1.50 threads", p.stdout)

    def test_a_job_ends_under_the_queue_lock_so_a_resize_cannot_outlive_it(self):
        import threading
        loop = self.loop()
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.assertTrue(self.cycles(loop, lambda: self.queue_file(tok)["state"] == "released"))
        for _ in range(200):  # it ends; no cycle runs, so the loop has not ended it yet
            if self.status(tid) == "Done":
                break
            time.sleep(0.05)
        time.sleep(0.5)
        adm = self.j.Admission(self.settings)
        fd = adm.lock("queue.lock")  # a resize in progress
        t = threading.Thread(target=lambda: (self.host.tick(), loop.cycle()))
        try:
            t.start()
            time.sleep(1.5)
            self.assertTrue(os.path.exists(os.path.join(self.state, "queue", tok + ".json")))  # the end waits
            adm.write_json(adm.resize_path(tok), {"size": {"cpus": 1, "ram": 50 * MiB}, "resized": [{"t": 1.0}]})
        finally:
            os.close(fd)
        t.join(60)
        self.cycles(loop, lambda: (self.record(tok) or {}).get("how") is not None, n=10)
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue", tok + ".json")))
        self.assertFalse(os.path.exists(adm.resize_path(tok)))
        self.assertIsNotNone(self.record(tok)["ended"])
        # an orphan resize file (from a host before this fix) is swept
        adm.write_json(adm.resize_path("f" * 32), {"size": {}})
        self.host.tick()
        loop.cycle()
        self.assertFalse(os.path.exists(adm.resize_path("f" * 32)))

    def test_a_failed_resize_changes_nothing(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        os.environ.update(self.env())  # host_resize runs in this process
        self.addCleanup(os.environ.pop, "JOB_ADMISSION_CONFIG", None)
        real = self.j.Admission.record

        def failing(adm, con, row):
            if "resized" in row:
                raise sqlite3.OperationalError("disk I/O error")
            return real(adm, con, row)
        self.j.Admission.record = failing
        try:
            with self.assertRaises(self.j.JobError) as e:
                self.j.host_resize({"id": tid, "ram": 300 * MiB})
        finally:
            self.j.Admission.record = real
        self.assertIn("was not resized", str(e.exception))
        self.assertEqual(self.memory_max(loop, tok), was)
        self.assertFalse(os.path.exists(os.path.join(self.state, "resize", tok + ".json")))
        self.assertIsNone(self.record(tok)["resized"])

    def test_a_held_resize_wakes_the_loop(self):
        self.host.set(avail=3 * GB)
        r = self.submit_sized("true", cpus=1, ram=4 * GB)
        self.addCleanup(self.drop, r["data"]["id"])
        q = os.path.join(self.state, "queue")
        os.utime(q, (1.0, 1.0))
        p = self.job("resize", "local", str(r["data"]["id"]), "--ram", "1G", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(os.path.getmtime(q), 1.0)

    def test_the_first_held_line_follows_the_loops_order(self):
        # a paused group, and a full one
        self.d.pueue("group", "add", "rv-paused")
        self.d.pueue("pause", "--group", "rv-paused")
        self.d.pueue("group", "add", "rv-full")
        self.d.pueue("parallel", "1", "--group", "rv-full")
        loop = self.loop()
        self.host.tick()
        loop.cycle()
        r = self.submit_sized("true", cpus=1, ram=100 * MiB, group="rv-paused")
        self.addCleanup(self.drop, r["data"]["id"])
        self.assertIn("its group rv-paused is paused", r["data"]["held_reason"])
        busy = self.d.pueue("add", "--group", "rv-full", "--", "sleep", "60")
        self.addCleanup(lambda: self.d.pueue("kill", "--group", "rv-full"))
        r = self.submit_sized("true", cpus=1, ram=100 * MiB, group="rv-full")
        self.addCleanup(self.drop, r["data"]["id"])
        self.assertIn("its group rv-full is full", r["data"]["held_reason"])
        self.assertTrue(busy)

    def test_the_first_held_line_says_when_the_fairness_rule_holds_a_job(self):
        now = [time.time()]
        self.host.set(avail=12 * GB)
        loop = self.loop(clock=lambda: now[0])
        first = self.submit_sized("sleep 60", cpus=1, ram=4 * GB)  # runs, counted at its 6 GB limit: 5 GB left
        self.addCleanup(lambda: self.job("kill", "local", str(first["data"]["id"])))
        self.assertTrue(self.cycles(loop, lambda: self.status(first["data"]["id"]) == "Running"))
        big = self.submit_sized("echo big", cpus=1, ram=4 * GB)  # lacks 1 GB of RAM
        self.addCleanup(self.drop, big["data"]["id"])
        self.assertIn("ends in exit 137", big["data"]["held_hint"])  # its size was not measured
        now[0] += 120
        self.host.tick()
        loop.cycle()
        # fits the 5 GB room (3 GB limit), but takes far over a tenth of the 1 GB the oldest lacks
        r = self.submit_sized("echo young", cpus=1, ram=2 * GB)
        self.addCleanup(self.drop, r["data"]["id"])
        self.assertIn("waiting behind an older job, T-adm (token %s)" % big["data"]["token"], r["data"]["held_reason"])
        self.assertNotIn("exit 137", r["data"]["held_reason"])

    def test_scopes_of_jobs_this_loop_did_not_admit_are_outside_use(self):
        loop = self.loop()
        r = self.submit_sized("sleep 30", cpus=1, ram=100 * MiB)
        self.addCleanup(lambda: self.job("kill", "local", str(r["data"]["id"])))
        tok = r["data"]["token"]
        other = "a" * 32
        got = loop.own_scopes({tok: "/x", other: "/y"})
        self.assertEqual(got, {tok: "/x"})

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


    # -- TASK-003.09: memory pressure outside the jobs' own limits; undersized jobs ---------------------------------

    def fake_pressure(self, rel, full10, full60=0.0):
        d = os.path.join(self.host.cgroups, rel.strip("/"))
        os.makedirs(d, exist_ok=True)
        self.host.fake_psi[d] = [full10, full60, 0]
        self.host.tick()

    def stalled_at_its_limit(self, loop, tok, full10=30.0, full60=30.0):
        """Make the loop read the job's own scope as stalled against its own limit (the kernel's figures, faked)."""
        real_pressure, real_events = loop.m.cgroup_pressure, loop.m.limit_events
        events, total = [0], [0]

        def pressure(path):
            if tok in path:
                total[0] += 100000  # its stall time grows, sample by sample
                return {"some": {"avg10": str(full10), "avg60": str(full60), "total": str(total[0])},
                        "full": {"avg10": str(full10), "avg60": str(full60), "total": str(total[0])}}
            return real_pressure(path)

        def limit_events(path):
            if tok in path:
                events[0] += 5  # its use reached its memory.max again since the last sample
                return events[0]
            return real_events(path)
        loop.m.cgroup_pressure, loop.m.limit_events = pressure, limit_events

    def test_a_job_stalled_against_its_own_limit_is_grown_while_the_host_has_room(self):
        self.fake_pressure("system.slice", 0.2)
        self.fake_pressure("user.slice/user-4242.slice", 0.5)
        loop, tid, tok, now = self.running_job()
        self.stalled_at_its_limit(loop, tok)
        first = self.queue_file(tok)["memory_max"]
        for _ in range(2):
            self.host.tick()
            loop.cycle()
        q = self.queue_file(tok)
        self.assertGreater(q["memory_max"], first)
        self.assertEqual(q["resized"][-1]["auto"], "grow")
        self.assertNotIn("undersized", q.get("alerted") or [])

    def test_a_job_stalled_against_its_own_limit_holds_no_one_and_alerts_its_owner(self):
        self.fake_pressure("system.slice", 0.2)
        self.fake_pressure("user.slice/user-4242.slice", 0.5)  # under 1%: nothing outside the jobs is stalled
        loop, tid, tok, now = self.running_job(auto_resize=False)
        self.stalled_at_its_limit(loop, tok)
        self.host.set(psi=22.4)  # the system-wide figure counts the job's own stall
        w = subprocess.Popen([sys.executable, JOB, "wait", "local", str(tid), "--token", tok, "--poll", "0.3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.d.env(**self.env()))
        time.sleep(1.5)
        self.host.tick()
        loop.cycle()
        self.host.tick()
        loop.cycle()
        out, err = w.communicate(timeout=60)
        self.assertEqual(w.returncode, 201, out + err)
        self.assertIn("undersized: over the last minute it spent 30% of its time stalled", out)
        self.assertIn("not host memory pressure", out)
        room = loop.room
        self.assertTrue(room["pressure_ok"], room)
        self.assertEqual((room["psi"], room["psi_source"], room["psi_where"]), (0.5, "cgroups", "/user.slice/user-4242.slice"))
        # another job is released although the system-wide figure is 22.4%
        r = self.submit_sized("sleep 30", cpus=1, ram=100 * MiB)
        self.addCleanup(lambda: self.job("kill", "local", str(r["data"]["id"])))
        self.assertTrue(self.cycles(loop, lambda: self.status(r["data"]["id"]) in ("Running", "Done"), n=20))
        text = self.job("room", "local", **self.env()).stdout
        self.assertIn("undersized: 30% of its time stalled against its own RAM limit", text)
        self.assertIn("memory pressure 0.5% (in /user.slice/user-4242.slice", text)
        self.assertIn("system-wide 22.4%", text)
        with open(os.path.join(self.state, "alerts", tok + ".jsonl")) as f:
            self.assertEqual([json.loads(x)["kind"] for x in f], ["undersized"])  # once per job

    def test_real_pressure_outside_the_jobs_still_holds_jobs(self):
        self.fake_pressure("system.slice", 1.0)
        self.fake_pressure("user.slice/user-4242.slice", 35.0)  # another user's notebook stalls: RAM is short
        self.host.set(psi=40.0)  # and so the system-wide figure is high too
        loop = self.loop()
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        self.addCleanup(self.drop, r["data"]["id"])
        for _ in range(3):
            self.host.tick()
            loop.cycle()
        self.assertEqual(self.status(r["data"]["id"]), "Stashed")
        reason = self.queue_file(r["data"]["token"])["reason"]
        self.assertIn("memory pressure 35.0% (in /user.slice/user-4242.slice", reason)

    # -- review 3 of the admission work ------------------------------------------------------------------------------

    def test_a_held_resize_reads_the_gpus_before_it_takes_the_queue_lock(self):
        self.host.set(avail=3 * GB)
        r = self.submit_sized("true", cpus=1, ram=4 * GB)
        tid = r["data"]["id"]
        self.addCleanup(self.drop, tid)
        fifo = os.path.join(self.d.dir, "gpus-%d.fifo" % time.monotonic_ns())
        os.mkfifo(fifo)  # a GPU query that does not answer until the test writes to it
        saved = {k: os.environ.get(k) for k in ("JOB_TEST_GPUS", "JOB_TEST_PROC")}
        os.environ.update(JOB_TEST_GPUS=fifo, JOB_TEST_PROC=self.host.proc)
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.j.host_resize({"id": tid, "ram": 2 * GB})))
        th.start()
        try:
            time.sleep(1.0)
            fd = os.open(os.path.join(self.state, "queue.lock"), os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free = True
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                free = False
            os.close(fd)
            with open(fifo, "w") as f:
                f.write(json.dumps({"cards": []}))
            th.join(30)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(free, "the queue lock was held while the GPUs were read")
        self.assertEqual(out["r"]["size"]["ram"], 2 * GB)

    def test_the_first_held_line_and_fit_now_see_a_busy_data_disk(self):
        self.write_settings(disk_busy_max_pct=50)
        adm = self.j.Admission(self.settings)
        now = time.time()
        samples = [dict(sample(now - 20 + i, avail=50 * GB), disks={"sda": 100.0}) for i in range(3)]
        room = self.j.compute_room(self.settings, samples, [], [], now)
        adm.write_json(adm.path("room.json"), {"t": now, "written": now, "room": room, "held": [], "jobs": [],
                                               "holds": [], "warmup": None, "fairness": None})
        adm.write_json(adm.path("heartbeat"), {"t": now})
        job = held(cpus=1, ram=1 * GB, group="sched", data_disk="sda")
        job["size"]["disk_mb_s"] = 500.0
        reason, _ = self.j._submit_reason(self.settings, adm, job, None)
        self.assertIn("disk sda is busy", reason)
        self.assertFalse(self.j._simulate_fit(self.settings, adm, job)[0])

    def test_the_first_held_line_says_no_loop_runs_when_none_ever_ran(self):
        adm = self.j.Admission(self.settings)
        reason, _ = self.j._submit_reason(self.settings, adm, held(group="sched"), None)
        self.assertIn("is not running", reason)

    def test_a_job_whose_peak_reached_its_limit_gets_no_alert_and_is_not_shrunk(self):
        if subprocess.run(["stat", "-f", "-c", "%T", "/var/tmp"], capture_output=True, text=True).stdout.strip() in (
                "tmpfs", "ramfs"):
            self.skipTest("/var/tmp is in memory here, so its pages are not file cache")
        d = tempfile.mkdtemp(prefix="jobt.", dir="/var/tmp")  # on disk: reading it back fills the page cache
        self.addCleanup(_remove_var_tmp_dir, d)
        cmd = ("python3 -c \"import os,time; f=open('%s/big','wb'); [f.write(b'x'*1048576) for _ in range(900)]; "
               "f.close(); os.system('cat %s/big > /dev/null; cat %s/big > /dev/null'); time.sleep(300)\"" % (d, d, d))
        loop, tid, tok, now = self.running_job(cmd, min_peak=1 * MiB)
        cg = os.path.join(loop.m.scopes()[tok], "job")
        for _ in range(60):  # until the 900 MB file has been read through the 600 MiB limit
            self.host.tick()
            loop.cycle()
            if (self.j._read_cg_int(cg, "memory.peak") or 0) >= 0.9 * self.memory_max(loop, tok):
                break
            time.sleep(0.5)
        self.assertGreaterEqual(self.j._read_cg_int(cg, "memory.peak"), 0.9 * self.memory_max(loop, tok))
        now[0] += 3600
        self.host.tick()
        loop.cycle()
        # memory.peak at its limit: what filled it is unknown (cache, or an anonymous spike that squeezed the cache
        # out), so the floor is the whole memory.peak and nothing can be given back
        self.assertIsNone(self.queue_file(tok).get("oversize"))
        self.assertNotIn("oversize", self.queue_file(tok).get("alerted") or [])
        was = self.memory_max(loop, tok)
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **self.env())
        self.assertEqual(p.returncode, 205, p.stdout + p.stderr)
        self.assertIn("reached 90% of its limit", p.stdout)
        self.assertEqual(self.memory_max(loop, tok), was)
        self.assertEqual(self.status(tid), "Running")

    def test_a_failed_shrink_never_puts_back_a_limit_above_the_recorded_one(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        cg = os.path.join(loop.m.scopes()[tok], "job")
        os.environ.update(self.env())
        self.addCleanup(os.environ.pop, "JOB_ADMISSION_CONFIG", None)
        adm = self.j.Admission(self.settings)
        mine = self.j.ram_limit(self.settings, 300 * MiB)
        recorded = self.j._whole_pages(mine + (was - mine) // 2)
        lower = self.j.ram_limit(self.settings, 250 * MiB)
        for case in ("recorded lower", "memory.max set since"):
            out = {}

            def run():
                try:
                    out["r"] = self.j.host_resize({"id": tid, "ram": 300 * MiB})
                except self.j.JobError as e:
                    out["e"] = str(e)
            fd = adm.lock("queue.lock")
            th = threading.Thread(target=run)
            th.start()
            try:
                for _ in range(200):
                    if self.memory_max(loop, tok) == mine:
                        break
                    time.sleep(0.05)
                self.assertEqual(self.memory_max(loop, tok), mine)
                q = adm.read_queue(tok)
                if case == "recorded lower":  # a resize recorded a limit between this one's and the old one
                    q["memory_max"] = recorded
                else:  # a resize set a lower memory.max (and recorded it) while this one waited for the lock
                    q["memory_max"] = lower
                    self.j._set_memory_max(cg, lower)
                adm.save(q)
            finally:
                os.close(fd)
            th.join(60)
            self.assertIn("changed while it was being resized", out.get("e", ""), out)
            self.assertEqual(self.memory_max(loop, tok), recorded if case == "recorded lower" else lower, case)
            q = adm.read_queue(tok)  # back to where it was for the next case
            q["memory_max"] = was
            adm.save(q)
            self.j._set_memory_max(cg, was)

    def test_a_resize_in_the_middle_of_a_cycle_stays_in_the_record(self):
        loop, tid, tok, now = self.running_job()
        q = self.queue_file(tok)  # the cycle's copy, read before the resize
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        limit = self.j.ram_limit(self.settings, 300 * MiB)
        con = loop.adm.db()
        try:
            loop.record_peaks(con, q, final=False)
            con.commit()
        finally:
            con.close()
        self.assertEqual(self.record(tok)["memory_max"], limit)
        self.assertEqual(self.record(tok)["ram_counted"], limit)

    def test_keeping_a_resized_limit_of_a_scope_that_ended_is_quiet(self):
        loop = self.loop()
        gone = os.path.join(self.d.dir, "gone-scope-%d" % time.monotonic_ns())
        loop.scopes_now = {"e" * 32: gone}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            loop.keep_resized_limit({"token": "e" * 32, "resized": [{}], "memory_max": 100 * MiB})
        self.assertEqual(err.getvalue(), "")

    def test_a_job_being_released_is_not_said_to_have_ended(self):
        r = self.submit_sized("true", cpus=1, ram=100 * MiB)
        tid, tok = r["data"]["id"], r["data"]["token"]
        self.addCleanup(self.drop, tid)
        adm = self.j.Admission(self.settings)
        q = adm.read_queue(tok)
        q.update(state="released", released=time.time())  # marked released; pueue has not enqueued it yet
        adm.save(q)
        with self.assertRaises(self.j.JobError) as e:
            self.j.host_resize({"id": tid, "ram": 50 * MiB})
        self.assertIn("is being released by the admission loop now", str(e.exception))

    # -- review 5 -------------------------------------------------------------------------------------------------

    def test_only_a_held_resize_reads_the_hosts_capacity(self):
        loop, tid, tok, now = self.running_job()
        os.environ.update(self.env())  # host_resize runs in this process
        self.addCleanup(os.environ.pop, "JOB_ADMISSION_CONFIG", None)
        calls, real = [], self.j._capacity
        self.j._capacity = lambda m: calls.append(1) or real(m)
        try:
            self.j.host_resize({"id": tid, "ram": 300 * MiB})
        finally:
            self.j._capacity = real
        self.assertEqual(calls, [])  # a running shrink needs no could-it-ever-fit check, so no GPU query

    def test_a_running_shrink_lowers_the_limit_before_the_queue_lock_and_checks_the_job_again_under_it(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        os.environ.update(self.env())
        self.addCleanup(os.environ.pop, "JOB_ADMISSION_CONFIG", None)
        adm = self.j.Admission(self.settings)
        limit = self.j.ram_limit(self.settings, 300 * MiB)
        for meanwhile in (True, False):
            out = {}

            def run():
                try:
                    out["r"] = self.j.host_resize({"id": tid, "ram": 300 * MiB})
                except self.j.JobError as e:
                    out["e"] = str(e)
            fd = adm.lock("queue.lock")  # a submit or a cycle holds the lock
            th = threading.Thread(target=run)
            th.start()
            try:
                for _ in range(200):
                    if self.memory_max(loop, tok) == limit:
                        break
                    time.sleep(0.05)
                lowered = self.memory_max(loop, tok)
                if meanwhile:  # another resize landed while this one waited for the lock
                    q = adm.read_queue(tok)
                    q["size"] = dict(q["size"], cpus=1.5)
                    adm.save(q)
            finally:
                os.close(fd)
            th.join(60)
            self.assertEqual(lowered, limit, "the limit was not lowered before the queue lock")
            if meanwhile:
                self.assertIn("changed while it was being resized", out.get("e", ""), out)
                self.assertEqual(self.memory_max(loop, tok), was)  # put back
                self.assertIsNone(self.record(tok)["resized"])
            else:
                self.assertEqual(out["r"]["memory_max"], limit, out)
                self.assertEqual(self.record(tok)["memory_max"], limit)

    def test_a_running_shrink_waits_while_its_file_cache_is_read_back(self):
        loop, tid, tok, now = self.running_job()
        was = self.memory_max(loop, tok)
        adm = self.j.Admission(self.settings)
        q = adm.read_queue(tok)
        q["refault"] = -1  # its file refault count grew since the loop's last sample
        adm.save(q)
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **self.env())
        self.assertEqual(p.returncode, 205, p.stdout)
        self.assertIn("workingset_refault_file grew", p.stdout)
        self.assertEqual(self.memory_max(loop, tok), was)
        q.pop("refault")
        adm.save(q)
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **self.env())
        self.assertIn("have not been sampled by the admission loop yet", p.stdout)
        self.host.tick()
        loop.cycle()  # the loop samples it again
        p = self.job("resize", "local", str(tid), "--ram", "300Mi", **self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


def _remove_var_tmp_dir(path):
    """Remove a folder a test made with tempfile.mkdtemp(prefix="jobt.", dir="/var/tmp"), and nothing else."""
    if isinstance(path, str) and path.startswith("/var/tmp/jobt.") and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


class PressureMeasure(unittest.TestCase):
    """TASK-003.09: memory pressure outside the jobs' own limits, on a fake cgroup tree (no daemon)."""

    USER = "/user.slice/user-1000.slice/user@1000.service"
    TOK = "ab" * 16

    def setUp(self):
        self.j = load()
        self.tmp = own_tmpdir()
        self.addCleanup(remove_own_dir, self.tmp)
        self.cg = os.path.join(self.tmp, "cgroup")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(self.proc, "pressure"))
        self.write(os.path.join(self.proc, "pressure", "memory"), self.psi_text(22.4))
        self.levels, self.totals = {}, {}  # {cgroup dir: (full avg10, whether its stall time grows)}, {dir: total}
        self.scope = self.cg + self.USER + "/app.slice/agent-pueue-%s.scope" % self.TOK
        self.set("/system.slice", 1.0)
        self.set("/init.scope", 0.0)
        self.set("/user.slice/user-1001.slice", 3.0)
        self.set(self.USER + "/app.slice/pueued.service", 0.5)
        self.set(self.USER + "/session.slice", 0.0)
        self.set(self.scope[len(self.cg):], 0.0)
        self.job(full=30.0, events=7, current=95 * MiB, limit=100 * MiB)
        self.m = self.j.Measure(proc=self.proc, cgroup_root=self.cg, uid=1000)
        self.allocstall(1000)
        self.assertTrue(self.m.global_reclaim())  # the first reading can't tell: the safe side
        self.assertFalse(self.m.global_reclaim())  # from here on, no global reclaim until the counters grow

    def allocstall(self, n):
        """/proc/vmstat with its allocstall_* counters summing to n (global direct reclaim so far)."""
        self.write(os.path.join(self.proc, "vmstat"), "nr_free_pages 1000\nallocstall_dma 0\nallocstall_dma32 0\n"
                   "allocstall_normal %d\nallocstall_movable %d\npgscan_kswapd 5\n" % (n // 2, n - n // 2))

    @staticmethod
    def psi_text(full10, full60=None, total=0):
        full60 = full10 if full60 is None else full60
        return ("some avg10=%.2f avg60=%.2f avg300=0.00 total=%d\nfull avg10=%.2f avg60=%.2f avg300=0.00 total=%d\n"
                % (full10, full60, total, full10, full60, total))

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def set(self, rel, full10, grows=None):
        """A cgroup's full avg10; its stall time grows between samples while it stalls (grows=False: an avg10 still
        decaying after its stall ended)."""
        d = self.cg + rel
        self.levels[d] = (full10, full10 > 0 if grows is None else grows)
        self.totals.setdefault(d, 0)
        self.write(d + "/memory.pressure", self.psi_text(full10, total=self.totals[d]))

    def events(self, rel, n, current=None, limit=None):
        d = self.cg + rel
        self.write(d + "/memory.events", "low 0\nhigh 0\nmax %d\noom 0\noom_kill 0\n" % n)
        if current is not None:
            self.write(d + "/memory.current", "%d\n" % current)
        if limit is not None:
            self.write(d + "/memory.max", "%d\n" % limit)

    def job(self, full, events, current, limit, grows=None):
        rel = self.scope[len(self.cg):] + "/job"
        self.set(rel, full, grows)
        self.events(rel, events, current, limit)

    def measure(self):
        """A sample: the stall time of each stalling cgroup grows first, as the kernel's would over an interval."""
        for d, (full10, grows) in self.levels.items():
            if grows:
                self.totals[d] += int(full10 * 1000) + 1
            try:
                self.write(d + "/memory.pressure", self.psi_text(full10, total=self.totals[d]))
            except PermissionError:
                pass  # made unreadable by the test
        return self.m.pressure({self.TOK: self.scope})

    def test_a_job_at_its_own_limit_is_not_host_pressure(self):
        got = self.measure()  # first sight: it has reached its limit (memory.events max) and is at it now
        self.assertEqual((got["psi"], got["source"], got["where"]), (3.0, "cgroups", "/user.slice/user-1001.slice"))
        self.assertEqual(got["system"], 22.4)
        self.assertEqual(got["jobs"][self.TOK], {"full10": 30.0, "full60": 30.0, "at_limit": True})
        self.job(full=30.0, events=12, current=95 * MiB, limit=100 * MiB)  # reached it again since
        got = self.measure()
        self.assertEqual(got["psi"], 3.0)
        self.assertTrue(got["jobs"][self.TOK]["at_limit"])

    def test_a_job_stalled_below_its_limit_is_host_pressure(self):
        self.job(full=20.0, events=0, current=20 * MiB, limit=100 * MiB)  # global reclaim reaches it
        self.measure()
        got = self.measure()
        self.assertFalse(got["jobs"][self.TOK]["at_limit"])
        self.assertEqual((got["psi"], got["where"]), (20.0, self.scope[len(self.cg):]))
        self.job(full=20.0, events=7, current=95 * MiB, limit=100 * MiB)  # hit its limit long ago, not since
        self.measure()
        got = self.measure()
        self.assertFalse(got["jobs"][self.TOK]["at_limit"])
        self.assertEqual(got["psi"], 20.0)

    def test_pressure_anywhere_outside_the_jobs_counts(self):
        self.job(full=0.0, events=7, current=10 * MiB, limit=100 * MiB)  # the job does not stall
        self.set(self.USER + "/app.slice/pueued.service", 12.0)  # this user's own non-job unit
        got = self.measure()
        self.assertEqual((got["psi"], got["where"]), (12.0, self.USER + "/app.slice/pueued.service"))
        self.set("/system.slice", 15.0)
        self.assertEqual(self.measure()["psi"], 15.0)

    # -- review 5 -------------------------------------------------------------------------------------------------

    def test_a_bursty_thrasher_does_not_leak_in_after_its_burst(self):
        self.measure()  # first sight: at its limit
        self.job(full=30.0, events=12, current=95 * MiB, limit=100 * MiB)
        self.assertTrue(self.measure()["jobs"][self.TOK]["at_limit"])
        # the burst is over: no new limit events and no new stall time, while its avg10 still decays from 30%
        self.job(full=30.0, events=12, current=60 * MiB, limit=100 * MiB, grows=False)
        got = self.measure()
        self.assertFalse(got["jobs"][self.TOK]["at_limit"])
        self.assertEqual((got["psi"], got["where"]), (3.0, "/user.slice/user-1001.slice"))

    def test_a_member_stalled_at_its_own_limit_still_counts(self):
        # Only jobs are ever left out. A cgroup outside the jobs at its own limit may be hiding a real shortage (its
        # count is hierarchical, so one container at its limit would hide all of system.slice), so it counts.
        self.job(full=0.0, events=7, current=10 * MiB, limit=100 * MiB)  # the job does not stall
        self.set("/students.slice", 50.0)
        self.events("/students.slice", 3, current=int(9.9 * GB), limit=10 * GB)
        self.measure()
        self.events("/students.slice", 9)  # it reached its limit again since the last sample
        got = self.measure()
        self.assertEqual((got["psi"], got["source"], got["cgroups"]), (22.4, "system", 50.0))
        self.set("/students.slice", 0.0)

    def test_a_real_shortage_is_not_hidden_by_one_container_at_its_limit(self):
        # review 6 B1: a host-wide shortage (40%) while one container under system.slice touches its MemoryMax
        self.write(os.path.join(self.proc, "pressure", "memory"), self.psi_text(40.0))
        self.job(full=0.0, events=0, current=10 * MiB, limit=100 * MiB)
        self.set("/user.slice/user-1001.slice", 0.0)
        self.set(self.USER + "/app.slice/pueued.service", 0.0)
        self.set("/system.slice", 40.0)
        self.events("/system.slice/docker-x.scope", 0, current=10 ** 9, limit=10 ** 9)
        self.events("/system.slice", 5)
        self.measure()
        self.events("/system.slice", 6)
        got = self.measure()
        self.assertEqual((got["psi"], got["where"]), (40.0, "/system.slice"))

    def test_in_global_reclaim_nothing_is_left_out(self):
        # review 6 B3: a job at its own limit while the whole host reclaims (allocstall grows): the system-wide figure
        self.write(os.path.join(self.proc, "pressure", "memory"), self.psi_text(40.0))
        for r in ("/system.slice", "/user.slice/user-1001.slice", self.USER + "/app.slice/pueued.service"):
            self.set(r, 0.0)
        self.job(full=40.0, events=100, current=99 * MiB, limit=100 * MiB)
        self.measure()
        self.job(full=40.0, events=130, current=99 * MiB, limit=100 * MiB)
        got = self.measure()
        self.assertTrue(got["jobs"][self.TOK]["at_limit"])
        self.assertEqual((got["psi"], got["global"]), (0.0, False))  # no global reclaim: its own limit, left out
        self.job(full=40.0, events=160, current=99 * MiB, limit=100 * MiB)
        self.allocstall(1500)
        got = self.measure()
        self.assertTrue(got["jobs"][self.TOK]["at_limit"])
        self.assertEqual((got["psi"], got["source"], got["global"], got["cgroups"]), (40.0, "system", True, None))
        room = self.j.compute_room(settings(self.j, self.tmp), [dict(
            sample(100), psi=got["psi"], psi_source=got["source"], psi_system=40.0, psi_global=True)], [], [], 100)
        ok, why = self.j.fits(settings(self.j, self.tmp, memory_pressure_max_pct=5), room, held())
        self.assertFalse(ok)
        self.assertIn("the host is in global reclaim", why)

    def test_global_reclaim_is_assumed_when_it_cannot_be_told(self):
        os.unlink(os.path.join(self.proc, "vmstat"))
        self.assertTrue(self.m.global_reclaim())
        self.write(os.path.join(self.proc, "vmstat"), "nr_free_pages 1000\n")  # no allocstall counters
        self.assertTrue(self.m.global_reclaim())
        self.allocstall(7)
        self.assertTrue(self.m.global_reclaim())  # first reading after a gap
        self.assertFalse(self.m.global_reclaim())
        got = self.measure()  # the job is at its own limit: left out outside global reclaim
        self.assertEqual(got["psi"], 3.0)
        os.unlink(os.path.join(self.proc, "vmstat"))
        self.assertEqual((self.measure()["psi"], self.measure()["source"]), (22.4, "system"))

    def test_the_figure_is_never_above_the_system_wide_one(self):
        self.set("/system.slice", 40.0)
        got = self.measure()
        self.assertEqual((got["psi"], got["source"], got["cgroups"], got["cg_where"]),
                         (22.4, "system", 40.0, "/system.slice"))
        room = self.j.compute_room(settings(self.j, self.tmp), [dict(
            sample(100), psi=got["psi"], psi_source=got["source"], psi_system=22.4, psi_cgroups=40.0,
            psi_cg_where="/system.slice")], [], [], 100)
        ok, why = self.j.fits(settings(self.j, self.tmp, memory_pressure_max_pct=5), room, held())
        self.assertFalse(ok)
        self.assertIn("system-wide, lower than the highest of the cgroups leaving out jobs stalled against their own "
                      "RAM limits, 40.0% in /system.slice", why)

    def test_a_cgroup_removed_between_open_and_read_is_gone_not_unreadable(self):
        import errno
        real = self.m.cgroup_pressure

        def pressure(path):
            if path.endswith("user-1001.slice"):
                raise OSError(errno.ENODEV, "No such device")
            return real(path)
        self.m.cgroup_pressure = pressure
        got = self.measure()
        self.assertEqual((got["source"], got["unreadable"], got["psi"]), ("cgroups", 0, 1.0))

    def test_the_cache_a_lower_limit_drops_leaves_hot_mapped_pages(self):
        rel = "/user.slice/x.scope"
        self.write(self.cg + rel + "/memory.current", "%d\n" % (10 * GB))
        self.write(self.cg + rel + "/memory.stat", "anon %d\nactive_file %d\ninactive_file %d\nfile_mapped %d\n"
                   "workingset_refault_file 17\n" % (2 * GB, 6 * GB, 2 * GB, 5 * GB))
        # all file cache is 8 GB; of it, inactive (2 GB) and the active pages not mapped (1 GB) can go
        self.assertEqual(self.m.cgroup_mem(rel), (2 * GB, 3 * GB, 10 * GB, 17))
        self.assertEqual(self.j._droppable_cache({"active_file": "1", "inactive_file": "2", "file_mapped": "5"}), 2)



    def test_an_unreadable_cgroup_falls_back_to_the_system_wide_figure(self):
        if os.getuid() == 0:
            self.skipTest("root reads every file")
        os.chmod(self.cg + "/user.slice/user-1001.slice/memory.pressure", 0)
        got = self.measure()
        self.assertEqual((got["psi"], got["source"], got["unreadable"]), (22.4, "system", 1))
        self.assertEqual(got["unreadable_paths"], ["/user.slice/user-1001.slice"])
        room = self.j.compute_room(settings(self.j, self.tmp), [dict(
            sample(100), psi=got["psi"], psi_source=got["source"], psi_unreadable=1, psi_system=22.4,
            psi_unreadable_paths=got["unreadable_paths"])], [], [], 100)
        ok, why = self.j.fits(settings(self.j, self.tmp, memory_pressure_max_pct=5), room, held())
        self.assertFalse(ok)
        self.assertIn("system-wide: 1 cgroup(s) could not be read: /user.slice/user-1001.slice", why)

    def test_no_pressure_files_means_the_system_wide_figure(self):
        m = self.j.Measure(proc=self.proc, cgroup_root=os.path.join(self.tmp, "empty"), uid=1000)
        got = m.pressure({})
        self.assertEqual((got["psi"], got["source"]), (22.4, "system"))

    def test_an_undersize_threshold_setting_is_checked(self):
        self.assertEqual(settings(self.j, self.tmp)["undersize_stall_pct"], 10.0)
        with self.assertRaises(self.j.JobError):
            settings(self.j, self.tmp, undersize_stall_pct=150)


class ReviewThreeRules(unittest.TestCase):
    """Review 3 of the admission work (no daemon)."""

    def setUp(self):
        self.j = load()
        self.tmp = own_tmpdir()
        self.addCleanup(remove_own_dir, self.tmp)
        self.s = settings(self.j, self.tmp, ram_hard_factor=1.5)

    def test_the_floor_takes_file_cache_off_a_sampled_peak_under_its_limit(self):
        # limit 80 GB; memory.peak 60 GB, seen by sampling (memory.current was sampled at 60 GB), 40 GB of it file
        # cache it still holds: the cache comes off, and the sampled RAM peak times resize_margin is the floor
        floor = self.j._resize_floor(self.s, 26.5 * GB, 20 * GB, 60 * GB, 40 * GB, sampled_current=60 * GB,
                                     limit=80 * GB)
        self.assertAlmostEqual(floor, 26.5 * GB * 1.25)
        n = self.j._resize_suggestion(self.s, 26.5 * GB, 20 * GB, 60 * GB, 40 * GB, sampled_current=60 * GB,
                                      limit=80 * GB)
        self.assertEqual(self.j._size_arg(n), "33.2G")
        self.assertGreaterEqual(self.j.ram_limit(self.s, self.j._parse_size("33.2G")), floor)
        # anonymous growth between samples (little cache now) keeps its floor at the kernel's peak
        self.assertGreaterEqual(self.j._resize_floor(self.s, 26.5 * GB, 26 * GB, 60 * GB, 1 * GB, limit=80 * GB),
                                59 * GB)

    def test_a_job_whose_peak_reached_its_limit_cannot_be_shrunk(self):
        # sized 64 GB (limit 80 GB), sampled peak 26.5 GB, memory.peak at its 80 GB limit, 53 GB of it file cache now:
        # what filled the limit at its peak is unknown, so the floor is the whole memory.peak
        for limit in (80 * GB, 85 * GB, None):  # at it, at 94% of it, or not known
            self.assertEqual(self.j._resize_floor(self.s, 26.5 * GB, 26 * GB, 80 * GB, 53 * GB,
                                                  sampled_current=80 * GB, limit=limit), 80 * GB, limit)

    def test_review_6_a_hidden_spike_under_a_cache_filled_limit_is_not_given_away(self):
        # limit 100 GB, always cache-filled; anon sampled at 5 GB; an unsampled 40 GB anonymous spike squeezed the
        # cache out and it refilled since: memory.peak and every sampled memory.current are 100 GB
        floor = self.j._resize_floor(self.s, 5 * GB, 5 * GB, 100 * GB, 95 * GB, sampled_current=100 * GB,
                                     limit=100 * GB)
        self.assertGreaterEqual(floor, 40 * GB)

    def test_dirty_and_writeback_pages_are_not_droppable(self):
        self.assertEqual(self.j._droppable_cache({"inactive_file": 10, "active_file": 6, "file_mapped": 2,
                                                  "file_dirty": 3, "file_writeback": 1}), 10)
        self.assertEqual(self.j._droppable_cache({"inactive_file": 1, "file_dirty": 5}), 0)

    def oversize_loop(self):
        class M:  # enough of Measure for a Loop that is never cycled
            proc, cg = "/nonexistent", "/nonexistent"

            def disk_of(self, p):
                return None

            def meminfo(self):
                return {"MemTotal": 64 * GB}

            def gpus(self):
                return []

            def threads(self):
                return 8

            def cpu(self, now):
                return None, None

            def disks(self, now, names):
                return {}
        loop = self.j.Loop(self.s, measure=M(), clock=lambda: 10000.0)
        loop.alerts = []
        loop.adm.alert = lambda q, kind, msg, extra=None: loop.alerts.append((kind, msg, extra))
        return loop

    def oversized_job(self, **kw):
        q = {"token": "f" * 32, "task": "T-o", "size": {"cpus": 1, "ram": 10 * GB, "gpus": 0, "gpu_mem": 0},
             "started": 1000.0, "released": 1000.0, "memory_max": self.j.ram_limit(self.s, 10 * GB), "gpus": [],
             "stall60": 0.0, "refault_grew": False}
        q.update(kw)
        return q

    def test_a_rising_ram_trend_is_part_of_the_oversize_measure(self):
        loop = self.oversize_loop()
        p = {"ram": 1 * GB, "n": 10}
        u = {"ram": GB, "cache": 0, "limit": None}
        q = self.oversized_job()
        loop.check_oversize(q, 10000.0, "Running", p, u)
        self.assertEqual(q["oversize"]["suggest"], "1.3G")  # its peak times margins.ram, up to 0.1 GB
        # its progress says it will hold 10 GB at its end: not oversized
        q = self.oversized_job(progress=[[1, 10, 100, 1 * GB], [2, 20, 100, 2 * GB], [3, 30, 100, 3 * GB]])
        loop.alerts.clear()
        loop.check_oversize(q, 10000.0, "Running", p, u)
        self.assertIsNone(q["oversize"])
        self.assertEqual(loop.alerts, [])
        # on course for 3.2 GB: still oversized, and the suggestion covers the trend, not the peak so far
        q = self.oversized_job(progress=[[1, 10, 100, GB], [2, 20, 100, 1.25 * GB], [3, 30, 100, 1.5 * GB]])
        loop.check_oversize(q, 10000.0, "Running", p, u)
        self.assertGreaterEqual(self.j._parse_size(q["oversize"]["suggest"]), 3.25 * GB * 1.25 - 1e8)
        self.assertIn("its progress trend projects", loop.alerts[-1][1])

    def test_no_oversize_alert_when_a_raised_hard_factor_leaves_nothing_to_accept(self):
        loop = self.oversize_loop()
        # it runs under a 4 GB limit (the factor was lower at its release); the smallest size covering its floor,
        # 3.8 GB, gives 5.7 GB at today's factor of 1.5, over that limit, so job resize would refuse any shrink
        q = self.oversized_job(memory_max=4 * GB)
        loop.check_oversize(q, 10000.0, "Running", {"ram": 3 * GB, "n": 10}, {"ram": GB, "cache": 0, "limit": 4 * GB})
        self.assertIsNone(q["oversize"])
        self.assertEqual(loop.alerts, [])

    def test_the_like_source_text_never_raises(self):
        class Broken:
            def db(self):
                raise sqlite3.OperationalError("database is locked")
        self.assertEqual(self.j._like_source_text(Broken(), "a" * 32), "?")

    def test_like_last_and_the_held_floor_prefer_the_same_session(self):
        adm = self.j.Admission(self.s)
        con = adm.db()
        for tok, session, ended, peak in (("1" * 32, "mine", 1000.0, 2 * GB), ("2" * 32, "other", 2000.0, 9 * GB)):
            con.execute("INSERT INTO jobs (token, task, session, how, exit, ended, peak_ram, peak_cpu, peak_samples, "
                        "gpus, pueue_id) VALUES (?, 'T-s', ?, 'exit', 0, ?, ?, 1.0, 10, '[]', 7)",
                        (tok, session, ended, peak))
        con.commit()
        self.assertEqual(adm.find_last(con, "T-s", "mine")["token"], "1" * 32)  # not the newer run of another session
        self.assertEqual(adm.find_last(con, "T-s", "new")["token"], "2" * 32)  # none of its own: the newest of any
        self.assertIn("of session mine", self.j._like_source_text(adm, "1" * 32))
        cap = {"mem_total": 64 * GB, "threads": 8, "gpus": []}
        q = {"token": "3" * 32, "task": "T-s", "session": "mine", "constraints": {}}
        new = {"cpus": 1, "ram": 3 * GB, "gpus": 0, "gpu_mem": 0, "gpu_share": 0, "disk_mb_s": 0}
        change, used = self.j._resize_held(self.s, con, q, new, cap)  # over its own run's 2 GB, under the other's 9
        self.assertEqual(used["session"], "mine")
        with self.assertRaises(self.j.JobError) as e:
            self.j._resize_held(self.s, con, q, dict(new, ram=GB), cap)
        self.assertIn("session mine", str(e.exception))
        con.close()

class ReviewFiveRules(unittest.TestCase):
    """Review 5 of the memory-pressure and peak changes (no daemon)."""

    setUp = ReviewThreeRules.setUp
    oversize_loop = ReviewThreeRules.oversize_loop
    oversized_job = ReviewThreeRules.oversized_job

    def test_a_spike_between_samples_keeps_the_whole_kernel_peak_as_the_floor(self):
        # memory.peak 80 GB, but the highest memory.current the loop sampled was 30 GB: the peak came between
        # samples, of unknown make-up (an anonymous spike that squeezed the cache out, the cache refilled since)
        self.assertEqual(self.j._resize_floor(self.s, 10 * GB, 10 * GB, 80 * GB, 53 * GB, sampled_current=30 * GB,
                                              limit=100 * GB), 80 * GB)
        # within 2% of the sampled peak, and under 90% of its limit: seen by sampling, so its cache comes off
        self.assertEqual(self.j._resize_floor(self.s, 10 * GB, 10 * GB, 80 * GB, 53 * GB, sampled_current=79 * GB,
                                              limit=100 * GB), 27 * GB)
        n = self.j._resize_suggestion(self.s, 10 * GB, 10 * GB, 80 * GB, 53 * GB, sampled_current=30 * GB,
                                      limit=100 * GB)
        self.assertGreaterEqual(self.j.ram_limit(self.s, self.j._parse_size(self.j._size_arg(n))), 80 * GB)

    def test_no_oversize_alert_while_it_stalls_or_its_file_cache_is_read_back(self):
        p, u = {"ram": 1 * GB, "n": 10, "current": 2 * GB}, {"ram": GB, "need": GB, "cache": 0, "limit": None,
                                                             "current": 2 * GB}
        for blocked in ({"stall60": 2.5}, {"refault_grew": True}):
            loop = self.oversize_loop()
            q = self.oversized_job(**blocked)
            loop.check_oversize(q, 10000.0, "Running", p, u)
            self.assertIsNone(q["oversize"], blocked)
            self.assertEqual(loop.alerts, [])
        loop = self.oversize_loop()
        q = self.oversized_job(stall60=0.5, refault_grew=False)
        loop.check_oversize(q, 10000.0, "Running", p, u)
        self.assertEqual([a[0] for a in loop.alerts], ["oversize"])
        self.assertIn("stalled reclaiming memory in its own cgroup", self.j._shrink_blocked(2.5, False))
        self.assertIn("workingset_refault_file grew", self.j._shrink_blocked(0.0, True))
        self.assertIsNone(self.j._shrink_blocked(1.0, False))

    def test_the_loop_keeps_whether_refaults_grew(self):
        loop = self.oversize_loop()
        q = {}
        loop.track_memory(q, {"refault": 10, "psi": {"full60": 0.2}})
        self.assertEqual((q["refault"], q["refault_grew"], q["stall60"]), (10, None, 0.2))  # first: not known yet
        loop.track_memory(q, {"refault": 14, "psi": None})
        self.assertEqual((q["refault"], q["refault_grew"], q["stall60"]), (14, True, None))
        loop.track_memory(q, {"refault": 14, "psi": {"full60": 0.0}})
        self.assertIs(q["refault_grew"], False)
        loop.track_memory(q, {"refault": None, "psi": {"full60": 0.0}})  # a kernel without the counter
        self.assertIsNone(q["refault_grew"])
        loop.track_memory(q, None)  # not measured this cycle
        self.assertEqual((q["refault_grew"], q["stall60"]), (None, None))

    def test_a_shrink_and_the_oversize_alert_fail_closed_when_its_stall_cannot_be_read(self):
        self.assertIn("could not be read", self.j._shrink_blocked(None, False))
        self.assertIn("could not be read", self.j._shrink_blocked(0.0, None))
        p, u = {"ram": 1 * GB, "n": 10, "current": 2 * GB}, {"ram": GB, "need": GB, "cache": 0, "limit": None,
                                                             "current": 2 * GB}
        for unknown in ({"stall60": None}, {"refault_grew": None}):
            loop = self.oversize_loop()
            q = self.oversized_job(**unknown)
            loop.check_oversize(q, 10000.0, "Running", p, u)
            self.assertIsNone(q["oversize"], unknown)
            self.assertEqual(loop.alerts, [])

    def test_the_undersized_alert_waits_for_a_host_with_ram_to_spare(self):
        u = {"ram": GB, "cache": 0, "files": 0, "limit": 2 * GB,
             "psi": {"full10": 30.0, "full60": 30.0, "at_limit": True}}
        quiet = {"psi_global": False}
        for last, fires in ((dict(quiet, psi_outside=2.0, avail=50 * GB), False),   # something outside the jobs stalls
                            (dict(quiet, psi_outside=None, avail=50 * GB), False),  # not known
                            (dict(quiet, psi_outside=0.0, avail=GB // 2), False),   # MemAvailable under the headroom
                            ({"psi_global": True, "psi_outside": 0.0, "avail": 50 * GB}, False),  # global reclaim
                            ({"psi_outside": 0.0, "avail": 50 * GB}, False),        # not known to be out of it
                            (dict(quiet, psi_outside=0.0, avail=50 * GB), True)):
            loop = self.oversize_loop()
            loop.samples = [dict(sample(9999), **last)]
            q = self.oversized_job(grow_blocked=9990.0)  # the loop found no room to raise its limit
            loop.check_undersize(q, 10000.0, "Running", u)
            self.assertEqual([a[0] for a in loop.alerts], ["undersized"] if fires else [], last)
            self.assertEqual(q["undersized"] is not None, fires)
        loop = self.oversize_loop()  # with room to raise its limit, the loop grows it instead (auto_grow): no ALERT
        loop.samples = [dict(sample(9999), **dict(quiet, psi_outside=0.0, avail=50 * GB))]
        q = self.oversized_job()
        loop.check_undersize(q, 10000.0, "Running", u)
        self.assertEqual(loop.alerts, [])



class SoftLimitRules(unittest.TestCase):
    """TASK-018 review 2: soft limits (no daemon)."""

    setUp = ReviewThreeRules.setUp
    oversize_loop = ReviewThreeRules.oversize_loop

    def grow_loop(self, room):
        loop = self.oversize_loop()
        loop.scopes_now = {"t" * 32: "/nonexistent"}
        loop.room = {"pressure_ok": True, "ram": {"room": room}}
        loop.calls = []
        loop._soft_limit = lambda con, q, high, how: loop.calls.append((how, high)) or True
        loop._counted = lambda q, now: {"ram_counted": max(q.get("memory_high") or 0, q["_cur"])}
        loop.adm.save = lambda q: None
        return loop

    def test_a_soft_limit_just_under_the_hard_limit_is_lifted(self):
        loop = self.grow_loop(200 * GB)
        q = {"token": "t" * 32, "memory_high": 96 * GB, "_cur": 95 * GB, "constraints": {}, "size": {"cpus": 1}}
        use = {"t" * 32: {"limit": 100 * GB, "need": 95 * GB, "psi": {"full60": 50.0, "at_limit": True}}}
        orig = self.j._task_status
        self.j._task_status = lambda t: "Running"
        try:
            con = loop.adm.db()
            self.addCleanup(con.close)
            loop.auto_grow(con, 1000.0, [q], {"t" * 32: {}}, use)
        finally:
            self.j._task_status = orig
        self.assertEqual(loop.calls, [("grow", None)])
        self.assertIsNone(q.get("grow_blocked"))
        self.assertTrue(loop.throttled)  # the next cycle comes at the pilot interval

    def test_a_job_held_at_its_soft_limit_doubles_it_when_there_is_room(self):
        loop = self.grow_loop(200 * GB)
        q = {"token": "t" * 32, "memory_high": 10 * GB, "_cur": 10 * GB, "constraints": {}, "size": {"cpus": 1}}
        use = {"t" * 32: {"limit": 100 * GB, "need": 9.9 * GB, "psi": {"full10": 40.0, "full60": 5.0, "at_limit": True}}}
        orig = self.j._task_status
        self.j._task_status = lambda t: "Running"
        try:
            con = loop.adm.db()
            self.addCleanup(con.close)
            loop.auto_grow(con, 1000.0, [q], {"t" * 32: {}}, use)
        finally:
            self.j._task_status = orig
        self.assertEqual(loop.calls[0][0], "grow")
        self.assertAlmostEqual(loop.calls[0][1], 20 * GB, delta=4096)
        self.assertTrue(q.get("soft_capped"))  # its peak is no --like source now

    def test_a_job_that_only_trims_file_cache_at_its_soft_limit_is_not_held(self):
        loop = self.grow_loop(200 * GB)
        q = {"token": "t" * 32, "memory_high": 10 * GB, "_cur": 10 * GB, "constraints": {}, "size": {"cpus": 1}}
        use = {"t" * 32: {"limit": 100 * GB, "need": 2 * GB, "psi": {"full10": 0.0, "full60": 0.1, "at_limit": True}}}
        orig = self.j._task_status
        self.j._task_status = lambda t: "Running"
        try:
            con = loop.adm.db()
            self.addCleanup(con.close)
            loop.auto_grow(con, 1000.0, [q], {"t" * 32: {}}, use)
        finally:
            self.j._task_status = orig
        self.assertEqual(loop.calls, [])
        self.assertFalse(q.get("soft_capped"))
        self.assertFalse(loop.throttled)

    def test_the_soft_limit_counts_as_its_own_limit_in_its_stall(self):
        d = own_tmpdir()
        self.addCleanup(remove_own_dir, d)
        with open(os.path.join(d, "memory.events"), "w") as f:
            f.write("low 0\nhigh 7\nmax 2\noom 0\n")
        m = self.j.Measure(proc=d, psi_root=d)
        self.assertEqual(m.limit_events(d), 9)

    def test_a_job_whose_cgroup_lost_its_soft_limit_is_counted_at_its_hard_limit(self):
        d = own_tmpdir()
        self.addCleanup(remove_own_dir, d)
        os.makedirs(os.path.join(d, "job"))
        for name, v in (("memory.max", 8 * GB), ("memory.high", "max"), ("memory.current", GB)):
            with open(os.path.join(d, "job", name), "w") as f:
                f.write(str(v))
        loop = self.oversize_loop()
        loop.scopes_now = {"t" * 32: d}
        q = {"token": "t" * 32, "released": 1.0, "size": {"cpus": 1, "ram": 2 * GB}, "memory_max": 8 * GB,
             "ram_counted": 2 * GB, "memory_high": 2 * GB, "constraints": {}}
        self.assertEqual(loop._counted(q, 10.0)["ram_counted"], 8 * GB)
        self.assertNotIn("memory_high", q)

    def test_like_refuses_a_pilot_held_at_its_soft_limit(self):
        adm = self.j.Admission(self.s)
        con = adm.db()
        try:
            adm.record(con, {"token": "a" * 32, "task": "T-s", "session": "x", "ended": 100.0, "how": "pilot time limit",
                             "peak_samples": 10, "peak_ram": GB, "soft_capped": 1})
            con.commit()
            self.assertIsNone(adm.find_last(con, "T-s", "x"))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
