# -*- coding: utf-8 -*-
"""
upload_56.py — plan & commit appointment records into the 5.6 预约表.

Every write is guarded:
  * 目的地  must be an existing 5.6 单选 option (UPS / AST / 私人地址 → blocked,
    never auto-create a junk option).
  * 预约账号 = the chosen 仓库供应商, mapped where needed (VAST → 元浩) and
    validated against the live 5.6 options.
  * ISA (预约号) must be numeric; written as a Number.
  * 复制时间列 stored as text 'YYYY/MM/DD HH:MM' to match existing rows.
  * Dedup by ISA — both against what already exists in 5.6 AND within the same
    batch (a grouped 预约号 covering several containers is ONE appointment).

Delivery-plan interlink (出库计划互联)
  * Every warehouse supplier has its own 5.x delivery-plan table (see
    PLAN_TABLES). For each NEW appointment created in 5.6, ONE new trip
    record is created in that supplier's 5.x table, linked via the 5.x
    two-way link field 预约信息 → the 5.6 back-link (5.2 出库计划 卡尔加里
    etc.) fills in automatically, and the 5.x ISA/预约时间 formula columns
    resolve from the link.
  * An appointment that already exists in 5.6 but has NO trip yet (and whose
    预约账号 matches the chosen supplier) gets the missing trip backfilled.

`plan()` performs only READS. `commit()` is the sole writer (create records).
"""

import re
import json
import uuid
import threading
import lark_client as lark

# 仓库供应商 → 5.6 预约账号 alias (from config warehouses[].accountAlias /
# warehouses[].account: VAST books as 元浩; CAL-5505 books as BESTAR).
ACCOUNT_ALIAS = {"VAST": "元浩", "CAL-5505": "BESTAR"}

# Serialize commit() across HTTP threads (server is ThreadingHTTPServer):
# dedup is check-then-write against /records/search, so two concurrent
# commits for the same ISA would both pass the check and double-create.
_write_lock = threading.Lock()

# 仓库供应商 → 对应的 5.x 出库计划表（每个供应商一张表）。CAL-5505 走 BESTAR。
PLAN_TABLES = {
    "BESTAR": "5.2 BESTAR-CAL",
    "CAL-5505": "5.2 BESTAR-CAL",
    "WBLL": "5.3 WBLL-EDM",
    "VAST": "5.4 VAST-VAN-01",
    "GFL": "5.5 GFL-VAN-02",
}

# 每张 5.x 表上指向 5.6 的双向关联字段（单值）。写这一个字段即可互联。
TRIP_LINK_FIELD = "预约信息"

# 5.6 上对应各 5.x 表的回链字段（对线上 schema 核实于 2026-07-29），
# 用于判断“已存在的预约是否已关联出库计划”。
LINK_ON_56 = {
    "5.2 BESTAR-CAL": "5.2 出库计划 卡尔加里",
    "5.3 WBLL-EDM": "5.3 出库计划 埃德蒙顿",
    "5.4 VAST-VAN-01": "5.4 出库计划 温哥华",
    "5.5 GFL-VAN-02": "5.5 出库计划-GFL-预约信息",
}


def fmt_time(appt):
    """Normalise a parsed appointment to '2026/07/24 09:00' (5.6 stored format)."""
    if not appt:
        return None
    s = str(appt)
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi = m.groups()
        return f"{y}/{int(mo):02d}/{int(d):02d} {int(h):02d}:{mi}"
    return None


def map_account(warehouse, valid_accounts):
    """(account, reason) — the 预约账号 to write, or a block reason."""
    w = (warehouse or "").strip()
    if not w:
        return None, "未选择仓库供应商（预约账号）"
    if w in valid_accounts:
        return w, None
    alias = ACCOUNT_ALIAS.get(w)
    if alias and alias in valid_accounts:
        return alias, None
    return None, f"「{w}」不是 5.6 合法预约账号"


def _t56():
    return lark.config_values()["tables"]["5.6"]


def _base():
    return lark.config_values()["base_token"]


def _existing_isas(isas, extra_fields=()):
    """Map isa -> list of existing 5.6 rows (dedup key). READ."""
    base, t56 = _base(), _t56()
    fields = ["ISA", "目的地", "预约账号", *extra_fields]
    out = {}
    for isa in isas:
        hits = lark._api(
            "POST", f"/open-apis/bitable/v1/apps/{base}/tables/{t56}/records/search",
            payload={"filter": {"conjunction": "and", "conditions": [
                {"field_name": "ISA", "operator": "is", "value": [str(isa)]}]},
                "field_names": fields, "automatic_fields": False},
            query={"page_size": 50},
        ).get("items", [])
        out[isa] = hits
    return out


def _link_ids(v):
    """record_ids inside a link-field value as returned by the read APIs."""
    if isinstance(v, dict):
        return list(v.get("link_record_ids") or v.get("record_ids") or [])
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.extend(x.get("link_record_ids") or x.get("record_ids") or [])
            elif isinstance(x, str):
                out.append(x)
        return out
    return []


def plan(records, warehouse):
    """READ-ONLY. Decide, for each record, what would happen in 5.6 AND in the
    supplier's 5.x delivery-plan table.
    Returns {'items': [...], 'summary': {...}, 'account': str|None,
             'plan_table': str|None}."""
    t56 = _t56()
    fm = lark.field_meta(t56)["by_name"]
    valid_dests = set((fm.get("目的地") or {}).get("options", {}).values())
    valid_accounts = set((fm.get("预约账号") or {}).get("options", {}).values())
    account, acc_reason = map_account(warehouse, valid_accounts)

    plan_table = PLAN_TABLES.get((warehouse or "").strip())
    link56 = LINK_ON_56.get(plan_table)

    isas = {str(r.get("isa")).strip() for r in records
            if str(r.get("isa") or "").strip().isdigit()}
    existing = _existing_isas(isas, extra_fields=(link56,) if link56 else ())

    planned = set()       # ISAs already scheduled to be created in THIS batch
    trip_planned = set()  # ISAs whose trip (new or backfill) is already planned
    items = []
    for r in records:
        awb = r.get("awb")
        route = r.get("route")
        isa = str(r.get("isa") or "").strip()
        t = fmt_time(r.get("appointment"))
        it = {"awb": awb, "route": route, "isa": isa or None,
              "account": account, "time": t, "action": None, "reason": None,
              "exists": False, "trip": {"do": "none"}}

        ex = existing.get(isa) or []
        if not isa.isdigit():
            it["action"], it["reason"] = "block", "无有效预约号(ISA)"
        elif ex:
            it["action"], it["reason"], it["exists"] = "skip", "当前派送记录已存在", True
            dests = sorted({lark.display_value("目的地", h["fields"].get("目的地"), None)
                            for h in ex if h.get("fields", {}).get("目的地")})
            if dests:
                it["existing_dest"] = ", ".join(dests)
            it["trip"] = _trip_for_existing(ex, isa, account, plan_table,
                                            link56, trip_planned)
        elif isa in planned:
            it["action"], it["reason"], it["exists"] = "skip", "当前派送记录已存在", True
            it["trip"] = {"do": "none", "note": "同批已处理"}
        elif route not in valid_dests:
            it["action"], it["reason"] = "block", f"路线「{route}」非 5.6 目的地选项"
        elif acc_reason:
            it["action"], it["reason"] = "block", acc_reason
        else:
            it["action"] = "create"
            it["fields"] = {"ISA": int(isa), "目的地": route,
                            "预约账号": account, "复制时间列": t}
            planned.add(isa)
            if plan_table:
                it["trip"] = {"do": "create", "table": plan_table}
                trip_planned.add(isa)
            else:
                it["trip"] = {"do": "none", "note": "该供应商无对应出库计划表"}
        items.append(it)

    summary = {"create": 0, "skip": 0, "block": 0, "exists": 0,
               "trip_create": 0, "trip_backfill": 0}
    for it in items:
        summary[it["action"]] += 1
        if it.get("exists"):
            summary["exists"] += 1
        do = (it.get("trip") or {}).get("do")
        if do == "create":
            summary["trip_create"] += 1
        elif do == "backfill":
            summary["trip_backfill"] += 1
    return {"items": items, "summary": summary, "account": account,
            "account_reason": acc_reason, "plan_table": plan_table}


def _trip_for_existing(ex_rows, isa, account, plan_table, link56, trip_planned):
    """Trip decision for an ISA that already exists in 5.6:
    linked / backfill (missing trip, same account) / none."""
    if not plan_table or not link56:
        return {"do": "none"}
    for h in ex_rows:
        if _link_ids((h.get("fields") or {}).get(link56)):
            return {"do": "linked", "table": plan_table}
    if isa in trip_planned:
        return {"do": "none", "note": "同批已补建"}
    same_acc = [h for h in ex_rows
                if (h.get("fields") or {}).get("预约账号") == account]
    if account and same_acc:
        trip_planned.add(isa)
        return {"do": "backfill", "table": plan_table,
                "append_to": same_acc[0]["record_id"]}
    return {"do": "none", "note": "预约账号不同，不补建出库计划"}


def _ctoken(*parts):
    """Deterministic idempotency token: an identical retried batch reuses the
    same client_token, so an ambiguous failure (client timeout after the
    server actually wrote) cannot double-create on re-commit."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "larktunnel|" + "|".join(parts)))


def commit(records, warehouse):
    """Re-plan (fresh existence check), CREATE the 'create' items in 5.6, then
    create the linked trip records in the supplier's 5.x delivery-plan table
    (new trips for new appointments + backfills for link-less existing ones).
    Returns the plan items, annotated with record_id / trip.record_id / errors.
    Serialized process-wide — see _write_lock."""
    with _write_lock:
        return _commit_locked(records, warehouse)


def _commit_locked(records, warehouse):
    base, t56 = _base(), _t56()
    result = plan(records, warehouse)
    creates = [it for it in result["items"] if it["action"] == "create"]

    if creates:
        records_payload = [{"fields": it["fields"]} for it in creates]
        ctoken = _ctoken("56", base, t56,
                         json.dumps(records_payload, ensure_ascii=False, sort_keys=True))
        try:
            data = lark._api(
                "POST",
                f"/open-apis/bitable/v1/apps/{base}/tables/{t56}/records/batch_create",
                payload={"records": records_payload}, query={"client_token": ctoken},
            )
            made = data.get("records", [])
            for it, rec in zip(creates, made):
                it["record_id"] = rec.get("record_id")
                it["committed"] = True
        except Exception as e:  # LarkError or transport/parse errors alike:
            for it in creates:  # never let one write phase nuke the response
                it["committed"] = False
                it["error"] = str(e)

    _commit_trips(result)
    return result


def _commit_trips(result):
    """Create the planned 5.x trip records. Writing the 5.x 预约信息 two-way
    link auto-fills the 5.6 back-link, so one create per trip is enough.
    A failed trip create leaves the 5.6 appointment intact — the next upload
    re-plans it as a 'backfill', so the link converges."""
    jobs = []  # (item, appointment_record_id)
    for it in result["items"]:
        trip = it.get("trip") or {}
        if trip.get("do") == "create" and it.get("record_id"):
            jobs.append((it, it["record_id"]))
        elif trip.get("do") == "backfill" and trip.get("append_to"):
            jobs.append((it, trip["append_to"]))
    if not jobs:
        return

    base = _base()
    tables = lark.config_values()["tables"]
    label = result.get("plan_table")
    tid = tables.get(label)
    if not tid:
        for it, _ in jobs:
            it["trip"]["committed"] = False
            it["trip"]["error"] = f"config.js 中找不到表「{label}」"
        return

    payload = [{"fields": {TRIP_LINK_FIELD: [rid]}} for _, rid in jobs]
    ctoken = _ctoken("trip", base, tid, ",".join(sorted(rid for _, rid in jobs)))
    try:
        data = lark._api(
            "POST",
            f"/open-apis/bitable/v1/apps/{base}/tables/{tid}/records/batch_create",
            payload={"records": payload}, query={"client_token": ctoken},
        )
        made = data.get("records", [])
        for (it, _), rec in zip(jobs, made):
            it["trip"]["record_id"] = rec.get("record_id")
            it["trip"]["committed"] = True
    except Exception as e:  # keep the committed 5.6 items in the response
        for it, _ in jobs:
            it["trip"]["committed"] = False
            it["trip"]["error"] = str(e)
