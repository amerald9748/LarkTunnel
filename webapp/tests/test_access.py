# -*- coding: utf-8 -*-
"""Unit tests for access_control (授权表 verification, roles, grace, owner
tools). Offline: lark._api / field_meta are mocked; settings live in a temp
LARK_HOME; config.js authTable is mocked empty unless a test sets it.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app_settings      # noqa: E402
import apppaths          # noqa: E402
import access_control as ac  # noqa: E402
import lark_client as lark   # noqa: E402

TBL = "tblAUTH"


def member(key, name, status="启用", expiry="", rid="recM1", role=None):
    f = {"授权码": key, "姓名": name, "状态": status, "到期": expiry}
    if role is not None:
        f["角色"] = role
    return {"record_id": rid, "fields": f}


class FakeApi:
    def __init__(self, rows, role_field=True):
        self.rows = rows
        self.calls = []
        self.role_field = role_field          # does the table have 角色?

    def meta(self, _tid):
        names = ["授权码", "姓名", "状态", "到期", "备注", "最近使用"]
        if self.role_field:
            names += ["角色", "最近验证", "设备"]
        names += getattr(self, "extra_fields", [])
        return {"by_name": {n: {"field_name": n} for n in names}, "by_id": {}}

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
        if path.endswith("/fields"):
            self.extra_fields = getattr(self, "extra_fields", []) + [payload["field_name"]]
            if payload["field_name"] == "角色":
                self.role_field = True
            return {"field": {"field_name": payload["field_name"]}}
        if path.endswith("/tables"):
            return {"table_id": "tblCREATED"}
        raise AssertionError(path)


class Base(unittest.TestCase):
    role_field = True

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lt-access-")
        self._env = dict(os.environ)
        os.environ["LARK_HOME"] = self.home
        self.rows = [member("LT-good", "王五", role="管理员"),
                     member("LT-off", "李四", "停用", rid="recM2"),
                     member("LT-exp", "赵六", "启用", "2020-01-01", rid="recM3"),
                     member("LT-mem", "小明", rid="recM4", role="成员"),
                     member("LT-blank", "无角色", rid="recM5")]
        self.api = FakeApi(self.rows, role_field=self.role_field)
        self.cfg = {"base_token": "base", "tables": {}, "auth_table": ""}
        self.patches = [mock.patch.object(lark, "_api", self.api),
                        mock.patch.object(lark, "field_meta", self.api.meta),
                        mock.patch.object(lark, "config_values", lambda: self.cfg)]
        for p in self.patches:
            p.start()
        ac.invalidate()
        ac._state.update(ok=None, verified_at=0.0, name="", reason="", record_id=None,
                         role=None, role_field=None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.home, ignore_errors=True)


class TestSingleMode(Base):
    def test_no_table_means_open_admin(self):
        app_settings.update_settings({"operator_name": "老板"})
        st = ac.status()
        self.assertEqual(st["mode"], "single")
        self.assertTrue(st["ok"])
        self.assertEqual(st["name"], "老板")
        self.assertEqual(st["role"], "管理员")
        self.assertIn("admin", st["perms"])
        self.assertEqual(self.api.calls, [])          # no Feishu I/O at all


class TestTableResolution(Base):
    def test_config_auth_table_enables_team_mode(self):
        self.cfg["auth_table"] = TBL                 # baked into config.js
        app_settings.update_settings({"access_key": "LT-good"})
        st = ac.status()
        self.assertEqual(st["mode"], "team")
        self.assertTrue(st["ok"])
        self.assertEqual(ac.table_source(), "config")

    def test_settings_override_only_when_not_frozen(self):
        self.cfg["auth_table"] = TBL
        app_settings.update_settings({"auth_table": "tblLOCAL"})
        self.assertEqual(ac._table(), "tblLOCAL")
        with mock.patch.object(apppaths, "is_frozen", lambda: True):
            self.assertEqual(ac._table(), TBL)       # exe ignores local override
            self.assertEqual(ac.table_source(), "config")

    def test_blanking_settings_cannot_leave_team_mode_when_baked(self):
        self.cfg["auth_table"] = TBL
        app_settings.update_settings({"auth_table": ""})
        self.assertEqual(ac._table(), TBL)


class TestTeamMode(Base):
    def setUp(self):
        super().setUp()
        app_settings.update_settings({"auth_table": TBL})

    def test_active_key_ok_and_verification_stamped(self):
        app_settings.update_settings({"access_key": "LT-good"})
        st = ac.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["name"], "王五")
        # verification logged on the member row: time, verdict+role, device
        f = self.rows[0]["fields"]
        self.assertTrue(f.get("最近使用"))
        self.assertIn("通过（管理员）", f.get("最近验证"))
        self.assertIn("v", f.get("设备"))

    def test_refusal_is_stamped_too(self):
        app_settings.update_settings({"access_key": "LT-off"})
        self.assertFalse(ac.status()["ok"])
        self.assertIn("拒绝：已停用", self.rows[1]["fields"].get("最近验证"))
        app_settings.update_settings({"access_key": "LT-exp"})
        ac.invalidate()
        self.assertFalse(ac.status(force=True)["ok"])
        self.assertIn("到期", self.rows[2]["fields"].get("最近验证"))

    def test_disabled_key_locked(self):
        app_settings.update_settings({"access_key": "LT-off"})
        st = ac.status()
        self.assertFalse(st["ok"])
        self.assertIn("停用", st["reason"])
        self.assertEqual(st["perms"], [])

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
        self.assertEqual(st["role"], "管理员")          # role survives the blip

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
        self.assertEqual(out["role"], "成员")
        members = ac.list_members()
        self.assertIn("新人", [m["name"] for m in members])
        rid = out["record_id"]
        ac.set_status(rid, False)
        self.assertEqual(next(m for m in ac.list_members()
                              if m["record_id"] == rid)["status"], "停用")
        # the freshly issued key now locks
        app_settings.update_settings({"access_key": out["key"]})
        self.assertFalse(ac.status(force=True)["ok"])

    def test_create_table_saves_id_and_has_role_field(self):
        app_settings.update_settings({"auth_table": ""})
        tid = ac.create_auth_table()
        self.assertEqual(tid, "tblCREATED")
        self.assertEqual(app_settings.get_settings()["auth_table"], "tblCREATED")
        create = next(c for c in self.api.calls if c[1].endswith("/tables"))
        self.assertIn("角色", [f["field_name"] for f in create[2]["table"]["fields"]])


class TestRoles(Base):
    def setUp(self):
        super().setUp()
        app_settings.update_settings({"auth_table": TBL})

    def test_admin_gets_everything(self):
        app_settings.update_settings({"access_key": "LT-good"})
        st = ac.status()
        self.assertEqual(st["role"], "管理员")
        self.assertEqual(set(st["perms"]), set(ac.FEATURES))
        self.assertTrue(st["role_field"])

    def test_member_gets_four_tabs_only(self):
        app_settings.update_settings({"access_key": "LT-mem"})
        st = ac.status()
        self.assertEqual(st["role"], "成员")
        self.assertEqual(set(st["perms"]), {"import", "create", "sync", "verify"})
        for f in ("audit", "query", "parse", "admin"):
            self.assertNotIn(f, st["perms"])

    def test_blank_role_cell_means_member(self):
        app_settings.update_settings({"access_key": "LT-blank"})
        self.assertEqual(ac.status()["role"], "成员")

    def test_set_role_and_issue_with_role(self):
        ac.set_role("recM4", "管理员")
        self.assertEqual(self.rows[3]["fields"]["角色"], "管理员")
        with self.assertRaises(lark.LarkError):
            ac.set_role("recM4", "超级用户")
        out = ac.issue_key("管理小王", role="管理员")
        self.assertEqual(next(r for r in self.rows if r["record_id"] == out["record_id"])
                         ["fields"]["角色"], "管理员")

    def test_upgrade_is_idempotent_on_role_aware_table(self):
        app_settings.update_settings({"access_key": "LT-mem"})
        out = ac.upgrade_table()
        self.assertFalse(out["field_added"])
        self.assertTrue(out["self_admin"])
        self.assertEqual(self.rows[3]["fields"]["角色"], "管理员")


class TestLegacyTable(Base):
    """授权表 created before roles existed: no 角色 column."""
    role_field = False

    def setUp(self):
        super().setUp()
        app_settings.update_settings({"auth_table": TBL, "access_key": "LT-mem"})

    def test_everyone_is_admin_until_upgraded(self):
        st = ac.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["role"], "管理员")
        self.assertFalse(st["role_field"])
        self.assertIn("admin", st["perms"])

    def test_set_role_refused_before_upgrade(self):
        with self.assertRaises(lark.LarkError):
            ac.set_role("recM4", "成员")

    def test_legacy_table_only_stamps_existing_columns(self):
        self.assertTrue(ac.status()["ok"])
        f = self.rows[3]["fields"]
        self.assertTrue(f.get("最近使用"))
        self.assertNotIn("最近验证", f)          # column absent -> not written
        self.assertNotIn("设备", f)

    def test_upgrade_adds_field_and_makes_caller_admin(self):
        out = ac.upgrade_table()
        self.assertTrue(out["field_added"])
        self.assertEqual(set(out["added"]), {"角色", "最近验证", "设备"})
        self.assertTrue(out["self_admin"])
        self.assertTrue(self.api.role_field)
        self.assertEqual(self.rows[3]["fields"]["角色"], "管理员")
        # other members without a role are now restricted
        app_settings.update_settings({"access_key": "LT-blank"})
        ac.invalidate()
        self.assertEqual(ac.status(force=True)["role"], "成员")
        # issue_key now writes the role
        out2 = ac.issue_key("新成员")
        self.assertEqual(self.rows[-1]["fields"]["角色"], "成员")
        self.assertEqual(out2["role"], "成员")


if __name__ == "__main__":
    unittest.main()
