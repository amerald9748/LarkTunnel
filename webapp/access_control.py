# -*- coding: utf-8 -*-
"""
access_control.py — owner-controlled access for teammates (授权表).
================================================================================

WHY
    Teammates all use the same Feishu App credentials, so the App alone cannot
    tell people apart or switch one person off. This module adds a per-person
    授权码 checked against a small Bitable table the OWNER controls. Flipping
    a member's 状态 to 停用 locks that person's tool within one check interval
    — no redeploy, no secret rotation needed for the everyday case.
    (Rotating the App Secret remains the hard stop for someone who copied the
    secret itself; the settings page spells out that checklist.)

MODES
    single  — no 授权表 configured (settings.auth_table empty). The owner's own
              machine: nothing is checked, tool behaves as before.
    team    — 授权表 configured. On start and every CHECK_TTL seconds the tool
              looks up settings.access_key:
                 exists · 状态 == 启用 · not past 到期 → OK (name shown in topbar)
                 otherwise                              → LOCKED
              Feishu unreachable → last verified result stays valid for
              GRACE seconds (a network blip must not lock a legitimate user);
              never verified → LOCKED.

授权表 SCHEMA (created by create_auth_table(), fields matched BY NAME)
    授权码 (Text, primary) · 姓名 (Text) · 状态 (SingleSelect 启用/停用)
    · 角色 (SingleSelect 管理员/成员) · 到期 (Text 'YYYY-MM-DD', optional)
    · 备注 (Text) · 最近使用 (Text)

ROLES (one build for everyone — the table decides what each person sees)
    管理员  every tab + the admin half of ⚙ 设置
    成员    📦 库存导入 · ① 新建预约 · ② 计划同步 · ③ 核对 (+ own credentials/key)
    A table WITHOUT a 角色 field (created before 2026-09-21) treats everyone
    as 管理员 until the owner runs upgrade_table() from ⚙; an empty 角色 cell
    on a role-aware table means 成员 (restricted by default).
    The server enforces the same map (server._FEATURE_OF); hiding tabs is
    only the courtesy layer.

WHICH TABLE: config.js `authTable` is the distributed value (baked into the
exe). A local settings.json override is honoured only on a source checkout,
so a teammate cannot leave team mode by blanking their settings.

WRITES: the only write in normal use is the best-effort 最近使用 heartbeat
(so the owner can see who is actually active). Owner tools (create table,
issue key, set status) are explicit button actions in ⚙ 设置.
"""
import time
import secrets
import threading
import datetime

import lark_client as lark
import app_settings
import apppaths

CHECK_TTL = 300.0        # re-verify every 5 minutes
GRACE = 24 * 3600.0      # offline grace for an already-verified key
FIELDS = {"key": "授权码", "name": "姓名", "status": "状态", "expiry": "到期",
          "note": "备注", "seen": "最近使用", "role": "角色",
          "verify": "最近验证", "device": "设备"}
# columns upgrade_table() adds when missing (name -> Bitable field spec)
_UPGRADE_FIELDS = {
    "role": {"type": 3, "property": {"options": [{"name": "管理员"}, {"name": "成员"}]}},
    "verify": {"type": 1},
    "device": {"type": 1},
    "seen": {"type": 1},
}
ACTIVE, INACTIVE = "启用", "停用"
ADMIN, MEMBER = "管理员", "成员"
FEATURES = ("import", "create", "sync", "verify", "audit", "query", "parse", "admin")
ROLE_PERMS = {ADMIN: set(FEATURES),
              MEMBER: {"import", "create", "sync", "verify"}}

_state = {"checked": 0.0, "verified_at": 0.0, "ok": None, "name": "",
          "reason": "", "record_id": None, "role": None, "role_field": None}
_lock = threading.Lock()


def _base():
    return lark.config_values()["base_token"]


def _table():
    """Effective 授权表 id. Frozen exe: config.js only (baked in). Source
    checkout: settings.json override, else config.js."""
    cfg = (lark.config_values().get("auth_table") or "").strip()
    if apppaths.is_frozen():
        return cfg
    return (app_settings.get_settings().get("auth_table") or "").strip() or cfg


def table_source():
    cfg = (lark.config_values().get("auth_table") or "").strip()
    local = (app_settings.get_settings().get("auth_table") or "").strip()
    if apppaths.is_frozen():
        return "config" if cfg else None
    return "settings" if local else ("config" if cfg else None)


def perms_for(role):
    return sorted(ROLE_PERMS.get(role, ROLE_PERMS[MEMBER]))


def _fields_present(table):
    """Set of column names the 授权表 currently has (5-min cached meta);
    None when the meta cannot be read."""
    try:
        return set(lark.field_meta(table)["by_name"])
    except Exception:
        return None


def _role_field_present(table):
    """Does the 授权表 have a 角色 column yet? Unknown -> assume present
    (restrictive)."""
    present = _fields_present(table)
    return True if present is None else FIELDS["role"] in present


def device_label():
    """What gets stamped into 设备: computer / Windows user / app version."""
    import socket
    import getpass
    try:
        import server
        ver = server.VERSION
    except Exception:
        ver = "?"
    try:
        host = socket.gethostname()
    except Exception:
        host = "?"
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"
    return f"{host}\\{user} · v{ver}" + (" exe" if apppaths.is_frozen() else " src")


def mode():
    return "team" if _table() else "single"


def status(force=False):
    """Cached access verdict for THIS user. Never raises."""
    st = app_settings.get_settings()
    table = _table()
    if not table:
        return {"mode": "single", "ok": True, "name": st.get("operator_name") or "",
                "reason": "单机模式（未配置授权表）", "role": ADMIN,
                "perms": perms_for(ADMIN), "role_field": None}
    key = (st.get("access_key") or "").strip()
    now = time.time()
    with _lock:
        fresh = (now - _state["checked"]) < CHECK_TTL and _state["ok"] is not None
        if fresh and not force:
            return _verdict(st)
    verdict = _verify(table, key)
    with _lock:
        _state["checked"] = now
        if verdict is None:                       # network trouble
            if _state["ok"] and now - _state["verified_at"] < GRACE:
                _state["reason"] = "飞书暂不可达 — 沿用最近一次验证结果"
            else:
                _state.update(ok=False, reason="无法连接飞书验证授权，且没有可沿用的验证记录")
        else:
            _state.update(verified_at=now if verdict["ok"] else _state["verified_at"],
                          ok=verdict["ok"], name=verdict.get("name", ""),
                          reason=verdict.get("reason", ""), record_id=verdict.get("record_id"),
                          role=verdict.get("role"), role_field=verdict.get("role_field"))
        return _verdict(st)


def _verdict(st):
    ok = bool(_state["ok"])
    role = _state["role"] if ok else None
    return {"mode": "team", "ok": ok,
            "name": _state["name"] or st.get("operator_name") or "",
            "reason": _state["reason"], "record_id": _state["record_id"],
            "checked": _state["checked"],
            "role": role, "perms": perms_for(role) if ok else [],
            "role_field": _state["role_field"]}


def _verify(table, key):
    """-> {'ok', 'name', 'reason', 'record_id', 'role', 'role_field'} or None
    when Feishu is unreachable."""
    if not key:
        return {"ok": False, "reason": "未填写授权码 — 请在 ⚙ 设置 中输入管理员发给你的授权码"}
    try:
        hits = lark._api("POST", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records/search",
                         payload={"filter": {"conjunction": "and", "conditions": [
                             {"field_name": FIELDS["key"], "operator": "is", "value": [key]}]},
                             "automatic_fields": False}, query={"page_size": 5}).get("items", [])
    except lark.LarkError as e:
        if e.code == "net" or (isinstance(e.code, int) and e.code >= 500):
            return None
        return {"ok": False, "reason": f"读取授权表失败：{e}"}
    except Exception as e:                        # e.g. NoCredentials
        return {"ok": False, "reason": f"无法验证授权：{e}"}
    if not hits:
        return {"ok": False, "reason": "授权码无效（授权表中不存在）"}
    f = hits[0].get("fields") or {}
    rid = hits[0]["record_id"]
    name = lark.flat_text(f.get(FIELDS["name"])) or ""
    if f.get(FIELDS["status"]) != ACTIVE:
        _stamp(table, rid, "拒绝：已停用")
        return {"ok": False, "name": name, "record_id": rid,
                "reason": f"该授权已停用（{name or key}）— 请联系管理员"}
    exp = lark.flat_text(f.get(FIELDS["expiry"]))
    if exp:
        try:
            if datetime.date.fromisoformat(exp[:10]) < datetime.date.today():
                _stamp(table, rid, f"拒绝：已于 {exp[:10]} 到期")
                return {"ok": False, "name": name, "record_id": rid,
                        "reason": f"授权已于 {exp[:10]} 到期 — 请联系管理员"}
        except ValueError:
            pass
    role_field = _role_field_present(table)
    if not role_field:
        role = ADMIN                     # legacy table: nobody is restricted yet
    else:
        raw = f.get(FIELDS["role"])
        role = raw if raw in (ADMIN, MEMBER) else MEMBER   # empty cell = 成员
    _stamp(table, rid, f"通过（{role}）")
    return {"ok": True, "name": name, "record_id": rid, "reason": "",
            "role": role, "role_field": role_field}


def _stamp(table, rid, verdict):
    """Log this verification on the member's row — 最近使用 (time), 最近验证
    (time + verdict), 设备 (computer/user/version) — writing only the columns
    the table has. Best-effort: never blocks or fails the check."""
    try:
        present = _fields_present(table) or {FIELDS["seen"]}
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        fields = {}
        if FIELDS["seen"] in present:
            fields[FIELDS["seen"]] = now
        if FIELDS["verify"] in present:
            fields[FIELDS["verify"]] = f"{now} {verdict}"
        if FIELDS["device"] in present:
            fields[FIELDS["device"]] = device_label()
        if fields:
            lark._api("PUT", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records/{rid}",
                      payload={"fields": fields})
    except Exception:
        pass


def _heartbeat(table, rid):          # kept for callers/tests of the old name
    _stamp(table, rid, "通过")


def invalidate():
    with _lock:
        _state["checked"] = 0.0


# ---------------------------------------------------------------------------
# Owner tools (explicit actions from ⚙ 设置)
# ---------------------------------------------------------------------------
def create_auth_table(name="LarkTunnel 授权表"):
    """Create the 授权表 in the configured Base and save its id to settings.
    An explicit owner action (confirmed in the UI) — it adds a table to the
    live Base."""
    fields = [
        {"field_name": FIELDS["key"], "type": 1},
        {"field_name": FIELDS["name"], "type": 1},
        {"field_name": FIELDS["status"], "type": 3,
         "property": {"options": [{"name": ACTIVE}, {"name": INACTIVE}]}},
        {"field_name": FIELDS["role"], "type": 3,
         "property": {"options": [{"name": ADMIN}, {"name": MEMBER}]}},
        {"field_name": FIELDS["expiry"], "type": 1},
        {"field_name": FIELDS["note"], "type": 1},
        {"field_name": FIELDS["seen"], "type": 1},
        {"field_name": FIELDS["verify"], "type": 1},
        {"field_name": FIELDS["device"], "type": 1},
    ]
    d = lark._api("POST", f"/open-apis/bitable/v1/apps/{_base()}/tables",
                  payload={"table": {"name": name, "default_view_name": "全部",
                                     "fields": fields}})
    tid = d.get("table_id")
    if not tid:
        raise lark.LarkError(f"创建授权表失败：{d}")
    app_settings.update_settings({"auth_table": tid})
    invalidate()
    return tid


def list_members():
    table = _table()
    if not table:
        return []
    items, pt = [], None
    while True:
        q = {"page_size": 200}
        if pt:
            q["page_token"] = pt
        d = lark._api("POST", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records/search",
                      payload={"automatic_fields": False}, query=q)
        items += d.get("items", [])
        if not d.get("has_more"):
            break
        pt = d.get("page_token")
    out = []
    for r in items:
        f = r.get("fields") or {}
        out.append({"record_id": r["record_id"],
                    "key": lark.flat_text(f.get(FIELDS["key"])),
                    "name": lark.flat_text(f.get(FIELDS["name"])),
                    "status": f.get(FIELDS["status"]) or "",
                    "role": f.get(FIELDS["role"]) or "",
                    "expiry": lark.flat_text(f.get(FIELDS["expiry"])),
                    "note": lark.flat_text(f.get(FIELDS["note"])),
                    "seen": lark.flat_text(f.get(FIELDS["seen"])),
                    "verify": lark.flat_text(f.get(FIELDS["verify"])),
                    "device": lark.flat_text(f.get(FIELDS["device"]))})
    return out


def issue_key(name, note="", expiry="", role=MEMBER):
    """Create an 启用 member with a fresh random 授权码; returns the key ONCE
    for the owner to hand over. Default role 成员 (restricted)."""
    table = _table()
    if not table:
        raise lark.LarkError("尚未配置授权表")
    if not (name or "").strip():
        raise lark.LarkError("请填写成员姓名")
    if role not in (ADMIN, MEMBER):
        raise lark.LarkError("角色只能是 管理员 或 成员")
    key = "LT-" + secrets.token_urlsafe(18)
    fields = {FIELDS["key"]: key, FIELDS["name"]: name.strip(), FIELDS["status"]: ACTIVE}
    if _role_field_present(table):
        fields[FIELDS["role"]] = role
    if note:
        fields[FIELDS["note"]] = note.strip()
    if expiry:
        fields[FIELDS["expiry"]] = expiry.strip()
    d = lark._api("POST", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records",
                  payload={"fields": fields})
    rid = (d.get("record") or {}).get("record_id")
    return {"key": key, "record_id": rid, "name": name.strip(), "role": role}


def set_status(record_id, active: bool):
    table = _table()
    if not table:
        raise lark.LarkError("尚未配置授权表")
    lark._api("PUT", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records/{record_id}",
              payload={"fields": {FIELDS["status"]: ACTIVE if active else INACTIVE}})
    invalidate()
    return True


def set_role(record_id, role):
    table = _table()
    if not table:
        raise lark.LarkError("尚未配置授权表")
    if role not in (ADMIN, MEMBER):
        raise lark.LarkError("角色只能是 管理员 或 成员")
    if not _role_field_present(table):
        raise lark.LarkError("授权表还没有「角色」字段 — 请先点「升级授权表」")
    lark._api("PUT", f"/open-apis/bitable/v1/apps/{_base()}/tables/{table}/records/{record_id}",
              payload={"fields": {FIELDS["role"]: role}})
    invalidate()
    return True


def upgrade_table():
    """Bring an older 授权表 up to the current schema — add 角色 / 最近验证 /
    设备 (whatever is missing) — and mark the CALLER's own row 管理员
    (otherwise the owner would lock themself down to 成员). Explicit owner
    action from ⚙; idempotent."""
    table = _table()
    if not table:
        raise lark.LarkError("尚未配置授权表")
    base = _base()
    present = _fields_present(table) or set()
    added_names = []
    for key, spec in _UPGRADE_FIELDS.items():
        name = FIELDS[key]
        if name in present:
            continue
        lark._api("POST", f"/open-apis/bitable/v1/apps/{base}/tables/{table}/fields",
                  payload={"field_name": name, **spec})
        added_names.append(name)
    if added_names:
        lark._meta_cache.pop(f"fieldmeta:{table}", None)
    added = bool(added_names)
    self_admin = False
    key = (app_settings.get_settings().get("access_key") or "").strip()
    if key:
        hits = lark._api("POST", f"/open-apis/bitable/v1/apps/{base}/tables/{table}/records/search",
                         payload={"filter": {"conjunction": "and", "conditions": [
                             {"field_name": FIELDS["key"], "operator": "is", "value": [key]}]},
                             "automatic_fields": False}, query={"page_size": 5}).get("items", [])
        if hits:
            lark._api("PUT", f"/open-apis/bitable/v1/apps/{base}/tables/{table}/records/{hits[0]['record_id']}",
                      payload={"fields": {FIELDS["role"]: ADMIN}})
            self_admin = True
    invalidate()
    return {"field_added": added, "added": added_names, "self_admin": self_admin}
