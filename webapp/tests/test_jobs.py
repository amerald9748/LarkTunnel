# -*- coding: utf-8 -*-
"""Unit tests for sync_jobs (background jobs + progress) and the progress
plumbing through plan(). Offline.

Run:  python -m unittest discover webapp/tests -v"""
import os
import sys
import time
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_jobs                 # noqa: E402
import appointment_sync as sync  # noqa: E402
import lark_client as lark       # noqa: E402
from test_planner import (Fixture, fake_field_meta, fake_table_id,  # noqa: E402
                          make_31)


def wait_done(job_id, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = sync_jobs.get(job_id)
        if j["state"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


class TestJobLifecycle(unittest.TestCase):
    def test_result_and_progress(self):
        def runner(progress):
            progress(stage="a", done=1, total=3, current="x")
            progress(stage="b", done=3)
            return {"answer": 42}
        job = sync_jobs.start("plan", runner)
        # a fast runner may already have finished when start() snapshots
        self.assertIn(job["state"], ("running", "done"))
        done = wait_done(job["id"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["stage"], "完成")
        self.assertEqual(done["result"], {"answer": 42})
        self.assertEqual(done["done"], 3)
        self.assertGreaterEqual(done["elapsed"], 0)

    def test_error_captured_not_lost(self):
        def runner(progress):
            raise lark.LarkError("boom 出错")
        job = sync_jobs.start("plan", runner)
        done = wait_done(job["id"])
        self.assertEqual(done["state"], "error")
        self.assertIn("boom", done["error"])
        self.assertNotIn("result", done)

    def test_unknown_job(self):
        self.assertIsNone(sync_jobs.get("nope"))

    def test_result_only_when_finished(self):
        release = threading.Event()

        def runner(progress):
            release.wait(2)
            return {"late": True}
        job = sync_jobs.start("plan", runner)
        snap = sync_jobs.get(job["id"])
        self.assertEqual(snap["state"], "running")
        self.assertNotIn("result", snap)
        release.set()
        self.assertEqual(wait_done(job["id"])["result"], {"late": True})


class TestSingleFlight(unittest.TestCase):
    def test_second_commit_refused_while_first_runs(self):
        gate = threading.Event()

        def slow(progress):
            gate.wait(2)
            return {}
        first = sync_jobs.start("commit", slow)
        try:
            with self.assertRaises(sync_jobs.Busy):
                sync_jobs.start("commit", lambda p: {})
            # plan jobs are NOT blocked by a running commit
            plan_job = sync_jobs.start("plan", lambda p: {})
            wait_done(plan_job["id"])
        finally:
            gate.set()
        wait_done(first["id"])
        # after the first finishes, commits are accepted again
        again = sync_jobs.start("commit", lambda p: {})
        self.assertEqual(wait_done(again["id"])["state"], "done")

    def test_latest_prefers_running(self):
        gate = threading.Event()
        done_job = sync_jobs.start("plan", lambda p: {})
        wait_done(done_job["id"])
        running = sync_jobs.start("plan", lambda p: (gate.wait(2), {})[1])
        try:
            self.assertEqual(sync_jobs.latest()["id"], running["id"])
        finally:
            gate.set()
        wait_done(running["id"])


class TestPlanProgress(unittest.TestCase):
    """plan() must stream per-row progress and keep input order under the
    parallel executor."""

    def setUp(self):
        self.fx = Fixture()
        self.patches = [
            mock.patch.object(sync, "_search", self.fx.search),
            mock.patch.object(sync, "_batch_get", self.fx.batch_get),
            mock.patch.object(lark, "field_meta", fake_field_meta),
            mock.patch.object(lark, "table_id", fake_table_id),
            mock.patch.object(lark, "ENV", "prod"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_progress_ticks_and_row_order(self):
        n = 6
        self.fx.rows31 = [make_31(f"r{i}", awb=f"OOCU902071{i}B") for i in range(n)]
        text = "\n".join(f"OOCU902071{i}B YVR2 4 141" for i in range(n))
        ticks = []
        lock = threading.Lock()

        def progress(**kw):
            with lock:
                ticks.append(dict(kw))
        r = sync.plan("VAST", text, progress=progress)
        # order preserved despite parallel planning
        self.assertEqual([row["parsed"]["awb"] for row in r["rows"]],
                         [f"OOCU902071{i}B" for i in range(n)])
        dones = [t["done"] for t in ticks if t.get("done") is not None]
        self.assertIn(n, dones)                        # reached N/N
        self.assertEqual(sorted(set(dones))[-1], n)
        totals = {t.get("total") for t in ticks if t.get("total")}
        self.assertEqual(totals, {n})
        stages = [t["stage"] for t in ticks if t.get("stage")]
        self.assertTrue(any("逐行核对" in s for s in stages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
