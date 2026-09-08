# -*- coding: utf-8 -*-
"""
identity_harvest.py — auto-build the operator alias map (open_id -> name).
================================================================================

WHY: audit events identify operators by open_id. The contact API can only
resolve users inside the app's 通讯录授权范围 (error 41050 otherwise), and the
Lark UI never displays open_ids at all — so a manual reverse lookup is
impossible. But Bitable records already embed {open_id, name} pairs in their
automatic fields（创建人/最后修改人）and person-type fields, readable with the
base:record:read scope we already have. Sweeping the Base's tables therefore
recovers the identity of everyone who ever created/edited a record — the very
population that shows up as audit operators.

SOURCES
  1. records/search with automatic_fields=true across every table in the Base
     (created_by / last_modified_by / 人员 fields) — READ-ONLY Lark calls;
  2. the local audit store itself (person-field field_identity_value blobs
     inside captured events) — fully offline.

OUTPUT: config/operators-auto.json （machine-written; the hand-maintained
config/operators.json always takes precedence at resolve time）.
"""

import os
import json
import time

import lark_client as lark
import audit_store

AUTO_PATH = os.path.join(lark.ROOT, "config", "operators-auto.json")

PER_TABLE_DEFAULT = 400          # newest N records per table per sweep
PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Identity extraction — shape-agnostic walker
# ---------------------------------------------------------------------------

def walk_identities(node, out=None, depth=0):
    """Collect {open_id: name} from any nested payload: matches every dict
    carrying an ou_… id alongside a name (covers created_by/modified_by,
    person fields, field_identity_value, group members …)."""
    if out is None:
        out = {}
    if depth > 14 or node is None:
        return out
    if isinstance(node, dict):
        oid = node.get("id") or node.get("open_id")
        if isinstance(oid, dict):                 # field_identity_value.id{...}
            oid = oid.get("open_id")
        name = node.get("name") or node.get("en_name")
        if (isinstance(oid, str) and oid.startswith("ou_")
                and isinstance(name, str) and name.strip()):
            out.setdefault(oid, name.strip())
        for v in node.values():
            walk_identities(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            walk_identities(v, out, depth + 1)
    elif isinstance(node, str) and len(node) < 20000 and node[:1] in "[{":
        try:
            walk_identities(json.loads(node), out, depth + 1)
        except (ValueError, TypeError):
            pass
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def harvest_table(table_id, per_table=PER_TABLE_DEFAULT):
    """{open_id: name} from one table's records (automatic fields included)."""
    base = lark.config_values()["base_token"]
    found, pt, fetched = {}, None, 0
    while fetched < per_table:
        q = {"page_size": min(PAGE_SIZE, per_table - fetched)}
        if pt:
            q["page_token"] = pt
        data = lark._api(
            "POST",
            f"/open-apis/bitable/v1/apps/{base}/tables/{table_id}/records/search",
            payload={"automatic_fields": True}, query=q)
        items = data.get("items", [])
        fetched += len(items)
        walk_identities(items, found)
        if not data.get("has_more") or not items:
            break
        pt = data.get("page_token")
    return found


def harvest_audit_store():
    """{open_id: name} mined offline from already-captured events
    (person-field field_identity_value blobs inside raw_json)."""
    found = {}
    rows = audit_store.search(action="all", limit=1000)
    for r in rows:
        walk_identities(r.get("raw"), found)
    return found


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_auto():
    try:
        with open(AUTO_PATH, encoding="utf-8-sig") as f:
            return {str(k): str(v) for k, v in json.load(f).items()
                    if not str(k).startswith("_")}
    except (OSError, ValueError):
        return {}


def _save_auto(mapping):
    payload = {"_generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "_readme": "Machine-written by identity_harvest.py — do not edit; "
                          "put manual overrides in operators.json instead."}
    payload.update(dict(sorted(mapping.items())))
    with open(AUTO_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def harvest(per_table=PER_TABLE_DEFAULT, progress=None):
    """Sweep every table in the Base + the local audit store; merge into
    operators-auto.json. Returns stats. READ-ONLY towards Lark."""
    tick = progress or (lambda **_: None)
    known = _load_auto()
    found = {}

    tick(stage="扫描本地审计库（离线）")
    found.update(harvest_audit_store())

    tables = lark.list_tables()                     # {table_id: name}
    ids = list(tables)
    for i, tid in enumerate(ids):
        tick(stage="扫描数据表 创建人/修改人", done=i, total=len(ids),
             current=tables.get(tid, tid))
        try:
            found.update(harvest_table(tid, per_table=per_table))
        except lark.LarkError as e:
            tick(current=f"{tables.get(tid, tid)}: {e}")

    merged = dict(known)
    new = {k: v for k, v in found.items() if known.get(k) != v}
    merged.update(found)
    _save_auto(merged)
    tick(stage="完成", done=len(ids), total=len(ids), current="")
    return {"tables_scanned": len(ids), "found": len(found),
            "new_or_changed": len(new), "total_known": len(merged),
            "sample_new": dict(list(new.items())[:20]),
            "auto_path": AUTO_PATH}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    stats = harvest(progress=lambda **kw: print(
        f"[harvest] {kw.get('stage', '')} {kw.get('current', '')}", flush=True))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
