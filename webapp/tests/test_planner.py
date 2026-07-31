# -*- coding: utf-8 -*-
"""Unit tests for the appointment_sync PLAN decision tree — every branch of
the runbook, offline. Network readers (_search/_batch_get, field_meta,
table_id) are monkeypatched with an in-memory fixture Base.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appointment_sync as sync  # noqa: E402
import lark_client as lark       # noqa: E402

T31, T56, T54 = "t31", "t56", "t54"   # fixture table ids (VAST -> 5.4)


def fake_field_meta(_tid):
    return {"by_name": {
        "目的地": {"options": {"1": "YVR2", "2": "YVR4", "3": "YEG1", "4": "YEG2"}},
        "预约账号": {"options": {"1": "元浩", "2": "BESTAR", "3": "WBLL", "4": "GFL"}},
    }}


def fake_table_id(label):
    return {"3.1": T31, "5.6": T56, "5.4 VAST-VAN-01": T54,
            "5.2 BESTAR-CAL": "t52", "5.3 WBLL-EDM": "t53",
            "5.5 GFL-VAN-02": "t55"}[label]


class Fixture:
    """In-memory stand-in for the three tables the planner reads."""

    def __init__(self):
        self.rows31 = []       # [{record_id, fields}]
        self.rows56 = []
        self.trips = {}        # record_id -> fields

    # ---- plugs for appointment_sync._search / _batch_get -------------------
    def search(self, table, conditions, field_names, page_size=20):
        data = {T31: self.rows31, T56: self.rows56}.get(table, [])
        out = []
        for rec in data:
            keep = True
            for c in conditions:
                fname, op, val = c["field_name"], c["operator"], c["value"][0]
                got = rec["fields"].get(fname)
                got_s = str(got if not isinstance(got, list) else
                            "".join(x.get("text", "") for x in got))
                if op == "is" and got_s != val:
                    keep = False
                elif op == "contains" and val not in got_s:
                    keep = False
            if keep:
                out.append(rec)
        return out

    def batch_get(self, table, record_ids, field_names=None):
        if table == T54:
            src = self.trips
            return {rid: dict(src[rid]) for rid in record_ids if rid in src}
        data = {T31: self.rows31, T56: self.rows56}.get(table, [])
        by_id = {r["record_id"]: r["fields"] for r in data}
        return {rid: dict(by_id[rid]) for rid in record_ids if rid in by_id}


def make_31(rid="r31a", awb="OOCU9020713B", wh="VAST", dest="YVR2",
            actual="", est=4.0, boxes=141, plan_links=None):
    f = {"柜号/AWB": [{"text": awb}], "仓库供应商": wh, "目的地路线": dest,
         "实际板数": ([{"text": actual}] if actual else None),
         "预计板数": {"type": 2, "value": [est]} if est is not None else None,
         "箱数": boxes, "客户批次号": [{"text": "B-1"}], "派送计划": None}
    if plan_links:
        f["5.4 VAST-VAN-01"] = {"link_record_ids": plan_links}
    return {"record_id": rid, "fields": {k: v for k, v in f.items() if v is not None}}


def make_56(rid="r56a", isa=7403350996, time="2026/07/30 13:00", dest="YVR2",
            account="元浩", trip_links=None):
    f = {"ISA": isa, "复制时间列": [{"text": time}], "目的地": dest, "预约账号": account}
    if trip_links:
        f["5.4 出库计划 温哥华"] = {"link_record_ids": trip_links}
    return {"record_id": rid, "fields": f}


def make_trip(inv_ids=(), isa_ids=()):
    return {"预约信息": {"link_record_ids": list(isa_ids)},
            "库存信息-元浩": {"link_record_ids": list(inv_ids)}}


LINE_BASIC = "OOCU9020713B YVR2 4 141"
LINE_FULL = "OOCU9020713B YVR2 4 141 7403350996 07/30/2026 13:00 PDT"


class PlannerCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.patches = [
            mock.patch.object(sync, "_search", self.fx.search),
            mock.patch.object(sync, "_batch_get", self.fx.batch_get),
            mock.patch.object(lark, "field_meta", fake_field_meta),
            mock.patch.object(lark, "table_id", fake_table_id),
            mock.patch.object(lark, "ENV", "prod"),   # prod field names in fixtures
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def plan1(self, line, warehouse="VAST"):
        r = sync.plan(warehouse, line)
        return r["rows"][0]

    def action_types(self, row):
        return [a["type"] for a in row["actions"]]


class TestStep2Matching(PlannerCase):
    def test_no_match(self):
        row = self.plan1(LINE_BASIC)
        self.assertIn("无匹配", row["match_error"])

    def test_ambiguous(self):
        self.fx.rows31 = [make_31("a"), make_31("b")]
        row = self.plan1(LINE_BASIC)
        self.assertIn("2 行", row["match_error"])

    def test_suffix_fallback(self):
        # stored as …713BA (one extra char) — search input …713B still matches
        self.fx.rows31 = [make_31(awb="OOCU9020713BA")]
        row = self.plan1(LINE_BASIC)
        self.assertIsNone(row["match_error"])
        self.assertEqual(row["match"]["awb"], "OOCU9020713BA")

    def test_merged_container_not_mismatched(self):
        # a longer, different container must NOT match via the fallback
        self.fx.rows31 = [make_31(awb="OOCU9020713BXX")]
        row = self.plan1(LINE_BASIC)
        self.assertIn("无匹配", row["match_error"])


class TestStep3Pallets(PlannerCase):
    def test_empty_filled(self):
        self.fx.rows31 = [make_31(actual="", est=4.0)]
        row = self.plan1(LINE_BASIC)
        self.assertIn("fill_pallets", self.action_types(row))
        self.assertFalse([w for w in row["warnings"] if w.startswith("W1")])

    def test_empty_filled_with_w1(self):
        self.fx.rows31 = [make_31(actual="", est=9.0)]   # |4-9| = 5 > 2
        row = self.plan1(LINE_BASIC)
        self.assertIn("fill_pallets", self.action_types(row))
        self.assertTrue([w for w in row["warnings"] if w.startswith("W1")])

    def test_occupied_never_overwritten(self):
        self.fx.rows31 = [make_31(actual="7", est=9.0)]  # |4-9| > 2 AND occupied
        row = self.plan1(LINE_BASIC)
        self.assertNotIn("fill_pallets", self.action_types(row))
        w2 = [w for w in row["warnings"] if w.startswith("W2")]
        self.assertTrue(w2)
        self.assertIn("未覆盖", w2[0])

    def test_occupied_small_diff_no_warning(self):
        self.fx.rows31 = [make_31(actual="5", est=4.0)]  # diff 0 vs est... |4-4|
        row = self.plan1(LINE_BASIC)
        self.assertNotIn("fill_pallets", self.action_types(row))
        self.assertFalse([w for w in row["warnings"] if w.startswith("W2")])

    def test_boundary_diff_exactly_2_no_warning(self):
        self.fx.rows31 = [make_31(actual="", est=6.0)]   # |4-6| == 2, not > 2
        row = self.plan1(LINE_BASIC)
        self.assertFalse([w for w in row["warnings"] if w.startswith("W1")])

    def test_box_mismatch_warns(self):
        self.fx.rows31 = [make_31(boxes=999)]
        row = self.plan1(LINE_BASIC)
        self.assertTrue([w for w in row["warnings"] if "箱数不一致" in w])


class TestStep4APlanExists(PlannerCase):
    def _wire(self, isa=7403350996, time="2026/07/30 13:00", inv=("r31a",),
              actual="4"):
        # 实际板数 already "4" so the pallet step plans NOTHING — these tests
        # isolate the PLAN-wiring branch.
        self.fx.rows31 = [make_31(actual=actual, plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=isa, time=time, trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(inv_ids=inv, isa_ids=["r56a"])

    def test_match_does_nothing(self):
        self._wire()
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "has_plan_match")
        self.assertEqual(row["actions"], [])

    def test_isa_mismatch_updates_linked_56(self):
        self._wire(isa=1111111111)
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "has_plan_mismatch")
        acts = [a for a in row["actions"] if a["type"] == "update_isa_time"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["isa_record_id"], "r56a")   # EDIT THE LINKED record
        self.assertEqual(acts[0]["isa"], 7403350996)
        self.assertEqual(acts[0]["time"], "2026/07/30 13:00")

    def test_time_mismatch_updates(self):
        self._wire(time="2026/07/30 15:00")
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "has_plan_mismatch")

    def test_time_format_variants_still_match(self):
        # stored year-first vs pasted month-first must compare EQUAL
        self._wire(time="2026/7/30 13:00")
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "has_plan_match")

    def test_no_isa_provided_display_only(self):
        self._wire()
        row = self.plan1(LINE_BASIC)
        self.assertEqual(row["plan"]["status"], "has_plan")
        self.assertEqual(row["actions"], [])   # occupied pallets + no ISA -> nothing

    def test_no_isa_but_empty_pallets_still_fills(self):
        # 4-token mode never touches the plan, but the 板数 fill STILL happens.
        self._wire(actual="")
        row = self.plan1(LINE_BASIC)
        self.assertEqual(row["plan"]["status"], "has_plan")
        self.assertEqual([a["type"] for a in row["actions"]], ["fill_pallets"])

    def test_w3_overflow_on_existing_plan(self):
        others = [make_31(f"r31x{i}", awb=f"X{i}", actual="10") for i in range(3)]
        self.fx.rows31 = [make_31(plan_links=["trip1"])] + others
        self.fx.rows56 = [make_56(trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(
            inv_ids=["r31a"] + [o["record_id"] for o in others], isa_ids=["r56a"])
        row = self.plan1(LINE_FULL)   # 4 + 30 = 34 > 28
        self.assertTrue([w for w in row["warnings"] if w.startswith("W3")])


class TestStep4BNoPlan(PlannerCase):
    def test_no_isa_no_action(self):
        self.fx.rows31 = [make_31(actual="4")]
        row = self.plan1(LINE_BASIC)
        self.assertEqual(row["plan"]["status"], "no_plan")
        self.assertEqual(row["actions"], [])

    def test_isa_with_trip_links_shipment(self):
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56(trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(inv_ids=[], isa_ids=["r56a"])
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "link_existing")
        acts = [a for a in row["actions"] if a["type"] == "link_trip"]
        self.assertEqual(acts[0]["trip_id"], "trip1")
        self.assertEqual(acts[0]["plan_link_field"], "5.4 VAST-VAN-01")

    def test_isa_with_trip_overflow_warns(self):
        others = [make_31(f"r31x{i}", awb=f"X{i}", actual="9") for i in range(3)]
        self.fx.rows31 = [make_31()] + others
        self.fx.rows56 = [make_56(trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(
            inv_ids=[o["record_id"] for o in others], isa_ids=["r56a"])
        row = self.plan1(LINE_FULL)   # 27 existing + 4 = 31 > 28
        self.assertTrue([w for w in row["warnings"] if w.startswith("W3")])

    def test_isa_without_trip_creates_trip(self):
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56()]                      # no trip links
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "create_trip")
        self.assertEqual(self.action_types(row).count("create_trip"), 1)
        self.assertIn("link_trip", self.action_types(row))
        self.assertNotIn("create_isa", self.action_types(row))

    def test_time_refresh_on_existing_isa(self):
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56(time="2026/07/30 15:00")]   # stored differs
        row = self.plan1(LINE_FULL)
        self.assertIn("update_isa_time", self.action_types(row))

    def test_isa_missing_creates_isa_and_trip(self):
        self.fx.rows31 = [make_31()]
        row = self.plan1(LINE_FULL)
        self.assertEqual(row["plan"]["status"], "create_isa_and_trip")
        types = self.action_types(row)
        self.assertEqual(["create_isa", "create_trip", "link_trip"],
                         [t for t in types if t != "fill_pallets"])
        isa_act = next(a for a in row["actions"] if a["type"] == "create_isa")
        self.assertEqual(isa_act["fields"]["ISA"], 7403350996)
        self.assertEqual(isa_act["fields"]["预约账号"], "元浩")   # VAST -> 元浩
        self.assertEqual(isa_act["fields"]["复制时间列"], "2026/07/30 13:00")

    def test_invalid_dest_blocks_create(self):
        self.fx.rows31 = [make_31(dest="YVR9")]
        row = self.plan1("OOCU9020713B YVR9 4 141 7403350996 07/30/2026 13:00")
        self.assertTrue(row["blockers"])
        self.assertIn("YVR9", row["blockers"][0])
        self.assertNotIn("create_isa", self.action_types(row))

    def test_account_mismatch_warns(self):
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56(account="BESTAR", trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(isa_ids=["r56a"])
        row = self.plan1(LINE_FULL)
        self.assertTrue([w for w in row["warnings"] if "预约账号" in w])


class TestGroupsAndWarehouses(PlannerCase):
    def test_group_time_conflict_blocks(self):
        self.fx.rows31 = [make_31("a"), make_31("b", awb="TCNU4251020B", dest="YVR4")]
        text = ("OOCU9020713B YVR2 4 141 7403350996 07/30/2026 13:00\n"
                "TCNU4251020B YVR4 1 4 7403350996 07/30/2026 15:00")
        r = sync.plan("VAST", text)
        self.assertTrue(all(row["blockers"] for row in r["rows"]))

    def test_group_shares_one_create(self):
        self.fx.rows31 = [make_31("a"), make_31("b", awb="TCNU4251020B", dest="YVR2")]
        text = ("OOCU9020713B YVR2 4 141 7403350996 07/30/2026 13:00\n"
                "TCNU4251020B YVR2 1 4 7403350996 07/30/2026 13:00")
        r = sync.plan("VAST", text)
        # both rows plan create_isa/create_trip for the SAME group; commit
        # materializes each exactly once (deduped by group_isa)
        for row in r["rows"]:
            self.assertIn("create_isa", [a["type"] for a in row["actions"]])

    def test_group_cap_warning_new_trip(self):
        self.fx.rows31 = [make_31("a"), make_31("b", awb="TCNU4251020B", dest="YVR2")]
        text = ("OOCU9020713B YVR2 20 141 7403350996 07/30/2026 13:00\n"
                "TCNU4251020B YVR2 15 4 7403350996 07/30/2026 13:00")   # 35 > 28
        r = sync.plan("VAST", text)
        self.assertTrue(any(w.startswith("W3") for row in r["rows"] for w in row["warnings"]))

    def test_tor_1140_pallets_only(self):
        self.fx.rows31 = [make_31(wh="TOR-1140", dest="YEG2")]
        row = self.plan1("OOCU9020713B YEG2 4 141", warehouse="TOR-1140")
        self.assertEqual(row["plan"]["status"], "no_plan_table")
        self.assertEqual(self.action_types(row), ["fill_pallets"])

    def test_cal_5505_routes_to_bestar(self):
        self.assertEqual(sync.WAREHOUSES["CAL-5505"]["plan_table"], "5.2 BESTAR-CAL")
        self.assertEqual(sync.WAREHOUSES["CAL-5505"]["account"], "BESTAR")

    def test_unknown_warehouse_raises(self):
        with self.assertRaises(lark.LarkError):
            sync.plan("NOPE", LINE_BASIC)

    def test_sig_stable_and_change_detected(self):
        self.fx.rows31 = [make_31(actual="", est=4.0)]
        r1 = self.plan1(LINE_BASIC)
        r2 = self.plan1(LINE_BASIC)
        self.assertEqual(r1["sig"], r2["sig"])
        self.fx.rows31 = [make_31(actual="4", est=4.0)]   # someone filled it
        r3 = self.plan1(LINE_BASIC)
        self.assertNotEqual(r1["sig"], r3["sig"])


class TestDevWiring(PlannerCase):
    """LARK_ENV=dev field-name resolution (real config.js values)."""

    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.object(lark, "ENV", "dev")
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_bestar_uses_dev_columns(self):
        w = sync._env_wiring("5.2 BESTAR-CAL")
        self.assertTrue(w["enabled"])
        self.assertEqual(w["plan_link_31"],
                         "5.2 BESTAR-CAL-P-01 副本-3.1 库存总表 副本-5.2 BESTAR-CAL")
        self.assertEqual(w["link_on_56"],
                         "5.2 BESTAR-CAL-P-01 副本-5.6 预约表 副本-5.2 出库计划 卡尔加里")
        self.assertEqual(w["isa_field"], "5.6 预约表 副本-5.2 出库计划 卡尔加里")
        self.assertEqual(w["inv_field"], "3.1 库存总表 副本-5.2 BESTAR-CAL")

    def test_warehouse_without_dev_copy_disabled(self):
        # VAST has no dev 5.4 copy: trips OFF in dev, pallet fill still planned
        self.assertFalse(sync._env_wiring("5.4 VAST-VAN-01")["enabled"])
        self.fx.rows31 = [make_31()]     # empty 实际板数
        row = self.plan1(LINE_FULL)      # ISA+time provided
        self.assertEqual(row["plan"]["status"], "no_dev_plan_table")
        self.assertEqual([a["type"] for a in row["actions"]], ["fill_pallets"])

    def test_dev_bestar_plans_dev_link_field(self):
        self.fx.rows31 = [make_31(wh="BESTAR", dest="YEG2")]
        r = sync.plan("BESTAR",
                      "OOCU9020713B YEG2 4 141 9903350996 07/30/2026 13:00")
        row = r["rows"][0]
        link = next(a for a in row["actions"] if a["type"] == "link_trip")
        self.assertEqual(link["plan_link_field"],
                         "5.2 BESTAR-CAL-P-01 副本-3.1 库存总表 副本-5.2 BESTAR-CAL")

    def test_prod_wiring_unchanged(self):
        self.env_patch.stop()            # back to prod for this one
        try:
            w = sync._env_wiring("5.2 BESTAR-CAL")
            self.assertEqual(w["plan_link_31"], "5.2 BESTAR-CAL")
            self.assertEqual(w["link_on_56"], "5.2 出库计划 卡尔加里")
            self.assertEqual(w["isa_field"], "预约信息")
            self.assertEqual(w["inv_field"], "库存信息-卡尔加里")
        finally:
            self.env_patch.start()


if __name__ == "__main__":
    unittest.main(verbosity=2)
