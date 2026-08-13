# -*- coding: utf-8 -*-
"""Unit tests for inventory_import parsing/aggregation (收货派送计划 → 3.1).
Offline — no network. Run:  python -m unittest discover webapp/tests -v

The fixture mirrors the real BEAU6279991 收货派送计划 layout validated live on
2026-07-22: title block rows, header at row 7, a 汇总 footer row, and the
destination column named 仓库代码.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inventory_import as imp  # noqa: E402


def beau_sheet():
    """Minimal replica of the validated container plan (3 of 17 rows + 汇总)."""
    return {
        "name": "港后列表-收货派送计划",
        "rows": [
            ["收货派送计划表"],
            ["shipper:", "深圳劲港跨境物流有限公司"],
            ["柜号:", "BEAU6279991", "", "柜型:", "40HQ"],
            ["ETD:", "2026/06/28", "", "ETA:", "2026/07/12"],
            ["到仓日期:", "", "", "拆柜日期:", ""],
            [],
            ["SO", "FBA", "Reference ID", "件数", "重量", "体积", "地址类型",
             "仓库代码", "仓库地址", "邮编", "派送方式", "最后派送时间"],
            ["980260600006774", "FBA19G0GP3HG", "6WIK7PHJ", 223, 1873.4, 12.7,
             "Amazon", "YEG1", "addr", "T9E 0B4", "卡车派送", 46247],
            ["980260600006775", "FBA19G0GPC29", "X", 252, 1912.25, 13.56,
             "Amazon", "YEG1", "addr", "T9E 0B4", "卡车派送", 46247],
            ["990260400012038", "FBA19G85HX56", "Y", 15, 286.8, 1.77,
             "Amazon", "CA-YVR4", "addr", "V3M", "卡车派送", 46247],
            [None, None, "汇总", 490, 4072.45, 28.03, None, None],
        ],
    }


class TestSheetSelection(unittest.TestCase):
    def test_selects_data_sheet_and_header(self):
        sheet, hdr = imp.select_sheet([beau_sheet()])
        self.assertIsNotNone(sheet)
        self.assertEqual(hdr, 6)          # header on the 7th row (0-based 6)

    def test_skips_ups_named_sheets(self):
        ups = dict(beau_sheet(), name="UPS 快递单")
        sheet, _ = imp.select_sheet([ups])
        self.assertIsNone(sheet)          # UPS sheets never qualify

    def test_prefers_keyword_sheet_over_stronger_match(self):
        # 派送-named sheet wins by keyword rank even against an equal match.
        a = dict(beau_sheet(), name="Sheet1")
        b = dict(beau_sheet(), name="加西卡派清单")
        sheet, _ = imp.select_sheet([a, b])
        self.assertEqual(sheet["name"], "加西卡派清单")

    def test_no_recognizable_header(self):
        junk = {"name": "S", "rows": [["a", "b"], ["c", "d"]]}
        sheet, _ = imp.select_sheet([junk])
        self.assertIsNone(sheet)


class TestLoadPlan(unittest.TestCase):
    def test_drops_summary_row_and_maps_aliases(self):
        plan = imp.load_plan([beau_sheet()], {})
        self.assertEqual(len(plan["records"]), 3)       # 汇总 row dropped
        rec = plan["records"][0]
        self.assertEqual(rec["派送目的地"], "YEG1")      # 仓库代码 -> canonical
        self.assertEqual(rec["箱数/件数"], 223)          # 件数 -> canonical

    def test_missing_essential_column_raises(self):
        s = beau_sheet()
        s["rows"][6][7] = "别的列"                       # remove 仓库代码
        # header still matches >=2 aliases (件数/重量/体积), so load_plan gets
        # past sheet selection and must fail on the essential-column check
        with self.assertRaises(ValueError):
            imp.load_plan([s], {})


class TestMerges(unittest.TestCase):
    def test_quantity_merge_zeroes_subrows_identity_copies_down(self):
        s = beau_sheet()
        # merge rows 8-9 (0-based) on 件数 (col 3, quantity) and 仓库代码 (col 7)
        s["rows"][9][3] = ""                # sub-row cell reads blank
        s["rows"][9][7] = ""
        merges = [(8, 3, 9, 3), (8, 7, 9, 7)]
        rows = imp.fill_merges(s["rows"], merges, 6)
        self.assertEqual(rows[9][3], 0)             # quantity sub-row -> 0
        self.assertEqual(rows[9][7], "YEG1")        # identity copied down

    def test_merge_above_header_ignored(self):
        s = beau_sheet()
        before = [list(r) for r in s["rows"]]
        rows = imp.fill_merges(s["rows"], [(0, 0, 4, 0)], 6)
        self.assertEqual(rows, before)


class TestAggregate(unittest.TestCase):
    def test_sums_by_normalized_destination(self):
        plan = imp.load_plan([beau_sheet()], {})
        aggs, totals, ups = imp.aggregate(plan["records"])
        self.assertEqual(set(aggs), {"YEG1", "YVR4"})   # CA-YVR4 normalized
        self.assertEqual(aggs["YEG1"]["boxes"], 475)
        self.assertEqual(aggs["YEG1"]["weight"], 3785.65)
        self.assertEqual(aggs["YEG1"]["volume"], 26.26)
        self.assertEqual(aggs["YVR4"]["rows"], 1)
        self.assertEqual(totals["boxes"], 490)          # matches 汇总 row
        self.assertEqual(totals["weight"], 4072.45)
        self.assertEqual(totals["volume"], 28.03)
        self.assertEqual(ups, 0)

    def test_normalize_dest(self):
        self.assertEqual(imp.normalize_dest(" ca-yvr4 "), "YVR4")
        self.assertEqual(imp.normalize_dest("YEG1"), "YEG1")


class TestUpsDetection(unittest.TestCase):
    def test_keyword_in_method(self):
        self.assertTrue(imp.is_ups_row({"派送方式": "UPS 派送", "派送目的地": "YVR4"}))

    def test_tracking_code_anywhere(self):
        self.assertTrue(imp.is_ups_row(
            {"派送目的地": "YVR4", "FBA NO.": "1Z999AA10123456784"}))

    def test_short_1z_po_not_ups(self):
        # short PO starting with 1Z must NOT count as a tracking code
        self.assertFalse(imp.is_ups_row({"派送目的地": "YVR4", "PO#": "1Z99AA1"}))

    def test_plain_truck_row(self):
        self.assertFalse(imp.is_ups_row({"派送目的地": "YEG1", "派送方式": "卡车派送"}))


class TestMergeRefParsing(unittest.TestCase):
    def test_ref_rc(self):
        self.assertEqual(imp._ref_rc("A7"), (6, 0))
        self.assertEqual(imp._ref_rc("H9"), (8, 7))
        self.assertEqual(imp._ref_rc("AA10"), (9, 26))


if __name__ == "__main__":
    unittest.main(verbosity=2)
