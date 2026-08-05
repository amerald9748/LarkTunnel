# -*- coding: utf-8 -*-
"""Unit tests for appointment_sync.commit() — phase ordering, ISA-group
dedup, signature freshness, partial approval, and client_token idempotency.
Offline: lark._api is replaced with a recorder that APPLIES writes to the
in-memory fixture, so the read-back verification phase is tested for real.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appointment_sync as sync  # noqa: E402
import lark_client as lark       # noqa: E402
from test_planner import (Fixture, fake_field_meta, fake_table_id,  # noqa: E402
                          make_31, make_56, make_trip, T31, T56, T54)

LINE_A = "OOCU9020713B YVR2 4 141 9903350996 07/30/2026 13:00 PDT"
LINE_B = "TCNU4251020B YVR2 1 4 9903350996 07/30/2026 13:00 PDT"


class WritingFixture(Fixture):
    """Fixture whose fake _api APPLIES batch writes, so Phase-5 read-back
    verification sees exactly what a real Base would contain."""

    def __init__(self):
        super().__init__()
        self.calls = []          # (kind, table, payload, client_token)
        self.seq = 0

    def _rid(self, prefix):
        self.seq += 1
        return f"{prefix}{self.seq}"

    def api(self, method, path, payload=None, query=None):
        token = (query or {}).get("client_token")
        table = path.split("/tables/")[1].split("/")[0]
        if path.endswith("/records/batch_create"):
            self.calls.append(("create", table, payload, token))
            made = []
            for r in payload["records"]:
                rid = self._rid("new")
                made.append({"record_id": rid})
                if table == T56:
                    f = dict(r["fields"])
                    if "复制时间列" in f:
                        f["复制时间列"] = [{"text": f["复制时间列"]}]
                    self.rows56.append({"record_id": rid, "fields": f})
                elif table == T54:
                    self.trips[rid] = dict(r["fields"])
            return {"records": made}
        if path.endswith("/records/batch_update"):
            self.calls.append(("update", table, payload, token))
            for r in payload["records"]:
                rid, fields = r["record_id"], r["fields"]
                if table == T31:
                    row = next(x for x in self.rows31 if x["record_id"] == rid)
                    for k, v in fields.items():
                        row["fields"][k] = ([{"text": v}] if k == "实际板数"
                                            else {"link_record_ids": v}
                                            if isinstance(v, list) else v)
                elif table == T56:
                    row = next(x for x in self.rows56 if x["record_id"] == rid)
                    for k, v in fields.items():
                        row["fields"][k] = ([{"text": v}] if k == "复制时间列" else v)
                elif table == T54:
                    self.trips[rid].update(
                        {k: {"link_record_ids": v} for k, v in fields.items()})
            return {"records": payload["records"]}
        raise AssertionError(f"unexpected write path {path}")


class CommitCase(unittest.TestCase):
    def setUp(self):
        self.fx = WritingFixture()
        self.patches = [
            mock.patch.object(sync, "_search", self.fx.search),
            mock.patch.object(sync, "_batch_get", self.fx.batch_get),
            mock.patch.object(lark, "field_meta", fake_field_meta),
            mock.patch.object(lark, "table_id", fake_table_id),
            mock.patch.object(lark, "ENV", "prod"),
            mock.patch.object(lark, "_api", self.fx.api),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def approve_all(self, planned):
        return [{"line_no": r["line_no"], "sig": r["sig"]}
                for r in planned["rows"] if r["actions"] and not r["blockers"]]


class TestCommitPhases(CommitCase):
    def test_group_creates_one_trip_links_twice(self):
        """②计划同步 on a group whose appointment already exists (made by ①):
        ONE 出库计划 create, both shipments linked, NO 5.6 create at all."""
        self.fx.rows31 = [make_31("a"), make_31("b", awb="TCNU4251020B", dest="YVR2")]
        self.fx.rows56 = [make_56(isa=9903350996)]        # created earlier by ①
        planned = sync.plan("VAST", LINE_A + "\n" + LINE_B)
        res = sync.commit("VAST", LINE_A + "\n" + LINE_B,
                          self.approve_all(planned), "prod")

        kinds = [(k, t) for k, t, _, _ in self.fx.calls]
        # NEVER a 5.6 create from this flow — that is ①'s exclusive job
        self.assertEqual(kinds.count(("create", T56)), 0)
        self.assertEqual(kinds.count(("create", T54)), 1)
        self.assertEqual(kinds.count(("update", T31)), 1)
        # phase order: 5.x 出库计划 -> 3.1
        self.assertEqual(kinds, [("create", T54), ("update", T31)])

        u31 = next(p for k, t, p, _ in self.fx.calls if (k, t) == ("update", T31))
        self.assertEqual(len(u31["records"]), 2)          # both shipments linked
        for r in u31["records"]:
            self.assertIn("5.4 VAST-VAN-01", r["fields"])  # link written from 3.1 side
            self.assertIn("实际板数", r["fields"])          # fill merged into SAME update

        # trip created WITH its 预约信息 link (5.6 back-link auto-fills in Lark)
        ctrip = next(p for k, t, p, _ in self.fx.calls if (k, t) == ("create", T54))
        self.assertEqual(list(ctrip["records"][0]["fields"]) , ["预约信息"])

        # read-back verification passed for both rows
        done = [r for r in res["rows"] if r.get("approved")]
        self.assertEqual(len(done), 2)
        for r in done:
            self.assertTrue(r["commit"]["done"])
            self.assertTrue(r["commit"]["verified"], r["commit"]["checks"])

    def test_4a_mismatch_updates_linked_record_only(self):
        self.fx.rows31 = [make_31(actual="4", plan_links=["trip1"])]
        self.fx.rows56 = [make_56(isa=1111111111, trip_links=["trip1"])]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["r56a"])
        planned = sync.plan("VAST", LINE_A)
        res = sync.commit("VAST", LINE_A, self.approve_all(planned), "prod")

        kinds = [(k, t) for k, t, _, _ in self.fx.calls]
        self.assertEqual(kinds, [("update", T56)])        # ONLY the 5.6 edit
        u56 = self.fx.calls[0][2]["records"][0]
        self.assertEqual(u56["record_id"], "r56a")        # the LINKED record
        self.assertEqual(u56["fields"]["ISA"], 9903350996)
        # STORAGE format is month-first (operator-specified 2026-08-04)
        self.assertEqual(u56["fields"]["复制时间列"], "07/30/2026 13:00")
        row = next(r for r in res["rows"] if r.get("approved"))
        self.assertTrue(row["commit"]["verified"])

    def test_relink_commits_and_readback_verifies(self):
        # newest-wins: pasted ISA exists on ANOTHER 5.6 record -> the trip's
        # 预约信息 is repointed there (+ its time updated); NO 5.6 create, and
        # the previously linked record is left untouched.
        self.fx.rows31 = [make_31(actual="4", plan_links=["trip1"])]
        self.fx.rows56 = [
            make_56("linked", isa=1111111111, trip_links=["trip1"]),
            make_56("other", isa=9903350996, time="2026/07/30 15:00"),
        ]
        self.fx.trips["trip1"] = make_trip(inv_ids=["r31a"], isa_ids=["linked"])
        planned = sync.plan("VAST", LINE_A)
        res = sync.commit("VAST", LINE_A, self.approve_all(planned), "prod")

        kinds = [(k, t) for k, t, _, _ in self.fx.calls]
        self.assertEqual(kinds, [("update", T54), ("update", T56)])
        # the trip now points at 'other'
        self.assertEqual(self.fx.trips["trip1"]["预约信息"]["link_record_ids"],
                         ["other"])
        # the TARGET record got the pasted time; the old record is untouched
        upd56 = next(p for k, t, p, _ in self.fx.calls if t == T56)
        self.assertEqual(upd56["records"][0]["record_id"], "other")
        linked = next(r for r in self.fx.rows56 if r["record_id"] == "linked")
        self.assertEqual(linked["fields"]["ISA"], 1111111111)
        row = next(r for r in res["rows"] if r.get("approved"))
        self.assertTrue(row["commit"]["verified"], row["commit"]["checks"])
        self.assertTrue(any(c["what"] == "出库计划改挂预约" and c["ok"]
                            for c in row["commit"]["checks"]))

    def test_partial_approval_writes_only_approved(self):
        self.fx.rows31 = [make_31("a"), make_31("b", awb="TCNU4251020B", dest="YVR2")]
        text = "OOCU9020713B YVR2 4 141\nTCNU4251020B YVR2 1 4"
        planned = sync.plan("VAST", text)
        first = [a for a in self.approve_all(planned) if a["line_no"] == 1]
        sync.commit("VAST", text, first, "prod")
        u31 = next(p for k, t, p, _ in self.fx.calls if (k, t) == ("update", T31))
        self.assertEqual(len(u31["records"]), 1)
        self.assertEqual(u31["records"][0]["record_id"], "a")

    def test_sig_mismatch_skips_and_writes_nothing(self):
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56(isa=9903350996)]        # appointment exists
        planned = sync.plan("VAST", LINE_A)
        approvals = self.approve_all(planned)
        # the Base changes between review and click:
        self.fx.rows31[0]["fields"]["实际板数"] = [{"text": "9"}]
        res = sync.commit("VAST", LINE_A, approvals, "prod")
        self.assertEqual(self.fx.calls, [])               # nothing written
        row = next(r for r in res["rows"] if r.get("approved"))
        self.assertIn("情况已变化", row["commit"]["skipped"])

    def test_env_mismatch_refused(self):
        with self.assertRaises(lark.LarkError):
            sync.commit("VAST", LINE_A, [], "dev")        # server is 'prod'

    def test_recommit_converges_instead_of_duplicating(self):
        # Operator-level retry safety: a re-click after a successful commit
        # re-plans, finds the created 出库计划, and plans NO further creates.
        self.fx.rows31 = [make_31()]
        self.fx.rows56 = [make_56(isa=9903350996)]        # appointment exists
        planned = sync.plan("VAST", LINE_A)
        sync.commit("VAST", LINE_A, self.approve_all(planned), "prod")
        first_creates = sum(1 for k, _, _, _ in self.fx.calls if k == "create")
        self.assertEqual(first_creates, 1)                # one 出库计划 only
        replanned = sync.plan("VAST", LINE_A)
        row = replanned["rows"][0]
        self.assertEqual(row["plan"]["status"], "has_plan_match")
        self.assertEqual(row["actions"], [])              # nothing left to write
        sync.commit("VAST", LINE_A, self.approve_all(replanned), "prod")
        self.assertEqual(sum(1 for k, _, _, _ in self.fx.calls if k == "create"),
                         first_creates)                   # NO new creates on retry

    def test_unapproved_rows_untouched(self):
        self.fx.rows31 = [make_31()]
        sync.plan("VAST", LINE_A)
        res = sync.commit("VAST", LINE_A, [], "prod")     # nothing approved
        self.assertEqual(self.fx.calls, [])
        self.assertFalse(any(r.get("approved") for r in res["rows"]))

    def test_client_token_is_fresh_uuid_v4(self):
        # Feishu requires v4 format (1254037) AND rejects replayed tokens
        # even after the records are deleted (1254608) — so tokens must be
        # fresh per request, never repeated.
        import uuid as _uuid
        t1, t2 = sync._ctoken("a", "b"), sync._ctoken("a", "b")
        self.assertNotEqual(t1, t2)                       # fresh every time
        u = _uuid.UUID(t1)
        self.assertEqual(u.version, 4)
        self.assertEqual(u.variant, _uuid.RFC_4122)


if __name__ == "__main__":
    unittest.main(verbosity=2)
