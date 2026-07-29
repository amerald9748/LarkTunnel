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

`plan()` performs only READS. `commit()` is the sole writer (create records).
"""

import re
import uuid
import lark_client as lark

# 仓库供应商 → 5.6 预约账号 alias (from config warehouses[].accountAlias)
ACCOUNT_ALIAS = {"VAST": "元浩"}


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


def _existing_isas(isas):
    """Map isa -> list of existing 5.6 rows (dedup key). READ."""
    base, t56 = _base(), _t56()
    out = {}
    for isa in isas:
        hits = lark._api(
            "POST", f"/open-apis/bitable/v1/apps/{base}/tables/{t56}/records/search",
            payload={"filter": {"conjunction": "and", "conditions": [
                {"field_name": "ISA", "operator": "is", "value": [str(isa)]}]},
                "field_names": ["ISA", "目的地", "预约账号"], "automatic_fields": False},
            query={"page_size": 5},
        ).get("items", [])
        out[isa] = hits
    return out


def plan(records, warehouse):
    """READ-ONLY. Decide, for each record, what would happen in 5.6.
    Returns {'items': [...], 'summary': {...}, 'account': str|None}."""
    t56 = _t56()
    fm = lark.field_meta(t56)["by_name"]
    valid_dests = set((fm.get("目的地") or {}).get("options", {}).values())
    valid_accounts = set((fm.get("预约账号") or {}).get("options", {}).values())
    account, acc_reason = map_account(warehouse, valid_accounts)

    isas = {str(r.get("isa")).strip() for r in records
            if str(r.get("isa") or "").strip().isdigit()}
    existing = _existing_isas(isas)

    planned = set()      # ISAs already scheduled to be created in THIS batch
    items = []
    for r in records:
        awb = r.get("awb")
        route = r.get("route")
        isa = str(r.get("isa") or "").strip()
        t = fmt_time(r.get("appointment"))
        it = {"awb": awb, "route": route, "isa": isa or None,
              "account": account, "time": t, "action": None, "reason": None,
              "exists": False}

        ex = existing.get(isa) or []
        if not isa.isdigit():
            it["action"], it["reason"] = "block", "无有效预约号(ISA)"
        elif ex:
            it["action"], it["reason"], it["exists"] = "skip", "当前派送记录已存在", True
            dests = sorted({lark.display_value("目的地", h["fields"].get("目的地"), None)
                            for h in ex if h.get("fields", {}).get("目的地")})
            if dests:
                it["existing_dest"] = ", ".join(dests)
        elif isa in planned:
            it["action"], it["reason"], it["exists"] = "skip", "当前派送记录已存在", True
        elif route not in valid_dests:
            it["action"], it["reason"] = "block", f"路线「{route}」非 5.6 目的地选项"
        elif acc_reason:
            it["action"], it["reason"] = "block", acc_reason
        else:
            it["action"] = "create"
            it["fields"] = {"ISA": int(isa), "目的地": route,
                            "预约账号": account, "复制时间列": t}
            planned.add(isa)
        items.append(it)

    summary = {"create": 0, "skip": 0, "block": 0, "exists": 0}
    for it in items:
        summary[it["action"]] += 1
        if it.get("exists"):
            summary["exists"] += 1
    return {"items": items, "summary": summary, "account": account,
            "account_reason": acc_reason}


def commit(records, warehouse):
    """Re-plan (fresh existence check) and CREATE the 'create' items in 5.6.
    Returns the plan items, each create annotated with record_id or error."""
    base, t56 = _base(), _t56()
    result = plan(records, warehouse)
    creates = [it for it in result["items"] if it["action"] == "create"]
    if not creates:
        return result

    records_payload = [{"fields": it["fields"]} for it in creates]
    ctoken = str(uuid.uuid4())
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
    except lark.LarkError as e:
        for it in creates:
            it["committed"] = False
            it["error"] = str(e)
    return result
