# -*- coding: utf-8 -*-
"""Unit tests for the FAST commit path — plan-time record snapshots
(`observed`), `_recheck`, `_plan_reusable`, and commit(cached_plan=…).
Offline, built on the writing fixture from test_commit.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appointment_sync as sync  # noqa: E402
import lark_client as lark       # noqa: E402
from test_planner import make_31, make_56, make_trip, T31, T56, T54  # noqa: E402
from test_commit import CommitCase, LINE_A, LINE_B                    # noqa: E402


class FastCommitCase(CommitCase):
    def plan(self, text):
        return sync.plan("VAST", text)

    def commit(self, text, planned, cached=True):
        return sync.commit("VAST", text, self.approve_all(planned), "prod",
                           cached_plan=planned if cached else None)

    def n_full_plans(self):
        """The full re-plan searches 3.1; the fast path never does."""
        return sum(1 for c in self.fx.search_calls if c == T31) \
            if hasattr(self.fx, "search_calls") else None


class TestSnapshots(FastCommitCase):
    def test_plan_records_observed_state(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        r = self.plan(LINE_A)["rows"][0]
        obs = r["observed"]
        self.assertEqual(obs["31"]["r31a"]["actual"], "")
        self.assertEqual(obs["31"]["r31a"]["links"], [])
        self.assertEqual(obs["56"]["r56a"]["isa"], 9903350996)
        self.assertEqual(obs["56"]["r56a"]["trips"], [])
        self.assertIn("planned_at", self.plan(LINE_A))
        self.assertEqual(self.plan(LINE_A)["text_sha"], sync._text_sha(LINE_A))

    def test_4a_snapshot_includes_trip_and_linked_appointment(self):
        self.fx.rows31.append(make_31(actual="4", plan_links=["trip1"]))
        self.fx.rows56.append(make_56(isa=9903350996, time="2026/07/30 10:00",
                                      trip_links=["trip1"]))
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        r = self.plan(LINE_A)["rows"][0]
        self.assertEqual(r["observed"]["5x"]["trip1"]["isa"], ["r56a"])
        self.assertEqual(r["observed"]["5x"]["trip1"]["inv"], ["r31a"])
        self.assertEqual(r["observed"]["56"]["r56a"]["time"], "2026/07/30 10:00")


class TestReusable(FastCommitCase):
    def setUp(self):
        super().setUp()
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        self.planned = self.plan(LINE_A)

    def test_fresh_same_text_is_reusable(self):
        self.assertTrue(sync._plan_reusable(self.planned, "VAST", LINE_A))

    def test_text_change_env_change_age_break_reuse(self):
        self.assertFalse(sync._plan_reusable(self.planned, "VAST", LINE_A + "\nX"))
        self.assertFalse(sync._plan_reusable(self.planned, "BESTAR", LINE_A))
        old = dict(self.planned, planned_at=time.time() - sync.PLAN_REUSE_TTL - 1)
        self.assertFalse(sync._plan_reusable(old, "VAST", LINE_A))
        with mock.patch.object(lark, "ENV", "dev"):
            self.assertFalse(sync._plan_reusable(self.planned, "VAST", LINE_A))
        self.assertFalse(sync._plan_reusable(None, "VAST", LINE_A))
        legacy = dict(self.planned, rows=[{k: v for k, v in r.items() if k != "observed"}
                                          for r in self.planned["rows"]])
        self.assertFalse(sync._plan_reusable(legacy, "VAST", LINE_A))


class TestFastCommit(FastCommitCase):
    def test_fast_path_writes_without_replanning(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        with mock.patch.object(sync, "plan", side_effect=AssertionError("re-plan ran")):
            res = self.commit(LINE_A, planned)
        self.assertEqual(res["recheck"]["mode"], "fast")
        self.assertEqual(res["recheck"]["changed"], 0)
        self.assertGreaterEqual(res["recheck"]["records"], 2)
        row = res["rows"][0]
        self.assertTrue(row["commit"]["done"])
        self.assertTrue(row["commit"]["verified"])
        kinds = [c[0] for c in self.fx.calls]
        self.assertIn("create", kinds)             # 出库计划 created
        self.assertIn("update", kinds)             # 3.1 filled + linked
        # the cached plan handed in was NOT mutated (job store keeps it clean)
        self.assertNotIn("commit", planned["rows"][0])

    def test_changed_record_is_skipped_and_untouched(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        # someone filled 实际板数 between 预检 and 执行
        self.fx.rows31[0]["fields"]["实际板数"] = [{"text": "7"}]
        res = self.commit(LINE_A, planned)
        self.assertEqual(res["recheck"]["mode"], "fast")
        self.assertEqual(res["recheck"]["changed"], 1)
        c = res["rows"][0]["commit"]
        self.assertFalse(c["done"])
        self.assertIn("情况已变化", c["skipped"])
        self.assertIn("实际板数", c["skipped"])
        self.assertEqual(self.fx.calls, [])        # nothing written at all

    def test_appointment_gained_a_plan_meanwhile(self):
        """Another batch created this ISA's 出库计划 after our 预检: the 5.6
        link snapshot differs -> row refused instead of creating a 2nd plan."""
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        self.fx.trips["tripX"] = make_trip(inv_ids=[], isa_ids=["r56a"])
        self.fx.rows56[0]["fields"]["5.4 出库计划 温哥华"] = {"link_record_ids": ["tripX"]}
        res = self.commit(LINE_A, planned)
        self.assertEqual(res["recheck"]["changed"], 1)
        self.assertIn("出库计划关联", res["rows"][0]["commit"]["skipped"])
        self.assertEqual([c[0] for c in self.fx.calls], [])

    def test_missing_record_counts_as_changed(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        self.fx.rows31.clear()
        res = self.commit(LINE_A, planned)
        self.assertIn("已不存在", res["rows"][0]["commit"]["skipped"])
        self.assertEqual(self.fx.calls, [])

    def test_only_changed_rows_are_refused_in_a_group(self):
        self.fx.rows31.append(make_31(rid="r31a", awb="OOCU9020713B", boxes=141))
        self.fx.rows31.append(make_31(rid="r31b", awb="TCNU4251020B", est=1.0, boxes=4))
        self.fx.rows56.append(make_56(isa=9903350996))
        text = LINE_A + "\n" + LINE_B
        planned = self.plan(text)
        self.fx.rows31[1]["fields"]["实际板数"] = [{"text": "1"}]     # row B moved
        res = self.commit(text, planned)
        a, b = res["rows"]
        self.assertTrue(a["commit"]["done"])
        self.assertIn("情况已变化", b["commit"]["skipped"])
        self.assertEqual(res["recheck"]["changed"], 1)

    def test_stale_plan_falls_back_to_full_replan(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        planned["planned_at"] = time.time() - sync.PLAN_REUSE_TTL - 5
        res = self.commit(LINE_A, planned)
        self.assertEqual(res["recheck"]["mode"], "full")
        self.assertTrue(res["rows"][0]["commit"]["done"])

    def test_no_cached_plan_is_full(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        res = self.commit(LINE_A, planned, cached=False)
        self.assertEqual(res["recheck"]["mode"], "full")

    def test_sig_mismatch_still_guards_fast_path(self):
        self.fx.rows31.append(make_31())
        self.fx.rows56.append(make_56(isa=9903350996))
        planned = self.plan(LINE_A)
        bad = [{"line_no": 1, "sig": "deadbeefdeadbeef"}]
        res = sync.commit("VAST", LINE_A, bad, "prod", cached_plan=planned)
        self.assertIn("情况已变化", res["rows"][0]["commit"]["skipped"])
        self.assertEqual(self.fx.calls, [])


class TestBatchGetChunking(unittest.TestCase):
    def test_chunks_of_100(self):
        seen = []

        def api(method, path, payload=None, query=None):
            seen.append(len(payload["record_ids"]))
            return {"records": [{"record_id": r, "fields": {"x": 1}}
                                for r in payload["record_ids"]]}
        with mock.patch.object(lark, "_api", api), \
                mock.patch.object(sync, "_base", lambda: "b"):
            out = sync._batch_get("t", [f"r{i}" for i in range(250)] + ["r0"])
        self.assertEqual(seen, [100, 100, 50])
        self.assertEqual(len(out), 250)


if __name__ == "__main__":
    unittest.main()
