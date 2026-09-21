# -*- coding: utf-8 -*-
"""Unit tests for app_settings (settings.json + DPAPI credential store) and
apppaths. Offline. Uses LARK_HOME to point the data dir at a temp folder.

Run:  python -m unittest discover webapp/tests -v
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apppaths          # noqa: E402
import app_settings      # noqa: E402


class TempHome(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lt-settings-")
        self._env = dict(os.environ)
        os.environ["LARK_HOME"] = self.home
        os.environ.pop("LARK_APP_ID", None)
        os.environ.pop("LARK_APP_SECRET", None)
        # hide the repo's legacy secrets.txt so tests are hermetic
        self._legacy = mock.patch.object(app_settings, "LEGACY_SECRETS",
                                         lambda: os.path.join(self.home, "nope.txt"))
        self._legacy.start()

    def tearDown(self):
        self._legacy.stop()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.home, ignore_errors=True)


class TestPaths(TempHome):
    def test_data_dir_honours_lark_home(self):
        self.assertEqual(apppaths.data_dir(), self.home)
        p = apppaths.data_path("a", "b.json")
        self.assertTrue(os.path.isdir(os.path.dirname(p)))

    def test_state_path_is_repo_relative_when_not_frozen(self):
        self.assertFalse(apppaths.is_frozen())
        self.assertTrue(apppaths.state_path("logs", "x.log")
                        .startswith(apppaths.repo_root()))


class TestSettings(TempHome):
    def test_defaults_then_patch_only_known_keys(self):
        st = app_settings.get_settings()
        self.assertEqual(st["env"], "prod")
        self.assertEqual(st["port"], 8787)
        out = app_settings.update_settings({"operator_name": "张三", "bogus": 1,
                                            "updated": 5})
        self.assertEqual(out["operator_name"], "张三")
        self.assertNotIn("bogus", out)
        self.assertNotEqual(out["updated"], 5)          # server-stamped
        with open(app_settings.SETTINGS_PATH(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["operator_name"], "张三")

    def test_corrupt_settings_file_falls_back_to_defaults(self):
        with open(app_settings.SETTINGS_PATH(), "w") as f:
            f.write("{not json")
        self.assertEqual(app_settings.get_settings()["port"], 8787)


class TestCredentials(TempHome):
    def test_protect_roundtrip(self):
        blob = app_settings.protect(b"hello \xe4\xb8\xad")
        self.assertNotIn(b"hello", blob)
        self.assertEqual(app_settings.unprotect(blob), b"hello \xe4\xb8\xad")

    def test_none_configured(self):
        self.assertEqual(app_settings.resolve_credentials(), (None, None, None))
        with self.assertRaises(app_settings.NoCredentials):
            app_settings.get_credentials()
        st = app_settings.credential_status()
        self.assertFalse(st["configured"])
        self.assertIsNone(st["secret_hint"])

    def test_save_resolve_clear(self):
        app_settings.save_credentials("cli_abc123456", "S" * 32)
        a, s, src = app_settings.resolve_credentials()
        self.assertEqual((a, src), ("cli_abc123456", "dpapi"))
        self.assertEqual(s, "S" * 32)
        with open(app_settings.SECRETS_PATH(), "rb") as f:
            raw = f.read()
        self.assertNotIn(b"S" * 32, raw)              # encrypted at rest
        st = app_settings.credential_status()
        self.assertTrue(st["configured"])
        self.assertNotEqual(st["secret_hint"], "S" * 32)
        self.assertTrue(st["secret_hint"].startswith("SSS"))
        app_settings.clear_credentials()
        self.assertEqual(app_settings.resolve_credentials()[2], None)

    def test_validation(self):
        with self.assertRaises(ValueError):
            app_settings.save_credentials("notcli", "S" * 32)
        with self.assertRaises(ValueError):
            app_settings.save_credentials("cli_abc123456", "short")

    def test_env_fallback_order(self):
        os.environ["LARK_APP_ID"] = "cli_envenvenv"
        os.environ["LARK_APP_SECRET"] = "E" * 32
        self.assertEqual(app_settings.resolve_credentials()[2], "env")
        app_settings.save_credentials("cli_dpapidpapi", "D" * 32)
        self.assertEqual(app_settings.resolve_credentials()[0], "cli_dpapidpapi")

    def test_legacy_secrets_txt(self):
        self._legacy.stop()
        legacy = os.path.join(self.home, "secrets.txt")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("AppId: cli_legacy9999\nAppSecret: LEGACYSECRETLEGACYSECRET12\n")
        self._legacy = mock.patch.object(app_settings, "LEGACY_SECRETS", lambda: legacy)
        self._legacy.start()
        a, s, src = app_settings.resolve_credentials()
        self.assertEqual((a, s, src), ("cli_legacy9999", "LEGACYSECRETLEGACYSECRET12", "legacy"))

    def test_save_invalidates_token_cache(self):
        import lark_client as lark
        lark._token.update(host="h", value="old", exp=9e12)
        app_settings.save_credentials("cli_abc123456", "S" * 32)
        self.assertIsNone(lark._token["value"])

    def test_undecryptable_blob_is_ignored(self):
        with open(app_settings.SECRETS_PATH(), "wb") as f:
            f.write(b"DPAPI1" + b"\x00garbage")
        self.assertEqual(app_settings.resolve_credentials()[2], None)


if __name__ == "__main__":
    unittest.main()
