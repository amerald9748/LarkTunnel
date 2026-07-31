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

3.1 实际板数 reconciliation (板数校验 + 回写)
  * Each uploaded row is matched to its 3.1 inventory row by
    柜号+路线+仓库供应商; the file's pallet count is written into the TEXT
    column 实际板数. Statuses: fill (empty), update (existing is a plain
    number and differs), same, keep (existing is non-numeric human text —
    NEVER overwritten), conflict (two rows target one 3.1 record with
    different values — both refused), dup (same value, merged to one write).
  * GUARD: if |file pallets − 预计板数| > PALLET_DIFF_BLOCK the row is
    BLOCKED, and the block poisons its whole ISA group — a grouped 预约号
    covering several containers must not be created via a sibling row.
    Blocked rows write nothing: no 5.6 create, no trip, no pallet write.

`plan()` performs only READS. `commit()` is the sole writer (create records).
"""

import re
import json
import math
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

# ---- 3.1 实际板数 reconciliation -------------------------------------------
# 上传时把文件里的板数写进 3.1 的实际板数（文本字段，核实于 2026-07-30）。
# 匹配键 = 柜号/AWB + 目的地路线 + 仓库供应商（同一柜号按路线拆多行）；
# 柜号精确匹配不到时回退为前缀匹配（拆柜后缀 …A/…B）。
F31 = {"awb": "柜号/AWB", "warehouse": "仓库供应商", "route": "目的地路线",
       "actual": "实际板数", "estimated": "预计板数"}

# 文件板数与 3.1 预计板数（公式列）差距超过该值 → 拦截该行上传并报错。
PALLET_DIFF_BLOCK = 3


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


def _t31():
    return lark.config_values()["tables"]["3.1"]


def _flat_text(v):
    """Text out of a search-API field value (segments list / wrapped / plain)."""
    if isinstance(v, list):
        return "".join(x.get("text", "") for x in v if isinstance(x, dict)).strip()
    if isinstance(v, dict):
        return _flat_text(v.get("value")) if "value" in v else ""
    if v is None:
        return ""
    return str(v).strip()


def _num_of(v):
    """Number out of a search-API field value ({'type':2,'value':[n]} / text
    segments / plain). None when not numeric."""
    if isinstance(v, dict) and "value" in v:
        v = v["value"]
    if isinstance(v, list):
        if v and isinstance(v[0], dict):   # text-segment list -> flatten first
            v = _flat_text(v)
        else:
            v = v[0] if v else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_n(x):
    return str(int(x)) if float(x).is_integer() else ("%g" % x)


def _find_31(awb, route, warehouse):
    """3.1 rows for 柜号+路线+仓库. Exact 柜号 first; fall back to a contains
    search tightened to prefix matches (拆柜后缀 …A/…B). READ."""
    base, t31 = _base(), _t31()

    def search(op):
        conds = [
            {"field_name": F31["awb"], "operator": op, "value": [awb]},
            {"field_name": F31["warehouse"], "operator": "is", "value": [warehouse]},
            {"field_name": F31["route"], "operator": "is", "value": [route]},
        ]
        return lark._api(
            "POST", f"/open-apis/bitable/v1/apps/{base}/tables/{t31}/records/search",
            payload={"filter": {"conjunction": "and", "conditions": conds},
                     "field_names": [F31["awb"], F31["actual"], F31["estimated"]],
                     "automatic_fields": False},
            query={"page_size": 10},
        ).get("items", [])

    hits = search("is")
    if not hits:
        # 拆柜/截断后缀 = 恰好多 1 个字符（…A/…B 或补回的第 8 位数字）。收紧到
        # 该模式，避免把「MATU…/ZCSU…」这类合并柜号行误当成本柜号的行。
        pat = re.compile(re.escape(awb) + r"[A-Z0-9]")
        hits = [h for h in search("contains")
                if pat.fullmatch(_flat_text((h.get("fields") or {}).get(F31["awb"])))]
    return hits


def _pallet_plan(rec, warehouse, cache):
    """READ-ONLY decision for one record's 3.1 实际板数:
    fill / update / same / blocked (diff>PALLET_DIFF_BLOCK) / nomatch /
    ambiguous / nofile. `blocked` stops the whole row's upload."""
    awb = str(rec.get("awb") or "").strip()
    route = str(rec.get("route") or "").strip()
    raw = rec.get("pallets")
    out = {"status": None, "note": None}
    if raw is None or str(raw).strip() == "":
        out.update(status="nofile", note="文件无板数")
        return out
    try:
        pnum = float(raw)
    except (TypeError, ValueError):
        out.update(status="nofile", note=f"板数「{raw}」非数字")
        return out
    if not math.isfinite(pnum):   # NaN/inf would slip past the diff guard
        out.update(status="nofile", note=f"板数「{raw}」不是有效数字")
        return out
    out["value"] = _fmt_n(pnum)
    if not awb or not route:
        out.update(status="nomatch", note="缺柜号/路线，无法匹配 3.1")
        return out

    key = (awb, route)
    if key not in cache:
        cache[key] = _find_31(awb, route, warehouse)
    rows = cache[key]
    if not rows:
        out.update(status="nomatch", note="3.1 无匹配行")
        return out
    if len(rows) > 1:
        out.update(status="ambiguous", note=f"3.1 匹配 {len(rows)} 行，请手动处理")
        return out

    f = rows[0].get("fields") or {}
    est = _num_of(f.get(F31["estimated"]))
    existing = _flat_text(f.get(F31["actual"]))
    out.update(record_id=rows[0]["record_id"], estimated=est,
               existing=existing or None)
    if est is not None and abs(pnum - est) > PALLET_DIFF_BLOCK:
        out.update(status="blocked", blocked=True,
                   note=(f"板数差异过大：文件 {_fmt_n(pnum)} vs 预计板数 "
                         f"{_fmt_n(est)}（差 {_fmt_n(abs(pnum - est))} > "
                         f"{PALLET_DIFF_BLOCK}），已阻止上传"))
        return out
    if not existing:
        out.update(status="fill")
    else:
        old = _num_of(existing)
        if (old is not None and old == pnum) or existing == out["value"]:
            out.update(status="same", note="与现值一致")
        elif re.fullmatch(r"\d+(?:\.\d+)?", existing):
            out.update(status="update", note=f"原值 {existing} → {out['value']}")
        else:
            # non-numeric human annotation (e.g. "13P-9") — never clobber it
            out.update(status="keep",
                       note=f"已有人工值「{existing}」，未覆盖（文件板数 {out['value']}）")
    if est is None and out["status"] in ("fill", "update"):
        out["note"] = ((out.get("note") or "") + "（预计板数为空，未校验差异）").strip()
    return out


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

    wh_raw = (warehouse or "").strip()
    planned = set()       # ISAs already scheduled to be created in THIS batch
    trip_planned = set()  # ISAs whose trip (new or backfill) is already planned
    p_cache = {}          # (awb, route) -> 3.1 rows, shared across the batch

    # Pass 1 — pallet reconciliation per row (READ). Skipped when the account
    # is invalid: every row blocks on the account anyway, and an empty/unknown
    # warehouse would make the 3.1 search meaningless. A diff-blocked row
    # poisons its WHOLE ISA group — a grouped 预约号 covers several containers
    # and must not be created via a sibling row behind the block.
    pallet_plans = []
    blocked_isas = {}  # isa -> awb of the row that tripped the guard
    for r in records:
        isa = str(r.get("isa") or "").strip()
        p = (_pallet_plan(r, wh_raw, p_cache)
             if isa.isdigit() and not acc_reason else None)
        pallet_plans.append(p)
        if p and p.get("blocked") and isa not in blocked_isas:
            blocked_isas[isa] = str(r.get("awb") or "?")

    items = []
    for r, pallet in zip(records, pallet_plans):
        awb = r.get("awb")
        route = r.get("route")
        isa = str(r.get("isa") or "").strip()
        t = fmt_time(r.get("appointment"))
        it = {"awb": awb, "route": route, "isa": isa or None,
              "account": account, "time": t, "action": None, "reason": None,
              "exists": False, "trip": {"do": "none"}}

        ex = existing.get(isa) or []
        if pallet is not None:
            it["pallet"] = pallet
        if not isa.isdigit():
            it["action"], it["reason"] = "block", "无有效预约号(ISA)"
        elif isa in blocked_isas:
            # 文件板数 vs 3.1 预计板数 差距超限 -> 整组拦截（不建预约/不建
            # 出库计划/不写实际板数），包括同预约号下未超限的兄弟行
            if pallet and pallet.get("blocked"):
                reason = pallet["note"]
            else:
                reason = (f"同预约号(ISA)下柜号 {blocked_isas[isa]} 板数差异"
                          f"超限，整组已阻止上传")
            if ex:
                it["exists"] = True
                reason += "（该预约已存在，本次不补建出库计划、不更新板数）"
            it["action"], it["reason"] = "block", reason
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

    # Cross-row consistency: rows that resolve to the SAME 3.1 record must
    # agree on the pallet value. Conflicts are refused HERE, at plan time —
    # before anything is written; identical duplicates merge into one write.
    by_rid = {}
    for it in items:
        p = it.get("pallet") or {}
        if (it["action"] != "block" and p.get("status") in ("fill", "update")
                and p.get("record_id")):
            by_rid.setdefault(p["record_id"], []).append(p)
    for ps in by_rid.values():
        vals = sorted({p["value"] for p in ps})
        if len(vals) > 1:
            for p in ps:
                p.update(status="conflict",
                         note=(f"同批对同一 3.1 行给出不同板数（{' / '.join(vals)}），"
                               f"均未写入，请人工处理"))
        else:
            for p in ps[1:]:
                p.update(status="dup", note="同批重复（同值），合并为一次写入")

    summary = {"create": 0, "skip": 0, "block": 0, "exists": 0,
               "trip_create": 0, "trip_backfill": 0,
               "pallet_write": 0, "pallet_block": 0}
    for it in items:
        summary[it["action"]] += 1
        if it.get("exists"):
            summary["exists"] += 1
        do = (it.get("trip") or {}).get("do")
        if do == "create":
            summary["trip_create"] += 1
        elif do == "backfill":
            summary["trip_backfill"] += 1
        p = it.get("pallet") or {}
        if p.get("status") == "blocked":
            summary["pallet_block"] += 1
        elif it["action"] != "block" and p.get("status") in ("fill", "update"):
            summary["pallet_write"] += 1
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
    _commit_pallets(result)
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


def _commit_pallets(result):
    """batch_update 3.1 实际板数 for the planned fill/update rows. Never touched:
    blocked rows, conflict/dup/keep/same rows (plan() marks those), rows whose
    own 5.6 create failed in this commit, and rows without a 3.1 match."""
    jobs, primary, dups = [], {}, []
    for it in result["items"]:
        p = it.get("pallet") or {}
        if it.get("action") == "block" or p.get("status") not in ("fill", "update"):
            continue
        if it.get("action") == "create" and not it.get("record_id"):
            continue  # its 5.6 create failed — a rejected row must write nothing
        rid = p.get("record_id")
        if not rid:
            continue
        prim = primary.get(rid)
        if prim is not None:
            if p.get("value") == prim.get("value"):
                dups.append((p, prim))  # mirror the primary's outcome later
            else:
                # plan() refuses conflicts before commit; if one still appears,
                # withdraw BOTH sides — never write an arbitrary winner
                if prim in jobs:
                    jobs.remove(prim)
                for q in (prim, p):
                    q["committed"] = False
                    q["error"] = "同批对同一 3.1 行板数冲突，均未写入"
            continue
        primary[rid] = p
        jobs.append(p)

    if jobs:
        base, t31 = _base(), _t31()
        payload = [{"record_id": p["record_id"],
                    "fields": {F31["actual"]: p["value"]}} for p in jobs]
        ctoken = _ctoken("pallet", base, t31,
                         json.dumps({p["record_id"]: p["value"] for p in jobs},
                                    sort_keys=True))
        try:
            lark._api(
                "POST",
                f"/open-apis/bitable/v1/apps/{base}/tables/{t31}/records/batch_update",
                payload={"records": payload}, query={"client_token": ctoken},
            )
            for p in jobs:
                p["committed"] = True
        except Exception as e:
            for p in jobs:
                p["committed"] = False
                p["error"] = str(e)
    for p, prim in dups:
        p["committed"] = prim.get("committed")
        if prim.get("error"):
            p["error"] = prim["error"]
