# -*- coding: utf-8 -*-
"""Unit tests for appointment_create (新建预约, 5.6-only) — offline.

Covers: the [目的地] [ISA] [时间] parser; the three dedup layers (in-batch,
table search, recent-creates cache); commit convergence on re-submission.

Run:  python -m unittest discover webapp/tests -v"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appointment_create as c56  # noqa: E402
import lark_client as lark        # noqa: E402
from test_planner import (Fixture, fake_field_meta, fake_table_id,  # noqa: E402
                          make_56, T56)


class TestParse(unittest.TestCase):
    def test_examples_from_spec(self):
        p = c56.parse_line("YVR4\t7405010996\t08/06/2026 20:00 PDT")
        self.assertEqual((p["dest"], p["isa"], p["time"], p["tz"]),
                         ("YVR4", 7405010996, "2026/08/06 20:00", "PDT"))
        # leading space inside the pasted time cell (spec example 3)
        p = c56.parse_line("YEG2\t147683024984\t 08/05/2026 19:00 MDT")
        self.assertEqual((p["isa"], p["time"]), (147683024984, "2026/08/05 19:00"))
        # trailing whitespace/tab (wave lines end with '\t')
        p = c56.parse_line("YEG1\t7229372996\t08/05/2026 07:00 MDT \t")
        self.assertEqual(p["isa"], 7229372996)

    def test_no_tz_ok(self):
        p = c56.parse_line("YEG1 7229872996 08/07/2026 08:00")
        self.assertIsNone(p["tz"])
        self.assertEqual(p["time"], "2026/08/07 08:00")

    def test_rejects(self):
        for bad in ("YEG1 7229872996",                       # missing time
                    "1XX 7229872996 08/07/2026 08:00",        # dest shape (digit first)
                    "YEG1 123 08/07/2026 08:00",              # short ISA
                    "YEG1 7229872996 99/99/2026 08:00",       # bad date
                    "YEG1 7229872996 08/07/2026 08:00 XYZ123",  # bad tz
                    "YEG1 7229872996 08/07/2026 08:00 MDT extra"):
            self.assertIn("error", c56.parse_line(bad), bad)

    def test_any_live_option_shape_parses(self):
        """Destinations are validated against LIVE 5.6 options at plan time,
        NOT a hardcoded pattern — XCAB (a real live option) must parse.
        Regression for the operator-reported rejection 2026-08-04."""
        p = c56.parse_line("XCAB\t7229872996\t08/07/2026 08:00 MDT")
        self.assertNotIn("error", p)
        self.assertEqual(p["dest"], "XCAB")
        # unknown-but-well-formed codes parse too; the plan step blocks them
        # against the live option list (see test_dest_not_a_56_option_blocked)
        self.assertNotIn("error", c56.parse_line("XXX1 7229872996 08/07/2026 08:00"))


class CreateCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.api_calls = []
        self.patches = [
            mock.patch.object(c56, "_search", self.fx.search),
            mock.patch.object(lark, "field_meta", fake_field_meta),
            mock.patch.object(lark, "table_id", fake_table_id),
            mock.patch.object(lark, "ENV", "prod"),
            mock.patch.object(lark, "_api", self.fake_api),
            mock.patch.object(c56, "VERIFY_WAIT_CAP", 0.5),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        # each test gets a clean recent-creates registry
        c56._RECENT.clear()
        self.addCleanup(c56._RECENT.clear)

    def fake_api(self, method, path, payload=None, query=None):
        self.api_calls.append((method, path, payload))
        if path.endswith("/records/batch_create"):
            recs = payload["records"]
            made = []
            for i, rec in enumerate(recs):
                rid = f"new56_{len(self.fx.rows56) + i}"
                made.append({"record_id": rid})
                # simulate the record landing in the table (search sees it)
                isa = rec["fields"]["ISA"]
                self.fx.rows56.append(make_56(rid, isa=isa,
                                              time=rec["fields"]["复制时间列"],
                                              dest=rec["fields"]["目的地"],
                                              account=rec["fields"]["预约账号"]))
            return {"records": made}
        return {}

    def creates_sent(self):
        return sum(len(p["records"]) for _, path, p in self.api_calls
                   if path.endswith("/records/batch_create"))


# NOTE: dests restricted to fake_field_meta's 目的地 options (no YYC4 there —
# the real YYC4 lines are exercised by the LIVE integration test).
WAVE1 = ("YEG1\t7227802996\t08/02/2026 07:00 MDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT \t\n"
         "YEG2\t129695028975\t08/02/2026 10:00 MDT")
WAVE2 = ("YVR4\t7404870996\t08/03/2026 13:00 PDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT \t\n"
         "YEG2\t147621024984\t 08/03/2026 09:00 MDT")


def approve(planned):
    return [{"line_no": r["line_no"], "sig": r["sig"]}
            for r in planned["rows"] if r["action"] == "create"]


class TestPlanDedup(CreateCase):
    def test_all_new(self):
        r = c56.plan("BESTAR", WAVE1)
        self.assertEqual([row["action"] for row in r["rows"]],
                         ["create"] * 3)
        self.assertEqual(r["summary"]["create"], 3)
        self.assertEqual(r["account"], "BESTAR")

    def test_existing_skipped_with_diff_warning(self):
        self.fx.rows56 = [make_56("e1", isa=7229372996,
                                  time="2026/08/05 07:00", dest="YEG1",
                                  account="BESTAR")]
        r = c56.plan("BESTAR", WAVE1)
        acts = [row["action"] for row in r["rows"]]
        self.assertEqual(acts, ["create", "exists", "create"])
        self.assertFalse(r["rows"][1]["warnings"])          # identical content
        # now stored content differs -> warning, still skip
        self.fx.rows56[0]["fields"]["目的地"] = "YVR4"
        r = c56.plan("BESTAR", WAVE1)
        self.assertEqual(r["rows"][1]["action"], "exists")
        self.assertTrue(any("不同" in w for w in r["rows"][1]["warnings"]))

    def test_inbatch_duplicate_creates_once(self):
        text = WAVE1 + "\nYEG1\t7227802996\t08/02/2026 07:00 MDT"
        r = c56.plan("BESTAR", text)
        self.assertEqual([row["action"] for row in r["rows"]],
                         ["create", "create", "create", "dup"])

    def test_inbatch_conflict_blocks_group(self):
        text = ("YEG1\t7227802996\t08/02/2026 07:00 MDT\n"
                "YEG2\t7227802996\t08/09/2026 07:00 MDT")   # same ISA, differs
        r = c56.plan("BESTAR", text)
        self.assertEqual([row["action"] for row in r["rows"]], ["block", "block"])

    def test_recent_cache_hits_without_search(self):
        c56._recent_put(7227802996, "recX", "YEG1", "2026/08/02 07:00", "BESTAR")
        r = c56.plan("BESTAR", "YEG1\t7227802996\t08/02/2026 07:00 MDT")
        row = r["rows"][0]
        self.assertEqual(row["action"], "exists")
        self.assertEqual(row["existing"]["source"], "recent")

    def test_recent_cache_expires(self):
        c56._recent_put(7227802996, "recX", "YEG1", "2026/08/02 07:00", "BESTAR")
        c56._RECENT[7227802996]["ts"] = time.time() - c56.RECENT_TTL - 1
        r = c56.plan("BESTAR", "YEG1\t7227802996\t08/02/2026 07:00 MDT")
        self.assertEqual(r["rows"][0]["action"], "create")  # expired -> re-check

    def test_vast_maps_to_yuanhao(self):
        r = c56.plan("VAST", "YVR4\t7404870996\t08/03/2026 13:00 PDT")
        self.assertEqual(r["account"], "元浩")
        self.assertEqual(r["rows"][0]["fields"]["预约账号"], "元浩")

    def test_tor1140_blocked(self):
        r = c56.plan("TOR-1140", "YEG1\t7227802996\t08/02/2026 07:00 MDT")
        self.assertEqual(r["rows"][0]["action"], "block")

    def test_dest_not_a_56_option_blocked(self):
        # YYC4 is NOT in fake_field_meta's 目的地 options
        r = c56.plan("BESTAR", "YYC4\t129695028975\t08/02/2026 10:00 MDT")
        self.assertEqual(r["rows"][0]["action"], "block")
        self.assertTrue(any("选项" in b for b in r["rows"][0]["blockers"]))


class TestCommit(CreateCase):
    WAVE1_OK = ("YEG1\t7227802996\t08/02/2026 07:00 MDT\n"
                "YEG1\t7229372996\t08/05/2026 07:00 MDT\n"
                "YVR4\t7404870996\t08/03/2026 13:00 PDT")   # all in fake options

    def test_waves_duplicate_created_once(self):
        p1 = c56.plan("BESTAR", self.WAVE1_OK)
        r1 = c56.commit("BESTAR", self.WAVE1_OK, approve(p1), "prod")
        self.assertEqual(self.creates_sent(), 3)
        self.assertTrue(all((row.get("commit") or {}).get("verified")
                            for row in r1["rows"] if row.get("approved")))
        # wave 2 shares 7229372996 — must NOT be created twice
        wave2 = ("YEG2\t147621024984\t08/03/2026 09:00 MDT\n"
                 "YEG1\t7229372996\t08/05/2026 07:00 MDT")
        p2 = c56.plan("BESTAR", wave2)
        self.assertEqual([row["action"] for row in p2["rows"]],
                         ["create", "exists"])
        c56.commit("BESTAR", wave2, approve(p2), "prod")
        self.assertEqual(self.creates_sent(), 4)            # 3 + 1, not 3 + 2
        # exactly one record with the shared ISA
        n = sum(1 for rec in self.fx.rows56
                if rec["fields"]["ISA"] == 7229372996)
        self.assertEqual(n, 1)

    def test_wave2_dedup_via_recent_cache_when_search_lags(self):
        p1 = c56.plan("BESTAR", self.WAVE1_OK)
        c56.commit("BESTAR", self.WAVE1_OK, approve(p1), "prod")
        # simulate Bitable index lag: search suddenly sees NOTHING
        self.fx.rows56 = []
        wave2 = "YEG1\t7229372996\t08/05/2026 07:00 MDT"
        p2 = c56.plan("BESTAR", wave2)
        row = p2["rows"][0]
        self.assertEqual(row["action"], "exists")           # cache caught it
        self.assertEqual(row["existing"]["source"], "recent")

    def test_recommit_creates_nothing(self):
        p1 = c56.plan("BESTAR", self.WAVE1_OK)
        c56.commit("BESTAR", self.WAVE1_OK, approve(p1), "prod")
        sent = self.creates_sent()
        p2 = c56.plan("BESTAR", self.WAVE1_OK)
        self.assertEqual(approve(p2), [])                   # nothing to approve
        c56.commit("BESTAR", self.WAVE1_OK, approve(p2), "prod")
        self.assertEqual(self.creates_sent(), sent)

    def test_stale_sig_skipped(self):
        p1 = c56.plan("BESTAR", self.WAVE1_OK)
        appr = approve(p1)
        appr[0]["sig"] = "deadbeef"
        r = c56.commit("BESTAR", self.WAVE1_OK, appr, "prod")
        skipped = [row for row in r["rows"]
                   if (row.get("commit") or {}).get("skipped")]
        self.assertTrue(any("情况已变化" in row["commit"]["skipped"]
                            for row in skipped))
        self.assertEqual(self.creates_sent(), 2)            # the other two only

    def test_env_mismatch_refused(self):
        with self.assertRaises(lark.LarkError):
            c56.commit("BESTAR", self.WAVE1_OK, [], "dev")  # server is 'prod'

    def test_latency_measured(self):
        p1 = c56.plan("BESTAR", "YEG1\t7227802996\t08/02/2026 07:00 MDT")
        r = c56.commit("BESTAR", "YEG1\t7227802996\t08/02/2026 07:00 MDT",
                       approve(p1), "prod")
        self.assertEqual(r["latency"]["created"], 1)
        self.assertEqual(r["latency"]["visible"], 1)
        self.assertIsNotNone(r["latency"]["avg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
