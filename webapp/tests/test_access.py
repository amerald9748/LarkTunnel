# -*- coding: utf-8 -*-
"""Unit tests for access_control (授权表 verification, grace, owner tools).
Offline: lark._api is mocked; settings live in a temp LARK_HOME.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import shutil
import datetime
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app_settings      # noqa: E402
import access_control as ac  # noqa: E402
import lark_client as lark   # noqa: E402

TBL = "tblAUTH"


def member(key, name, status="启用", expiry="", rid="recM1"):
    return {"record_id": rid, "fields": {"授权码": key, "姓名": name, "状态": status,
                                         "到期": expiry}}


class FakeApi:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, method, path, payload=None, query=None):
        self.calls.append((method, path, payload))
        if path.endswith("/records/search"):
            cond = ((payload or {}).get("filter") or {}).get("conditions") or []
            if cond:
                want = cond[0]["value"][0]
                return {"items": [r for r in self.rows if r["fields"]["授权码"] == want]}
            return {"items": list(self.rows), "has_more": False}
        if method == "PUT":
            rid = path.rsplit("/", 1)[1]
            for r in self.rows:
                if r["record_id"] == rid:
                    r["fields"].update(payload["fields"])
            return {}
        if path.endswith("/records"):
            rid = f"recNew{len(self.rows)}"
            self.rows.append({"record_id": rid, "fields": dict(payload["fields"])})
            return {"record": {"record_id": rid}}
        if path.endswith("/tables"):
            return {"table_id": "tblCREATED"}
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lt-access-")
        self._env = dict(os.environ)
        os.environ["LARK_HOME"] = self.home
        self.rows = [member("LT-good", "王五"), member("LT-off", "李四", "停用", rid="recM2"),
                     member("LT-exp", "赵六", "启用", "2020-01-01", rid="recM3")]
        self.api = FakeApi(self.rows)
        self.patches = [mock.patch.object(lark, "_api", self.api),
                        mock.patch.object(lark, "config_values",
                                          lambda: {"base_token": "base", "tables": {}})]
        for p in self.patches:
            p.start()
        ac.invalidate()
        ac._state.update(ok=None, verified_at=0.0, name="", reason="", record_id=None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.home, ignore_errors=True)


class TestSingleMode(Base):
    def test_no_table_means_open(self):
        app_settings.update_settings({"operator_name": "老板"})
        st = ac.status()
        self.assertEqual(st["mode"], "single")
        self.assertTrue(st["ok"])
        self.assertEqual(st["name"], "老板")
        self.assertEqual(self.api.calls, [])          # no Feishu I/O at all


class TestTeamMode(Base):
    def setUp(self):
        super().setUp()
        app_settings.update_settings({"auth_table": TBL})

    def test_active_key_ok_and_heartbeat(self):
        app_settings.update_settings({"access_key": "LT-good"})
        st = ac.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["name"], "王五")
        # heartbeat stamped 最近使用 on the member row
        self.assertTrue(self.rows[0]["fields"].get("最近使用"))

    def test_disabled_key_locked(self):
        app_settings.update_settings({"access_key": "LT-off"})
        st = ac.status()
        self.assertFalse(st["ok"])
        self.assertIn("停用", st["reason"])

    def test_expired_key_locked(self):
        app_settings.update_settings({"access_key": "LT-exp"})
        self.assertFalse(ac.status()["ok"])

    def test_unknown_or_missing_key_locked(self):
        app_settings.update_settings({"access_key": "LT-nope"})
        self.assertFalse(ac.status()["ok"])
        app_settings.update_settings({"access_key": ""})
        ac.invalidate()
        self.assertIn("未填写授权码", ac.status(force=True)["reason"])

    def test_result_cached_until_invalidate(self):
        app_settings.update_settings({"access_key": "LT-good"})
        ac.status()
        n = len(self.api.calls)
        ac.status()
        self.assertEqual(len(self.api.calls), n)       # cached
        ac.status(force=True)
        self.assertGreater(len(self.api.calls), n)

    def test_network_failure_keeps_verified_user_within_grace(self):
        app_settings.update_settings({"access_key": "LT-good"})
        self.assertTrue(ac.status()["ok"])

        def boom(*a, **k):
            raise lark.LarkError("net down", code="net")
        with mock.patch.object(lark, "_api", boom):
            st = ac.status(force=True)
        self.assertTrue(st["ok"])
        self.assertIn("沿用", st["reason"])

    def test_network_failure_never_verified_locks(self):
        app_settings.update_settings({"access_key": "LT-good"})

        def boom(*a, **k):
            raise lark.LarkError("net down", code="net")
        with mock.patch.object(lark, "_api", boom):
            st = ac.status(force=True)
        self.assertFalse(st["ok"])

    def test_owner_tools(self):
        out = ac.issue_key("新人", note="仓务", expiry="2027-01-01")
        self.assertTrue(out["key"].startswith("LT-"))
        members = ac.list_members()
        self.assertIn("新人", [m["name"] for m in members])
        rid = out["record_id"]
        ac.set_status(rid, False)
        self.assertEqual(next(m for m in ac.list_members()
                              if m["record_id"] == rid)["status"], "停用")
        # the freshly issued key now locks
        app_settings.update_settings({"access_key": out["key"]})
        self.assertFalse(ac.status(force=True)["ok"])

    def test_create_table_saves_id(self):
        app_settings.update_settings({"auth_table": ""})
        tid = ac.create_auth_table()
        self.assertEqual(tid, "tblCREATED")
        self.assertEqual(app_settings.get_settings()["auth_table"], "tblCREATED")


if __name__ == "__main__":
    unittest.main()
