# -*- coding: utf-8 -*-
"""Unit tests for appointment_sync.parse_line / parse_batch / norm_time.
Offline — no network. Run:  python -m unittest discover webapp/tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appointment_sync as sync  # noqa: E402


class TestNormTime(unittest.TestCase):
    def test_month_first(self):        # operator paste format
        self.assertEqual(sync.norm_time("07/30/2026 13:00"), "2026/07/30 13:00")

    def test_year_first(self):         # 5.6 stored format
        self.assertEqual(sync.norm_time("2026/07/30 13:00"), "2026/07/30 13:00")

    def test_single_digits(self):
        self.assertEqual(sync.norm_time("7/3/2026 8:05"), "2026/07/03 08:05")

    def test_dashes(self):
        self.assertEqual(sync.norm_time("2026-07-30 13:00"), "2026/07/30 13:00")

    def test_invalid(self):
        for bad in (None, "", "13:00", "07/30/26 13:00", "2026/07/30", "30/07/2026 25:00",
                    "13/45/2026 10:00"):
            self.assertIsNone(sync.norm_time(bad), bad)

    def test_roundtrip_equality(self):
        # The SAME canonicalizer runs on both sides of the match comparison.
        self.assertEqual(sync.norm_time("07/27/2026 08:00"), sync.norm_time("2026/07/27 08:00"))


class TestParseLine(unittest.TestCase):
    # ---- the user's three canonical examples --------------------------------
    def test_example_1_basic(self):
        p = sync.parse_line("OOCU9020713B\tYEG2\t4\t141")
        self.assertNotIn("error", p)
        self.assertEqual((p["awb"], p["dest"], p["pallets"], p["boxes"]),
                         ("OOCU9020713B", "YEG2", 4, 141))
        self.assertIsNone(p["isa"])

    def test_example_2_basic(self):
        p = sync.parse_line("TCNU4251020B\tYEG1\t1\t4")
        self.assertNotIn("error", p)
        self.assertEqual(p["pallets"], 1)

    def test_example_3_missing_pallets_rejected(self):
        p = sync.parse_line("CSGU6249922\tYVR4\t\t96")
        self.assertIn("error", p)
        self.assertIn("空列", p["error"])

    def test_example_3_space_variant_rejected(self):
        # Same ambiguity when the empty cell collapses into plain spaces.
        p = sync.parse_line("CSGU6249922 YVR4  96")
        self.assertIn("error", p)

    def test_full_form_with_tz(self):
        p = sync.parse_line("OOCU9020713B\tYEG2\t4\t141 7403350996\t 07/30/2026 13:00 PDT")
        self.assertNotIn("error", p)
        self.assertEqual(p["isa"], 7403350996)
        self.assertEqual(p["time"], "2026/07/30 13:00")
        self.assertEqual(p["tz"], "PDT")

    def test_full_form_example_2(self):
        p = sync.parse_line("CXDU2185835\tYEG2\t2\t135\t147204024984\t 07/27/2026 08:00 MDT")
        self.assertNotIn("error", p)
        self.assertEqual(p["isa"], 147204024984)
        self.assertEqual(p["time"], "2026/07/27 08:00")

    def test_full_form_without_tz(self):
        p = sync.parse_line("CXDU2185835 YEG2 2 135 147204024984 07/27/2026 08:00")
        self.assertNotIn("error", p)
        self.assertIsNone(p["tz"])

    # ---- rejection rules -----------------------------------------------------
    def test_isa_without_time_rejected(self):
        p = sync.parse_line("OOCU9020713B YEG2 4 141 7403350996")
        self.assertIn("error", p)

    def test_isa_with_date_but_no_time_rejected(self):
        p = sync.parse_line("OOCU9020713B YEG2 4 141 7403350996 07/30/2026")
        self.assertIn("error", p)

    def test_isa_slid_into_boxes_position(self):
        # pallets missing -> ISA lands in the 箱数 slot -> must be caught
        p = sync.parse_line("CSGU6249922 YVR4 96 7403350996 07/30/2026 13:00 PDT")
        self.assertIn("error", p)

    def test_dest_is_shape_checked_only(self):
        # The parser accepts any plausible warehouse-point CODE (YEG0, YYZ4,
        # XCAB, …) — real validity is decided against LIVE Lark options at
        # plan time (5.6 目的地 for creates; 3.1 search for lookups). A
        # hardcoded YEG/YYC/YVR list wrongly rejected the live option XCAB
        # (operator report 2026-08-04).
        for ok in ("YEG0", "YVR10", "YYZ4", "ABC1", "XCAB"):
            p = sync.parse_line(f"OOCU9020713B {ok} 4 141")
            self.assertNotIn("error", p, ok)
            self.assertEqual(p["dest"], ok)
        # but plainly-not-a-code tokens still fail the shape check
        for bad in ("8EG2", "Y", "YEG2YEG2YEG2"):
            p = sync.parse_line(f"OOCU9020713B {bad} 4 141")
            self.assertIn("error", p, bad)

    def test_store_time_month_first(self):
        # 复制时间列 STORES month-first; canonical stays year-first internally
        self.assertEqual(sync.store_time("2026/08/03 13:00"), "08/03/2026 13:00")
        # round-trip: stored form re-canonicalizes to the same internal value
        self.assertEqual(sync.norm_time(sync.store_time("2026/08/03 13:00")),
                         "2026/08/03 13:00")
        # legacy year-first strings read back fine too
        self.assertEqual(sync.norm_time("2025/03/18 20:00"), "2025/03/18 20:00")

    def test_lowercase_dest_ok(self):
        p = sync.parse_line("oocu9020713b yeg2 4 141")
        self.assertNotIn("error", p)
        self.assertEqual(p["dest"], "YEG2")
        self.assertEqual(p["awb"], "OOCU9020713B")

    def test_non_numeric_pallets(self):
        p = sync.parse_line("OOCU9020713B YEG2 4P 141")
        self.assertIn("error", p)

    def test_zero_pallets_rejected(self):
        p = sync.parse_line("OOCU9020713B YEG2 0 141")
        self.assertIn("error", p)

    def test_bad_isa_length(self):
        p = sync.parse_line("OOCU9020713B YEG2 4 141 12345 07/30/2026 13:00")
        self.assertIn("error", p)

    def test_bad_time(self):
        p = sync.parse_line("OOCU9020713B YEG2 4 141 7403350996 07/30/26 13:00")
        self.assertIn("error", p)

    def test_bad_tz(self):
        p = sync.parse_line("OOCU9020713B YEG2 4 141 7403350996 07/30/2026 13:00 P8T")
        self.assertIn("error", p)

    def test_nbsp_and_fullwidth_space(self):
        p = sync.parse_line("OOCU9020713B YEG2　4 141")
        self.assertNotIn("error", p)
        self.assertEqual(p["pallets"], 4)

    def test_air_waybill_style(self):
        p = sync.parse_line("093-9992123 YYC3 12 300")
        self.assertNotIn("error", p)
        self.assertEqual(p["awb"], "093-9992123")


class TestParseBatch(unittest.TestCase):
    def test_blank_lines_skipped(self):
        rows = sync.parse_batch("\nOOCU9020713B YEG2 4 141\n\n  \nTCNU4251020B YEG1 1 4\n")
        self.assertEqual([r["line_no"] for r in rows], [2, 5])

    def test_duplicate_awb_dest_allowed_split_shipments(self):
        # Same 柜号+路线 twice is REAL data (split shipments) — the parser
        # accepts both; the same-record guard at plan level protects writes.
        rows = sync.parse_batch("OOCU9733972 YEG1 8 706\nOOCU9733972 YEG1 23 0")
        self.assertNotIn("error", rows[0])
        self.assertNotIn("error", rows[1])

    def test_zero_boxes_allowed(self):
        # 箱数 0 is real (loose/overflow shipments) and a disambiguation key
        p = sync.parse_line("OOCU7443104 YYC4 13 0 129713028975 07/31/2026 18:00 MDT")
        self.assertNotIn("error", p)
        self.assertEqual(p["boxes"], 0)

    def test_same_awb_different_dest_ok(self):
        rows = sync.parse_batch("OOCU9020713B YEG2 4 141\nOOCU9020713B YEG1 5 99")
        self.assertNotIn("error", rows[0])
        self.assertNotIn("error", rows[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
