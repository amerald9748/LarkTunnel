# -*- coding: utf-8 -*-
"""Unit tests for the deletion-audit pipeline (audit_store + watcher handling).
Offline — no network. Run:  python -m unittest discover webapp/tests -v"""
import os
import sys
import json
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                      # webapp/

# Isolate the store BEFORE importing it (module reads env at import).
_TMP = tempfile.mkdtemp(prefix="lark-audit-test-")
os.environ["LARK_AUDIT_DB"] = os.path.join(_TMP, "audit-test.db")

import audit_store  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                "tools", "deletion-watcher"))
import watcher  # noqa: E402


def deletion_event(event_id="evt-1", table_id="tblFAKE31", record_id="recDEAD01",
                   awb="BEAU6279991"):
    """Shape mirrors P2DriveFileBitableRecordChangedV1 after _to_plain()."""
    return {
        "header": {"event_id": event_id, "event_type":
                   "drive.file.bitable_record_changed_v1",
                   "create_time": "1756900000000"},
        "event": {
            "file_type": "bitable",
            "file_token": "BASETOKEN",
            "table_id": table_id,
            "revision": 4242,
            "operator_id": {"open_id": "ou_operator1", "user_id": "u123",
                            "union_id": "on_x"},
            "update_time": 1756900000,
            "action_list": [{
                "record_id": record_id,
                "action": "record_deleted",
                "before_value": [
                    {"field_id": "fldAWB", "field_value": json.dumps(
                        [{"text": awb, "type": "text"}], ensure_ascii=False)},
                    {"field_id": "fldROUTE", "field_value": "\"YVR4\""},
                    {"field_id": "fldBOX", "field_value": "383"},
                ],
                "after_value": [],
            }],
        },
    }


class TestFlatten(unittest.TestCase):
    def test_decodes_nested_json_strings(self):
        leaves = audit_store.flatten_leaves(
            {"a": json.dumps([{"text": "BEAU6279991"}]), "b": 12.0})
        self.assertIn("BEAU6279991", leaves)
        self.assertIn("12", leaves)

    def test_search_text_lowercased(self):
        t = audit_store.build_search_text({"v": "YVR4"}, "RecXYZ")
        self.assertIn("yvr4", t)
        self.assertIn("recxyz", t)


class TestIngestAndSearch(unittest.TestCase):
    def setUp(self):
        watcher.handle_event(deletion_event())    # default policy: add + delete

    def test_deletion_stored_for_any_table(self):
        rows = audit_store.search(action="record_deleted")
        self.assertTrue(any(r["record_id"] == "recDEAD01" for r in rows))
        row = [r for r in rows if r["record_id"] == "recDEAD01"][0]
        self.assertEqual(row["operator_open_id"], "ou_operator1")
        self.assertEqual(row["ts"], 1756900000)
        self.assertEqual(row["table_id"], "tblFAKE31")

    def test_searchable_by_vanished_field_value(self):
        # THE core requirement: record no longer exists in the table, but its
        # field values (柜号 etc.) remain findable in the audit log.
        rows = audit_store.search(q="beau6279991")
        self.assertTrue(any(r["record_id"] == "recDEAD01" for r in rows))
        # multi-term = AND
        rows = audit_store.search(q="beau6279991 yvr4")
        self.assertTrue(any(r["record_id"] == "recDEAD01" for r in rows))
        rows = audit_store.search(q="beau6279991 nosuchvalue")
        self.assertFalse(any(r["record_id"] == "recDEAD01" for r in rows))

    def test_raw_json_preserves_before_value(self):
        row = audit_store.search(record_id="recDEAD01")[0]
        before = row["raw"]["action"]["before_value"]
        self.assertEqual(before[0]["field_id"], "fldAWB")

    def test_redelivery_is_deduped(self):
        n0 = len(audit_store.search(record_id="recDEAD01", action="record_deleted"))
        watcher.handle_event(deletion_event())            # same event_id again
        n1 = len(audit_store.search(record_id="recDEAD01", action="record_deleted"))
        self.assertEqual(n0, n1)

    def test_edits_not_tracked_by_default(self):
        # Operator-specified policy 2026-09-03: only creations + deletions.
        ev = deletion_event(event_id="evt-edit", record_id="recEDIT01")
        ev["event"]["action_list"][0]["action"] = "record_edited"
        watcher.handle_event(ev)
        self.assertEqual(audit_store.search(record_id="recEDIT01"), [])

    def test_addition_tracked_by_default(self):
        ev = deletion_event(event_id="evt-add1", record_id="recADD01")
        ev["event"]["action_list"][0]["action"] = "record_added"
        watcher.handle_event(ev)
        rows = audit_store.search(record_id="recADD01", action="record_added")
        self.assertEqual(len(rows), 1)

    def test_edit_stored_when_explicitly_tracked(self):
        # Re-enabling edits = add the action to TRACKED_ACTIONS; verify the knob.
        ev = deletion_event(event_id="evt-edit2", record_id="recEDIT02")
        ev["event"]["action_list"][0]["action"] = "record_edited"
        watcher.handle_event(ev, tracked={"record_edited"})
        rows = audit_store.search(record_id="recEDIT02", action="record_edited")
        self.assertEqual(len(rows), 1)

    def test_filter_by_table(self):
        rows = audit_store.search(table_id="tblNOPE")
        self.assertEqual(rows, [])


class TestOptionResolution(unittest.TestCase):
    def test_option_names_searchable_via_resolver(self):
        ev = deletion_event(event_id="evt-opt1", record_id="recOPT01")
        ev["event"]["action_list"][0]["before_value"].append(
            {"field_id": "fldDEST", "field_value": "\"optRoute99\""})
        watcher.handle_event(ev, resolver=lambda tid: {"optRoute99": "YYC4"})
        rows = audit_store.search(q="yyc4", action="record_deleted")
        self.assertTrue(any(r["record_id"] == "recOPT01" for r in rows))

    def test_no_resolver_still_ingests(self):
        ev = deletion_event(event_id="evt-opt2", record_id="recOPT02")
        watcher.handle_event(ev, resolver=None)
        self.assertEqual(len(audit_store.search(record_id="recOPT02")), 1)


class TestPrettyValue(unittest.TestCase):
    def test_data_bus_type_carrier(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import audit_view
        v = audit_view._pretty_value(json.dumps(
            {"data": ["5.75板 - 占比77.9%"], "bus_type": [201]}))
        self.assertEqual(v, "5.75板 - 占比77.9%")

    def test_option_id_mapped(self):
        import audit_view
        v = audit_view._pretty_value("\"optAbc\"", {"optAbc": "VAST"})
        self.assertEqual(v, "VAST")

    def test_autonumber(self):
        import audit_view
        v = audit_view._pretty_value(json.dumps(
            {"number": "113944", "sequence": "113944"}))
        self.assertEqual(v, "113944")


class TestOperatorAliases(unittest.TestCase):
    def test_alias_file_hot_reloads_and_skips_meta_keys(self):
        import audit_view
        alias_file = os.path.join(_TMP, "operators-hotreload.json")
        with open(alias_file, "w", encoding="utf-8") as f:
            json.dump({"_readme": "x", "ou_alias1": "AndyLiu"}, f)
        old_path = audit_view.OPERATORS_AUTO_PATH
        audit_view.OPERATORS_AUTO_PATH = alias_file
        audit_view._ops_cache.clear()
        try:
            self.assertEqual(audit_view.resolve_operator("ou_alias1"), "AndyLiu")
            # underscore-prefixed keys are ignored
            self.assertNotIn("_readme", audit_view._local_operators())
            # rewrite the file with a different mtime -> new value picked up
            os.utime(alias_file, (time_now := __import__("time").time() + 2,
                                  time_now))
            with open(alias_file, "w", encoding="utf-8") as f:
                json.dump({"ou_alias1": "AndyL2"}, f)
            os.utime(alias_file, (time_now + 4, time_now + 4))
            self.assertEqual(audit_view.resolve_operator("ou_alias1"), "AndyL2")
        finally:
            audit_view.OPERATORS_AUTO_PATH = old_path
            audit_view._ops_cache.clear()

    def test_api_failure_not_cached_forever(self):
        import audit_view
        calls = {"n": 0}

        def fake_api(*a, **k):
            calls["n"] += 1
            raise RuntimeError("41050")
        old = audit_view.lark._api
        audit_view.lark._api = fake_api
        try:
            self.assertEqual(audit_view.resolve_operator("ou_hidden1"), "ou_hidden1")
            audit_view.resolve_operator("ou_hidden1")   # within retry TTL
            self.assertEqual(calls["n"], 1)              # no hammering
            audit_view._name_fail_at["ou_hidden1"] = 0   # TTL elapsed
            audit_view.resolve_operator("ou_hidden1")
            self.assertEqual(calls["n"], 2)              # retried
        finally:
            audit_view.lark._api = old
            audit_view._name_fail_at.pop("ou_hidden1", None)


class TestTableLabels(unittest.TestCase):
    def test_live_names_with_registry_override(self):
        import audit_view
        old_list, old_cfg = audit_view.lark.list_tables, audit_view.lark.config_values
        audit_view.lark.list_tables = lambda: {
            "tblQTDrmAVDFKB1W": "7.1 应收", "tblbJiIHMwND2OGX": "3.1 库存信息"}
        audit_view.lark.config_values = lambda: {
            "tables": {"3.1": "tblbJiIHMwND2OGX"}, "dev_tables": {}}
        try:
            labels = audit_view._table_labels()
            self.assertEqual(labels["tblQTDrmAVDFKB1W"], "7.1 应收")   # live name
            self.assertEqual(labels["tblbJiIHMwND2OGX"], "3.1")        # registry wins
        finally:
            audit_view.lark.list_tables = old_list
            audit_view.lark.config_values = old_cfg


class TestIdentityHarvest(unittest.TestCase):
    def test_walker_extracts_identities_from_any_shape(self):
        import identity_harvest as ih
        payload = [{
            "record_id": "rec1",
            "created_by": {"id": "ou_creator1", "name": "AndyLiu", "email": ""},
            "fields": {
                "负责人": [{"id": "ou_person1", "name": "张三", "en_name": "San"}],
                "备注": "text",
                "嵌套": json.dumps({"id": "ou_json1", "name": "JsonUser"}),
                "identity": {"id": {"open_id": "ou_fiv1"}, "name": "FivUser"},
            },
            "last_modified_by": {"id": "ou_mod1", "en_name": "OnlyEn"},
        }]
        out = ih.walk_identities(payload)
        self.assertEqual(out.get("ou_creator1"), "AndyLiu")
        self.assertEqual(out.get("ou_person1"), "张三")
        self.assertEqual(out.get("ou_json1"), "JsonUser")
        self.assertEqual(out.get("ou_fiv1"), "FivUser")
        self.assertEqual(out.get("ou_mod1"), "OnlyEn")
        self.assertNotIn("rec1", out)

    def test_auto_alias_resolves(self):
        import audit_view
        auto_f = os.path.join(_TMP, "operators-auto.json")
        with open(auto_f, "w", encoding="utf-8") as f:
            json.dump({"_generated": "x", "ou_only_auto": "AutoOnly"}, f)
        olda = audit_view.OPERATORS_AUTO_PATH
        audit_view.OPERATORS_AUTO_PATH = auto_f
        audit_view._ops_cache.clear()
        try:
            self.assertEqual(audit_view.resolve_operator("ou_only_auto"), "AutoOnly")
        finally:
            audit_view.OPERATORS_AUTO_PATH = olda
            audit_view._ops_cache.clear()


class TestDeleteEvents(unittest.TestCase):
    def test_selective_delete(self):
        watcher.handle_event(deletion_event(event_id="evt-del1", record_id="recPRUNE1"))
        watcher.handle_event(deletion_event(event_id="evt-del2", record_id="recPRUNE2"))
        keep = audit_store.search(record_id="recPRUNE2")[0]["id"]
        gone = audit_store.search(record_id="recPRUNE1")[0]["id"]
        n = audit_store.delete_events([gone], vacuum=False)
        self.assertEqual(n, 1)
        self.assertEqual(audit_store.search(record_id="recPRUNE1"), [])
        self.assertEqual(audit_store.search(record_id="recPRUNE2")[0]["id"], keep)

    def test_delete_empty_ids_noop(self):
        self.assertEqual(audit_store.delete_events([]), 0)

    def test_conditional_delete_with_preview(self):
        watcher.handle_event(deletion_event(event_id="evt-cw1", record_id="recCW1",
                                            table_id="tblCONDDEL"))
        watcher.handle_event(deletion_event(event_id="evt-cw2", record_id="recCW2",
                                            table_id="tblCONDDEL"))
        n = audit_store.count_where(table_id="tblCONDDEL")
        self.assertEqual(n, 2)
        # date-range narrowing: events are at ts=1756900000
        self.assertEqual(audit_store.count_where(table_id="tblCONDDEL",
                                                 ts_to=1756899999), 0)
        deleted = audit_store.delete_where(vacuum=False, table_id="tblCONDDEL",
                                           ts_from=1756900000)
        self.assertEqual(deleted, 2)
        self.assertEqual(audit_store.count_where(table_id="tblCONDDEL"), 0)

    def test_conditional_delete_refuses_empty_conditions(self):
        with self.assertRaises(ValueError):
            audit_store.delete_where(vacuum=False)
        with self.assertRaises(ValueError):
            audit_store.count_where()


class TestStatus(unittest.TestCase):
    def test_status_counts(self):
        watcher.handle_event(deletion_event(event_id="evt-s1",
                                            record_id="recS1"))
        st = audit_store.status()
        self.assertGreaterEqual(st["total"], 1)
        self.assertGreaterEqual(st["deleted"], 1)
        self.assertIn("watcher_alive", st)   # liveness key present (value depends
        #                                      on whether the heartbeat test ran)

    def test_heartbeat_flips_alive(self):
        audit_store.heartbeat()
        self.assertTrue(audit_store.status()["watcher_alive"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
