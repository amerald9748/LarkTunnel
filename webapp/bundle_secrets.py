# -*- coding: utf-8 -*-
r"""
bundle_secrets.py — write the App credentials blob that build.bat bakes into
the distributed exe (config/bundled.bin inside the bundle).

    python webapp\bundle_secrets.py            -> ..\.tmp\bundled.bin
    python webapp\bundle_secrets.py <out.bin>

Source of the credentials: whatever the OWNER's machine resolves right now
(⚙ 设置 DPAPI store → LARK_APP_ID/SECRET env → config/secrets.txt). The blob
is obfuscated (see app_settings.write_bundle), never committed (.tmp/ is
git-ignored) and deleted by build.bat after the build.

Members therefore never type App ID / Secret: their exe carries them, and
their 授权码 (checked against the 授权表 on every start) is the only login.
Rotating the App Secret = re-run build.bat and redistribute.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import apppaths      # noqa: E402
import app_settings  # noqa: E402


def main(argv):
    out = argv[1] if len(argv) > 1 else os.path.join(apppaths.repo_root(), ".tmp", "bundled.bin")
    app_id, secret, src = app_settings.resolve_credentials()
    if not app_id:
        print("[bundle] no credentials found (DPAPI store / env / config/secrets.txt)")
        return 1
    if src == "bundled":
        print("[bundle] refusing to re-bundle from an existing bundle; provide real credentials")
        return 1
    app_settings.write_bundle(out, app_id, secret)
    back = app_settings.read_bundle(out)
    if back != (app_id, secret):
        print("[bundle] self-check failed")
        return 1
    print(f"[bundle] wrote {out} ({app_id}, secret {secret[:3]}...{secret[-2:]}, from {src})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
