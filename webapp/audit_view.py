# -*- coding: utf-8 -*-
"""
audit_view.py — read-time enrichment for the 🗑️ 删除日志 tab.

The audit store keeps events VERBATIM (field ids, raw operator ids). This
module prettifies at read time so improvements apply to already-logged events:

  * table_id  -> config label（3.1 / 5.2 … + dev 副本标注）
  * field_id  -> current field name via lark_client.field_meta (best effort —
                 fields renamed/deleted since the event fall back to the id)
  * field_value JSON strings -> readable text
  * operator open_id -> display name via contact API (optional scope; cached;
                 falls back to the raw open_id when unavailable)

READ ONLY — never writes to Lark or the store.
"""

import os
import json
import time
import threading

import lark_client as lark

_name_cache = {}
_name_fail_at = {}          # open_id -> last failed lookup ts (retry after TTL)
_FAIL_RETRY = 600.0
_name_lock = threading.Lock()

# Operator alias map — {open_id: display name}, machine-written by
# identity_harvest.py (created_by/modified_by sweeps of every table; refreshed
# via the 🗑️ tab's 🔄 button). Needed for operators OUTSIDE the app's
# 通讯录授权范围 (contact API error 41050). Hot-reloads on file change.
# (A hand-maintained operators.json layer existed briefly; removed 2026-09-03
#  at the operator's request — the harvest resolution suffices.)
OPERATORS_AUTO_PATH = os.path.join(lark.ROOT, "config", "operators-auto.json")
_ops_cache = {}          # path -> {"mtime": float, "map": dict}


def _read_alias_file(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _ops_cache.get(path)
    if hit and hit["mtime"] == mtime:
        return hit["map"]
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        m = {str(k): str(v) for k, v in data.items()
             if not str(k).startswith("_")}
        _ops_cache[path] = {"mtime": mtime, "map": m}
        return m
    except (ValueError, OSError):
        return (hit or {}).get("map", {})


def _local_operators():
    return _read_alias_file(OPERATORS_AUTO_PATH)


def _table_labels():
    # live table names first (every table in the Base, zero maintenance)…
    try:
        labels = dict(lark.list_tables())
    except Exception:                                             # noqa
        labels = {}
    # …overridden by the config registry's operator-friendly labels + dev tags
    cfg = lark.config_values()
    labels.update({tid: lbl for lbl, tid in cfg["tables"].items()})
    for lbl, tid in cfg["dev_tables"].items():
        labels[tid] = f"{lbl} (dev)"
    return labels


def _field_maps(table_id):
    """(field_id -> name, option_id -> option name) for a table; ({}, {}) on error."""
    try:
        by_id = lark.field_meta(table_id)["by_id"]
    except Exception:                                             # noqa
        return {}, {}
    names, options = {}, {}
    for fid, m in by_id.items():
        names[fid] = m["field_name"]
        options.update(m.get("options") or {})
    return names, options


def resolve_operator(open_id):
    """open_id -> display name.
    Order: config/operators.json (manual aliases, hot-reloaded) -> success
    cache -> contact API (failures retried after 10 min, so a widened
    通讯录授权范围 takes effect without a restart) -> the raw id."""
    if not open_id:
        return ""
    local = _local_operators().get(open_id)
    if local:
        return local
    with _name_lock:
        if open_id in _name_cache:
            return _name_cache[open_id]
        if time.time() - _name_fail_at.get(open_id, 0) < _FAIL_RETRY:
            return open_id
    try:
        data = lark._api("GET", f"/open-apis/contact/v3/users/{open_id}",
                         query={"user_id_type": "open_id"})
        u = data.get("user") or {}
        name = u.get("name") or u.get("en_name") or open_id
    except Exception:                                             # noqa
        # visibility (41050) / scope errors — don't poison the cache forever
        with _name_lock:
            _name_fail_at[open_id] = time.time()
        return open_id
    with _name_lock:
        _name_cache[open_id] = name
    return name


def _pretty_value(raw, options=None):
    """field_value string -> readable text. Decodes the JSON Bitable embeds,
    unwraps {data, bus_type} carriers, and maps option ids -> option names."""
    options = options or {}
    if raw is None:
        return ""
    s = str(raw)
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        v = s

    def flat(x):
        if x is None:
            return ""
        if isinstance(x, list):
            return "、".join(p for p in (flat(i) for i in x) if p)
        if isinstance(x, dict):
            if "data" in x:                      # {data:[...], bus_type:N} carrier
                return flat(x["data"])
            for k in ("text", "name", "full_name", "file_name", "link"):
                if x.get(k):
                    return str(x[k])
            if "value" in x:
                return flat(x["value"])
            if "record_ids" in x:
                return f"关联×{len(x['record_ids'])}"
            if "number" in x:                    # auto-number {number, sequence}
                return str(x["number"])
            return json.dumps(x, ensure_ascii=False)
        if isinstance(x, float) and x.is_integer():
            return str(int(x))
        xs = str(x)
        return options.get(xs, xs)               # option id -> option name
    return flat(v)


def _fields_of(action_payload, key, names, options):
    out = []
    for f in (action_payload or {}).get(key) or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("field_id") or ""
        out.append({
            "field_id": fid,
            "name": names.get(fid, fid),
            "value": _pretty_value(f.get("field_value"), options),
        })
    return out


# ---------------------------------------------------------------------------
# Ad-hoc token resolver — paste any id from a log detail, get readable text.
# ---------------------------------------------------------------------------

# Cross-table option/field index. Building it means field_meta on EVERY table
# (~30-60 API calls, 1-2 min cold) — so it gets its own LONG cache (6 h; the
# ids it maps are near-immutable) and a background pre-warm at server start,
# instead of lark_client's 5-min metadata TTL.
_GIDX = {"ts": 0.0, "value": ({}, {})}
_GIDX_TTL = 6 * 3600
_gidx_lock = threading.Lock()


def _build_indexes():
    opts, flds = {}, {}
    try:
        tables = lark.list_tables()
    except Exception:                                             # noqa
        tables = {}
    for tid, tname in tables.items():
        try:
            by_id = lark.field_meta(tid)["by_id"]
        except Exception:                                         # noqa
            continue
        for fid, m in by_id.items():
            flds.setdefault(fid, (m["field_name"], tname))
            for oid, oname in (m.get("options") or {}).items():
                opts.setdefault(oid, (oname, f"{tname}·{m['field_name']}"))
    return opts, flds


def _global_indexes():
    with _gidx_lock:
        if time.time() - _GIDX["ts"] < _GIDX_TTL:
            return _GIDX["value"]
    value = _build_indexes()                # network — outside the lock
    with _gidx_lock:
        # keep a non-empty older index over a failed (empty) rebuild
        if value[1] or not _GIDX["value"][1]:
            _GIDX.update(ts=time.time(), value=value)
        return _GIDX["value"]


def warm_indexes_async():
    """Fire-and-forget pre-warm so the first 解析 click is instant."""
    threading.Thread(target=lambda: _global_indexes(), daemon=True).start()


def resolve_token(token):
    """One pasted code -> readable text. Supports opt… (select option),
    fld… (field), tbl… (table), ou_… (operator). Returns a result dict."""
    t = (token or "").strip().strip('"').strip("'")
    if not t:
        return {"token": t, "kind": "empty", "name": None, "context": None}
    if t.startswith("ou_"):
        name = resolve_operator(t)
        return {"token": t, "kind": "操作人", "name": name if name != t else None,
                "context": None if name != t else
                "未识别 — 试试 🔄 采集，或该用户从未在任何表留下创建/修改痕迹"}
    if t.startswith("tbl"):
        try:
            name = lark.list_tables().get(t)
        except Exception:                                         # noqa
            name = None
        return {"token": t, "kind": "数据表", "name": name,
                "context": None if name else "不在当前 Base 的表清单里"}
    if t.startswith("opt") or t.startswith("fld"):
        opts, flds = _global_indexes()
        if t.startswith("opt"):
            hit = opts.get(t)
            return {"token": t, "kind": "选项值", "name": hit[0] if hit else None,
                    "context": hit[1] if hit else
                    "未找到 — 可能属于其它 Base 的表（公式跨表带出的选项无法解析）"}
        hit = flds.get(t)
        return {"token": t, "kind": "字段", "name": hit[0] if hit else None,
                "context": hit[1] if hit else "未找到"}
    return {"token": t, "kind": "未知类型", "name": None,
            "context": "支持 opt… / fld… / tbl… / ou_… 前缀的 id"}


def enrich(rows):
    """Add display fields to audit_store.search() rows (in place, returned)."""
    labels = _table_labels()
    maps_by_table = {}
    for r in rows:
        tid = r.get("table_id") or ""
        if tid not in maps_by_table:
            maps_by_table[tid] = _field_maps(tid)
        names, options = maps_by_table[tid]
        action_payload = (r.get("raw") or {}).get("action") or {}
        r["table_label"] = labels.get(tid, tid)
        r["operator_name"] = resolve_operator(r.get("operator_open_id"))
        r["before"] = _fields_of(action_payload, "before_value", names, options)
        r["after"] = _fields_of(action_payload, "after_value", names, options)
        r.pop("raw", None)          # keep the API response lean; raw stays in DB
    return rows
