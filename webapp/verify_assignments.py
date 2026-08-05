# -*- coding: utf-8 -*-
"""
verify_assignments.py — ③核对：确认预约被挂到了正确的库存行（只读）
================================================================================

Step 3 of the operator's workflow: after ①新建预约 and ②计划同步, prove that
the right appointment landed on the right 3.1 record.

STRICTLY READ-ONLY — this module contains no write calls at all.

INPUT
    Either a柜号 (one per line, e.g. `ZCSU9034790B`), or full ②计划同步 lines
    (`柜号 路线 板数 箱数 [ISA 时间]`) — the extra columns are used as the
    EXPECTED values to compare against, so you can paste the very same batch
    you just synced and see whether reality matches it.

FOR EACH 3.1 ROW IT FOLLOWS THE WHOLE CHAIN
    3.1 (柜号+路线+仓库) --plan link--> 5.x 出库计划 --预约信息--> 5.6 预约
    and reports: 实际板数/预计板数, the 出库计划 record, and the appointment's
    ISA / 目的地 / 复制时间列 / 预约账号, plus every mismatch it can detect:

    · missing    — 3.1 row has no 出库计划 at all
    · no_isa     — 出库计划 exists but no appointment linked
    · isa_diff   — linked appointment's ISA ≠ the expected ISA you pasted
    · time_diff  — appointment 复制时间列 ≠ expected time
    · dest_diff  — appointment 目的地 ≠ the 3.1 目的地路线 (wrong bay!)
    · acct_diff  — appointment 预约账号 ≠ the warehouse's account
    · shared     — the same appointment serves several 3.1 rows (normal for a
                   grouped 预约号; listed so it is a conscious decision)
    · ok         — everything lines up
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import lark_client as lark
from appointment_sync import (F31, F56, WAREHOUSES, DEST_RE, ISA_RE,
                              norm_time, _search, _batch_get, _env_wiring,
                              _find_31, _disambiguate_31, _base)

VERIFY_WORKERS = 4


def parse_line(raw):
    """Accepts a bare 柜号, or a full ②计划同步 line (extra cols = expected).
    Returns {'awb','dest','isa','time','pallets','boxes'} (missing -> None).
    板数/箱数 are captured so split rows (same 柜号+路线) can be narrowed to
    the ONE row this line refers to — same rule as ②'s _disambiguate_31."""
    line = raw.replace(" ", " ").replace("　", " ").strip()
    if not line:
        return {"error": "空行"}
    toks = line.split()
    out = {"awb": toks[0].upper(), "dest": None, "isa": None, "time": None,
           "pallets": None, "boxes": None}
    if len(toks) == 1:
        return out
    if DEST_RE.match(toks[1].upper()):
        out["dest"] = toks[1].upper()
    # the ② line shape puts [板数] [箱数] right after the 路线 — short plain
    # integers there are those counts (an ISA is ≥8 digits, never confused)
    if (len(toks) >= 4 and re.fullmatch(r"\d{1,3}", toks[2])
            and re.fullmatch(r"\d{1,7}", toks[3]) and len(toks[3]) < 8):
        out["pallets"], out["boxes"] = int(toks[2]), int(toks[3])
    # scan the tail for an ISA and a time (positions vary by paste source)
    for i, t in enumerate(toks[2:], start=2):
        if ISA_RE.match(t) and out["isa"] is None:
            out["isa"] = int(t)
        elif "/" in t and i + 1 < len(toks):
            tn = norm_time(t + " " + toks[i + 1])
            if tn:
                out["time"] = tn
    return out


def parse_batch(text):
    rows = []
    for i, raw in enumerate((text or "").splitlines()):
        if not raw.strip():
            continue
        p = parse_line(raw)
        p["line_no"] = i + 1
        p["raw"] = raw
        rows.append(p)
    return rows


def verify(warehouse, text, progress=None):
    """Walk 3.1 -> 出库计划 -> 5.6 for every pasted 柜号. READ-ONLY."""
    tick = progress or (lambda **_: None)
    warehouse = (warehouse or "").strip()
    wh = WAREHOUSES.get(warehouse)
    if wh is None:
        raise lark.LarkError(f"未知仓库供应商「{warehouse}」")
    wiring = _env_wiring(wh.get("plan_table"))
    t31, t56 = lark.table_id("3.1"), lark.table_id("5.6")
    t5x = lark.table_id(wh["plan_table"]) if wiring.get("enabled") else None

    parsed = parse_batch(text)
    tick(stage="核对：跟踪 3.1 → 出库计划 → 预约", done=0, total=len(parsed))
    done = {"n": 0}
    lock = threading.Lock()
    # appointment record -> the 3.1 rows it serves (to flag sharing)
    isa_usage, usage_lock = {}, threading.Lock()

    def one(p):
        res = {"line_no": p["line_no"], "raw": p["raw"], "awb": p.get("awb"),
               "expected": {"dest": p.get("dest"), "isa": p.get("isa"),
                            "time": p.get("time")},
               "rows": [], "error": None}
        if "error" in p:
            res["error"] = p["error"]
        elif not wiring.get("enabled"):
            res["error"] = (f"DEV 环境无「{wh.get('plan_table')}」副本表 — 无法核对出库计划"
                            if lark.env() == "dev" and wh.get("plan_table")
                            else f"仓库 {warehouse} 未配置出库计划表")
        else:
            try:
                res["rows"] = _verify_awb(p, warehouse, wh, wiring, t31, t56, t5x,
                                          isa_usage, usage_lock)
            except lark.LarkError as e:
                res["error"] = str(e)
        with lock:
            done["n"] += 1
            tick(done=done["n"], total=len(parsed),
                 current=f"第{p['line_no']}行 {p.get('awb') or ''}")
        return res

    if len(parsed) <= 1:
        results = [one(p) for p in parsed]
    else:
        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
            results = list(pool.map(one, parsed))

    # second pass: mark appointments serving multiple 3.1 rows
    for res in results:
        for row in res["rows"]:
            rec = (row.get("appointment") or {}).get("record_id")
            if rec and len(isa_usage.get(rec, [])) > 1:
                row["shared_with"] = [x for x in isa_usage[rec]
                                      if x != row["record_id"]]
                if "shared" not in row["flags"]:
                    row["flags"].append("shared")

    counts = {}
    for res in results:
        for row in res["rows"]:
            for f in (row["flags"] or ["ok"]):
                counts[f] = counts.get(f, 0) + 1
    summary = {"lines": len(results),
               "rows": sum(len(r["rows"]) for r in results),
               "errors": sum(1 for r in results if r["error"]),
               "flags": counts,
               "ok": counts.get("ok", 0),
               "problems": sum(v for k, v in counts.items()
                               if k not in ("ok", "shared"))}
    return {"env": lark.env(), "warehouse": warehouse,
            "account": wh.get("account"), "plan_table": wh.get("plan_table"),
            "results": results, "summary": summary}


def _verify_awb(p, warehouse, wh, wiring, t31, t56, t5x, isa_usage, usage_lock):
    """All 3.1 rows for this 柜号 (optionally narrowed to one 路线), each with
    its resolved 出库计划 + 预约 and the mismatch flags."""
    plan_link = wiring["plan_link_31"]
    isa_field, inv_field = wiring["isa_field"], wiring["inv_field"]

    if p.get("dest"):
        hits = _find_31(t31, p["awb"], p["dest"], warehouse, plan_link)
        # Split rows: when the pasted line carries 板数/箱数, narrow to the
        # ONE row it refers to (same rule as ②). Without those numbers —
        # or if they can't decide — list every row: for an AUDIT view,
        # showing all candidates is correct, not an error.
        if len(hits) > 1 and p.get("boxes") is not None:
            matched, _how = _disambiguate_31(hits, p.get("pallets"), p["boxes"])
            if matched is not None:
                hits = [matched]
    else:
        # no 路线 given: every row of this 柜号 for this warehouse
        hits = _search(t31, [
            {"field_name": F31["awb"], "operator": "is", "value": [p["awb"]]},
            {"field_name": F31["warehouse"], "operator": "is", "value": [warehouse]},
        ], [F31["awb"], F31["route"], F31["actual"], F31["estimated"],
            F31["boxes"], F31["batch"], plan_link], page_size=50)
    if not hits:
        raise lark.LarkError(f"3.1 无匹配行（柜号 {p['awb']}"
                             + (f" + 路线 {p['dest']}" if p.get("dest") else "")
                             + f" + 仓库 {warehouse}）")

    out = []
    for h in hits:
        f = h.get("fields") or {}
        row = {"record_id": h["record_id"],
               "awb": lark.flat_text(f.get(F31["awb"])),
               "route": f.get(F31["route"]),
               "actual": lark.flat_text(f.get(F31["actual"])) or None,
               "estimated": lark.num_of(f.get(F31["estimated"])),
               "boxes": lark.num_of(f.get(F31["boxes"])),
               "batch": lark.flat_text(f.get(F31["batch"])) or None,
               "trip": None, "appointment": None, "flags": []}
        trips = lark.link_ids(f.get(plan_link))
        if not trips:
            row["flags"].append("missing")
            out.append(row)
            continue
        trip_id = trips[0]
        row["trip"] = {"record_id": trip_id, "count": len(trips)}
        if len(trips) > 1:
            row["flags"].append("multi_trip")
        trip = _batch_get(t5x, [trip_id], [isa_field, inv_field]).get(trip_id, {})
        isa_ids = lark.link_ids(trip.get(isa_field))
        if not isa_ids:
            row["flags"].append("no_isa")
            out.append(row)
            continue
        rec56 = isa_ids[0]
        a = _batch_get(t56, [rec56], [F56["isa"], F56["time"], F56["dest"],
                                      F56["account"]]).get(rec56, {})
        isa_num = lark.num_of(a.get(F56["isa"]))
        appt = {"record_id": rec56,
                "isa": int(isa_num) if isa_num is not None else None,
                "time": norm_time(lark.flat_text(a.get(F56["time"]))),
                "dest": a.get(F56["dest"]),
                "account": a.get(F56["account"])}
        row["appointment"] = appt
        with usage_lock:
            isa_usage.setdefault(rec56, []).append(h["record_id"])

        # ---- mismatch detection -----------------------------------------
        exp = p
        if exp.get("isa") is not None and appt["isa"] != exp["isa"]:
            row["flags"].append("isa_diff")
        if exp.get("time") and appt["time"] != exp["time"]:
            row["flags"].append("time_diff")
        # the appointment's 目的地 should equal the 3.1 row's 目的地路线 —
        # a mismatch means the shipment is on the wrong bay's appointment
        if appt["dest"] and row["route"] and appt["dest"] != row["route"]:
            row["flags"].append("dest_diff")
        if wh.get("account") and appt["account"] \
                and appt["account"] != wh["account"]:
            row["flags"].append("acct_diff")
        out.append(row)
    return out
