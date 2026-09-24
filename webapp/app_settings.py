# -*- coding: utf-8 -*-
r"""
app_settings.py — per-user settings + Windows-DPAPI-encrypted credentials.
================================================================================

Replaces the plaintext config/secrets.txt for anyone who uses the ⚙ 设置 page,
while keeping the old file working on the owner's dev machine.

FILES (all under apppaths.data_dir(), i.e. %APPDATA%\LarkTunnel)
    settings.json   non-secret preferences: env, port, operator name,
                    access key, 授权表 id, window size … — plain JSON
    secrets.bin     {"app_id","app_secret"} encrypted with Windows DPAPI
                    (CryptProtectData, current-user scope + app entropy).
                    Bound to THIS Windows account on THIS machine: copying the
                    file elsewhere yields undecryptable bytes, so a departed
                    teammate's laptop image does not leak the App Secret.

CREDENTIAL RESOLUTION ORDER (resolve_credentials)
    1. secrets.bin (settings page, admin)   → source "dpapi"
    2. env LARK_APP_ID / LARK_APP_SECRET    → source "env"
    3. config/bundled.bin (baked into the distributed exe by build.bat via
       bundle_secrets.py; members never type credentials — they only enter
       their 授权码)                         → source "bundled"
    4. legacy config/secrets.txt (repo)     → source "legacy"
    none → NoCredentials — the UI then opens ⚙ 设置 automatically.

    bundled.bin is OBFUSCATED (SHA-256 keystream XOR keyed on the Base token),
    not truly secret: anyone who can read the exe folder can recover it with
    effort. It keeps the secret out of casual view and out of teammates'
    hands as a typed value; the real kill switch remains rotating the App
    Secret in the Feishu console and shipping a new build.

REVOCATION MODEL (documented in the settings page too)
    Access for teammates is controlled two ways, both owner-driven:
      • 授权表 (access_control.py): flip a member to 停用 → their tool locks
        within one check interval, even with valid app credentials.
      • rotate the App Secret in the Feishu developer console → every stored
        copy stops working; only people you hand the new secret to continue.
    Saving new credentials here invalidates the cached tenant token at once.
"""
import os
import io
import re
import json
import time
import base64
import ctypes
import threading

import apppaths

_LOCK = threading.RLock()


def SETTINGS_PATH():
    return apppaths.data_path("settings.json")


def SECRETS_PATH():
    return apppaths.data_path("secrets.bin")


def LEGACY_SECRETS():
    return os.path.join(apppaths.repo_root(), "config", "secrets.txt")


def BUNDLED_SECRETS():
    return os.path.join(apppaths.bundle_root(), "config", "bundled.bin")


DEFAULTS = {
    "env": "prod",              # 'prod' | 'dev' (dev only if devTables exist)
    "port": 8787,
    "operator_name": "",        # shown in the topbar, stamped into logs/ops.jsonl
    "access_key": "",           # this person's 授权码 (team mode)
    "auth_table": "",           # tbl id of the 授权表 (empty = single-user mode)
    "window": {"width": 1440, "height": 900},
    "updated": 0,
}

# ---------------------------------------------------------------------------
# DPAPI (ctypes, stdlib only — no pywin32 dependency)
# ---------------------------------------------------------------------------
_ENTROPY = b"LarkTunnel|secrets|v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(func_name, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inp, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _BLOB()
    fn = getattr(crypt32, func_name)
    ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(f"{func_name} failed (winerror {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    if os.name == "nt":
        return b"DPAPI1" + _dpapi("CryptProtectData", data)
    return b"PLAIN1" + base64.b64encode(data)      # non-Windows dev fallback


def unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"DPAPI1"):
        return _dpapi("CryptUnprotectData", blob[6:])
    if blob.startswith(b"PLAIN1"):
        return base64.b64decode(blob[6:])
    raise ValueError("unknown secrets.bin format")


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    with _LOCK:
        out = json.loads(json.dumps(DEFAULTS))
        try:
            with io.open(SETTINGS_PATH(), encoding="utf-8") as f:
                out.update(json.load(f) or {})
        except (OSError, ValueError):
            pass                        # missing/corrupt file -> defaults
        return out


def update_settings(patch: dict) -> dict:
    """Merge `patch` into settings.json (only known keys) and return the result."""
    with _LOCK:
        cur = get_settings()
        for k, v in (patch or {}).items():
            if k in DEFAULTS and k != "updated":
                cur[k] = v
        cur["updated"] = int(time.time())
        tmp = SETTINGS_PATH() + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH())
        return cur


# ---------------------------------------------------------------------------
# bundled credentials (build-time blob) — obfuscation, see module docstring
# ---------------------------------------------------------------------------
_BUNDLE_MAGIC = b"LTB1"


def _keystream(n, salt: bytes):
    import hashlib
    out, counter, seed = b"", 0, b"LarkTunnel|bundle|" + salt
    while len(out) < n:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


def _bundle_salt():
    try:
        import lark_client
        return lark_client.config_values()["base_token"].encode("utf-8")
    except Exception:
        return b""


def write_bundle(path, app_id, app_secret):
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    ks = _keystream(len(data), _bundle_salt())
    blob = _BUNDLE_MAGIC + bytes(a ^ b for a, b in zip(data, ks))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)


def read_bundle(path=None):
    path = path or BUNDLED_SECRETS()
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return None
    if not blob.startswith(_BUNDLE_MAGIC):
        return None
    body = blob[len(_BUNDLE_MAGIC):]
    try:
        data = bytes(a ^ b for a, b in zip(body, _keystream(len(body), _bundle_salt())))
        d = json.loads(data.decode("utf-8"))
        if d.get("app_id") and d.get("app_secret"):
            return d["app_id"], d["app_secret"]
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
class NoCredentials(Exception):
    pass


def _parse_legacy(raw: str):
    """config/secrets.txt — tolerant 'AppId: cli_…' / 'AppSecret: …' parsing
    (kept byte-compatible with the previous lark_client._load_creds)."""
    app_id = None
    m = re.search(r"\bcli_[A-Za-z0-9]+", raw)
    if m:
        app_id = m.group(0)
    kv = {}
    for line in raw.splitlines():
        mm = re.match(r"\s*[\"']?([A-Za-z0-9_\- ]+?)[\"']?\s*[:=]\s*[\"']?([^\"',#]+)", line)
        if mm:
            kv[mm.group(1).strip().lower().replace(" ", "_")] = mm.group(2).strip()
    if not app_id:
        for k in ("app_id", "appid", "app_key", "id"):
            if k in kv:
                app_id = kv[k]
                break
    secret = None
    for k in ("app_secret", "appsecret", "secret", "app_secret_key"):
        if k in kv:
            secret = kv[k]
            break
    if not secret:
        cands = [c for c in re.findall(r"\b[A-Za-z0-9]{20,}\b", raw)
                 if c != app_id and not c.startswith("cli_")]
        if cands:
            secret = cands[0]
    return app_id, secret


def _read_dpapi():
    try:
        with open(SECRETS_PATH(), "rb") as f:
            blob = f.read()
    except OSError:
        return None
    try:
        d = json.loads(unprotect(blob).decode("utf-8"))
        if d.get("app_id") and d.get("app_secret"):
            return d["app_id"], d["app_secret"]
    except Exception:
        return None            # another account's file — undecryptable here
    return None


def resolve_credentials():
    """-> (app_id, app_secret, source) or (None, None, None)."""
    with _LOCK:
        got = _read_dpapi()
        if got:
            return got[0], got[1], "dpapi"
        a, s = os.environ.get("LARK_APP_ID"), os.environ.get("LARK_APP_SECRET")
        if a and s:
            return a, s, "env"
        got = read_bundle()
        if got:
            return got[0], got[1], "bundled"
        try:
            with io.open(LEGACY_SECRETS(), encoding="utf-8-sig") as f:
                a, s = _parse_legacy(f.read())
            if a and s:
                return a, s, "legacy"
        except OSError:
            pass
        return None, None, None


def get_credentials():
    a, s, _src = resolve_credentials()
    if not a:
        raise NoCredentials("未配置飞书应用凭据 — 请在 ⚙ 设置 中填写 App ID / App Secret")
    return a, s


def save_credentials(app_id: str, app_secret: str):
    app_id, app_secret = (app_id or "").strip(), (app_secret or "").strip()
    if not app_id.startswith("cli_") or len(app_id) < 8:
        raise ValueError("App ID 格式不对（应以 cli_ 开头）")
    if len(app_secret) < 16:
        raise ValueError("App Secret 太短，请核对后重试")
    blob = protect(json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"))
    with _LOCK:
        tmp = SECRETS_PATH() + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, SECRETS_PATH())
    _invalidate_token()


def clear_credentials():
    with _LOCK:
        try:
            os.remove(SECRETS_PATH())
        except OSError:
            pass
    _invalidate_token()


def _invalidate_token():
    try:
        import lark_client
        lark_client.invalidate_token()
    except Exception:
        pass


def credential_status():
    """What the settings page shows — never the secret itself."""
    a, s, src = resolve_credentials()
    return {
        "configured": bool(a),
        "source": src,                        # dpapi | env | bundled | legacy | None
        "app_id": a,
        "secret_hint": (s[:3] + "…" + s[-2:]) if s else None,
        "store_path": SECRETS_PATH(),
        "data_dir": apppaths.data_dir(),
        "bundled": read_bundle() is not None,  # program carries credentials
    }


def test_connection(app_id=None, app_secret=None):
    """Mint a tenant token with the GIVEN (unsaved) or the stored credentials,
    then list the configured Base's tables — proves auth + Base access.
    Returns a dict for the UI; never raises."""
    import lark_client as lark
    if not app_id or not app_secret:
        try:
            app_id, app_secret = get_credentials()
        except NoCredentials as e:
            return {"ok": False, "error": str(e)}
    t0 = time.time()
    last = None
    for host in lark.HOSTS:
        try:
            r = lark._http("POST", f"{host}/open-apis/auth/v3/tenant_access_token/internal",
                           payload={"app_id": app_id, "app_secret": app_secret})
        except lark.LarkError as e:
            last = str(e)
            continue
        if r.get("code") != 0:
            last = f"code {r.get('code')}: {r.get('msg')}"
            continue
        tok = r["tenant_access_token"]
        base = lark.config_values()["base_token"]
        try:
            d = lark._http("GET", f"{host}/open-apis/bitable/v1/apps/{base}/tables?page_size=100",
                           headers={"Authorization": f"Bearer {tok}"})
        except lark.LarkError as e:
            return {"ok": False, "error": f"凭据有效，但读取 Base 失败：{e}", "host": host}
        if d.get("code") != 0:
            return {"ok": False, "host": host,
                    "error": f"凭据有效，但无权访问该 Base（code {d.get('code')}: {d.get('msg')}）"
                             "— 请确认应用已被添加为该多维表格的协作者"}
        items = (d.get("data") or {}).get("items") or []
        return {"ok": True, "host": host, "tables": len(items),
                "ms": int((time.time() - t0) * 1000), "app_id": app_id}
    return {"ok": False, "error": f"鉴权失败：{last}"}
