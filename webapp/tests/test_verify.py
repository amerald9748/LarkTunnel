# -*- coding: utf-8 -*-
"""Unit tests for verify_assignments (③核对) — offline.

Proves the audit follows 3.1 → 出库计划 → 5.6 and raises the right flag for
every way an assignment can be wrong. Also proves it writes NOTHING.

Run:  python -m unittest discover webapp/tests -v"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import verify_assignments as va   # noqa: E402
import lark_client as lark        # noqa: E402
from test_planner import (Fixture, fake_field_meta, fake_table_id,  # noqa: E402
                          make_31, make_56, make_trip)

AWB = "OOCU9020713B"


class TestParse(unittest.TestCase):
    def test_bare_awb(self):
        p = va.parse_line("ZCSU9034790B")
        self.assertEqual((p["awb"], p["dest"], p["isa"], p["time"]),
                         ("ZCSU9034790B", None, None, None))

    def test_full_sync_line_becomes_expectations(self):
        p = va.parse_line("ZCSU9034790B\tYVR4\t1\t14\t7404870996\t08/03/2026 13:00 PDT")
        self.assertEqual(p["awb"], "ZCSU9034790B")
        self.assertEqual(p["dest"], "YVR4")
        self.assertEqual(p["isa"], 7404870996)
        self.assertEqual(p["time"], "2026/08/03 13:00")

    def test_awb_plus_dest_only(self):
        p = va.parse_line("ZCSU9034790B YVR4")
        self.assertEqual(p["dest"], "YVR4")
        self.assertIsNone(p["isa"])


class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.writes = []
        self.patches = [
            mock.patch.object(va, "_search", self.fx.search),
            mock.patch.object(va, "_batch_get", self.fx.batch_get),
            mock.patch.object(va, "_find_31", self._find_31),
            mock.patch.object(lark, "field_meta", fake_field_meta),
            mock.patch.object(lark, "table_id", fake_table_id),
            mock.patch.object(lark, "ENV", "prod"),
            mock.patch.object(lark, "_api", self._no_writes),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _find_31(self, t31, awb, route, warehouse, plan_link=None):
        return [r for r in self.fx.rows31
                if r["fields"].get("目的地路线") == route
                and r["fields"].get("仓库供应商") == warehouse]

    def _no_writes(self, method, path, payload=None, query=None):
        # ③核对 must never call the API directly (all reads go through the
        # patched _search/_batch_get) — any hit here is a bug.
        self.writes.append((method, path))
        raise AssertionError(f"verify must not call _api: {method} {path}")

    def rows(self, warehouse="VAST", text=AWB):
        r = va.verify(warehouse, text)
        self.assertEqual(self.writes, [])
        return r

    def flags(self, res):
        return [f for x in res["results"] for row in x["rows"] for f in row["flags"]]


class TestChain(VerifyCase):
    def test_full_chain_ok(self):
        self.fx.rows31 = [make_31(actual="4", plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=9903350996, dest="YVR2")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        res = self.rows()
        row = res["results"][0]["rows"][0]
        self.assertEqual(row["flags"], [])                    # nothing wrong
        self.assertEqual(row["appointment"]["isa"], 9903350996)
        self.assertEqual(row["trip"]["record_id"], "trip1")
        self.assertEqual(res["summary"]["problems"], 0)

    def test_missing_plan(self):
        self.fx.rows31 = [make_31(actual="4")]                # no plan link
        res = self.rows()
        self.assertIn("missing", self.flags(res))
        self.assertEqual(res["summary"]["problems"], 1)

    def test_trip_without_appointment(self):
        self.fx.rows31 = [make_31(plan_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"])  # no isa link
        self.assertIn("no_isa", self.flags(self.rows()))

    def test_isa_and_time_mismatch_vs_expected(self):
        self.fx.rows31 = [make_31(plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=1111111111, time="2026/07/30 15:00",
                                  dest="YVR2")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        # expectation pasted from the ② batch
        res = self.rows(text=f"{AWB} YVR2 4 141 9903350996 07/30/2026 13:00 PDT")
        fl = self.flags(res)
        self.assertIn("isa_diff", fl)
        self.assertIn("time_diff", fl)

    def test_wrong_bay_detected(self):
        # 3.1 row is YVR2 but the linked appointment is for YVR4
        self.fx.rows31 = [make_31(plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=9903350996, dest="YVR4")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        self.assertIn("dest_diff", self.flags(self.rows()))

    def test_account_mismatch(self):
        self.fx.rows31 = [make_31(plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=9903350996, dest="YVR2", account="BESTAR")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        self.assertIn("acct_diff", self.flags(self.rows()))    # VAST wants 元浩

    def test_shared_appointment_flagged(self):
        # two 3.1 rows on the SAME appointment (a grouped 预约号)
        self.fx.rows31 = [make_31("a", dest="YVR2", plan_links=["trip1"]),
                          make_31("b", dest="YVR4", plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=9903350996, dest="YVR2")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["a", "b"], isa_ids=["r56a"])
        res = self.rows()
        self.assertEqual(len(res["results"][0]["rows"]), 2)
        self.assertTrue(all("shared" in row["flags"]
                            for row in res["results"][0]["rows"]))
        # 'shared' alone is not a problem (grouped appointments are normal)
        self.assertEqual(res["summary"]["flags"]["shared"], 2)

    def test_no_31_row_reports_error(self):
        res = va.verify("VAST", "NOSUCH0000000")
        self.assertIsNotNone(res["results"][0]["error"])
        self.assertEqual(res["summary"]["errors"], 1)

    def test_dest_narrows_to_one_row(self):
        self.fx.rows31 = [make_31("a", dest="YVR2", plan_links=["trip1"]),
                          make_31("b", dest="YVR4")]
        self.fx.rows56 = [make_56(isa=9903350996, dest="YVR2")]
        self.fx.trips["trip1"] = make_trip(inv_ids=["a"], isa_ids=["r56a"])
        res = self.rows(text=f"{AWB} YVR2")
        self.assertEqual(len(res["results"][0]["rows"]), 1)
        self.assertEqual(res["results"][0]["rows"][0]["route"], "YVR2")

    def test_dev_without_plan_table_copy(self):
        with mock.patch.object(lark, "ENV", "dev"):
            res = va.verify("VAST", AWB)      # no dev 5.4 copy
            self.assertIn("DEV", res["results"][0]["error"])


class TestFieldListRegression(unittest.TestCase):
    """_find_31 must REQUEST 目的地路线. It is a search condition, so it is
    tempting to omit it — but ③核对 reads it back to detect a wrong-bay
    assignment, and without it dest_diff silently never fires (real bug,
    caught live 2026-08-04)."""

    def test_find_31_requests_route_column(self):
        import appointment_sync as sync
        seen = {}

        def spy(table, conditions, field_names, page_size=20):
            seen["fields"] = field_names
            return []
        with mock.patch.object(sync, "_search", spy), \
             mock.patch.object(lark, "ENV", "prod"):
            sync._find_31("t31", AWB, "YVR2", "VAST", "5.4 VAST-VAN-01")
        self.assertIn("目的地路线", seen["fields"])
        self.assertIn("实际板数", seen["fields"])
        self.assertIn("5.4 VAST-VAN-01", seen["fields"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
