# -*- coding: utf-8 -*-
"""
appointment_create.py — 新建预约（仅写 5.6 预约表）
================================================================================

A deliberately narrow step, separate from the full 预约同步 flow: paste
appointment lines, pick the warehouse (-> 预约账号), and the tool creates the
5.6 records that DON'T exist yet. It writes nothing else — no 3.1, no trips.

INPUT FORMAT (one appointment per line, whitespace/tab separated)
    目的地  ISA  MM/DD/YYYY HH:MM [TZ]

    YVR4  7405010996   08/06/2026 20:00 PDT
    YEG1  7229872996   08/07/2026 08:00 MDT

FIELDS WRITTEN (dev/prod resolved by LARK_ENV):
    ISA (Number) · 目的地 (SingleSelect) · 预约账号 (SingleSelect, from the
    warehouse dropdown) · 复制时间列 (Text 'MM/DD/YYYY HH:MM', TZ dropped —
    same normalization as the sync flow).

UNIQUENESS — one ISA must exist at most once. Three layers:
    1. in-batch: repeated ISAs in one paste create ONCE (later lines -> dup);
       if they disagree on 目的地/时间 the group is blocked instead.
    2. against the table: plan() searches 5.6 per unique ISA (parallel).
    3. across rapid waves: Bitable's search index can lag a just-created
       record, so every create is also registered in RECENT_CREATES
       (in-process, TTL'd). plan()/commit() consult it BEFORE the search —
       a wave-2 paste seconds after wave 1 still sees wave 1's ISAs even if
       the search index hasn't caught up. commit() additionally re-plans
       under the process-wide WRITE_LOCK, so two overlapping submissions
       cannot both pass the existence check.
    Read-back verification measures the actual index latency per created ISA
    (searched until visible, capped) and reports it in the result.
"""

import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import lark_client as lark
from appointment_sync import (F56, WAREHOUSES, DEST_RE, ISA_RE, TZ_RE,
                              norm_time, store_time, _search, _sig, _base)

CHECK_WORKERS = 4          # parallel per-ISA existence lookups during plan
VERIFY_WAIT_CAP = 10.0     # seconds to wait for a created ISA to be searchable

# ---------------------------------------------------------------------------
# Recent-creates registry — closes the read-after-write window (layer 3).
# {isa(int): {"record_id", "ts", "dest", "time", "account"}}
# ---------------------------------------------------------------------------
RECENT_TTL = 600.0
_RECENT = {}
_RECENT_LOCK = threading.Lock()


def _recent_get(isa):
    with _RECENT_LOCK:
        hit = _RECENT.get(isa)
        if hit and time.time() - hit["ts"] <= RECENT_TTL:
            return hit
        if hit:
            del _RECENT[isa]
        return None


def _recent_put(isa, record_id, dest, time_norm, account):
    with _RECENT_LOCK:
        _RECENT[isa] = {"record_id": record_id, "ts": time.time(),
                        "dest": dest, "time": time_norm, "account": account}


# ===========================================================================
# 1. PARSING
# ===========================================================================

def parse_line(raw):
    """[目的地] [ISA] [日期] [时间] [时区?] -> dict, or {'error': 原因}."""
    line = raw.replace(" ", " ").replace("　", " ").strip()
    if not line:
        return {"error": "空行"}
    toks = line.split()
    if len(toks) < 4 or len(toks) > 5:
        return {"error": f"列数 {len(toks)} 无法解析 — 需要 "
                         "[目的地] [ISA] [MM/DD/YYYY] [HH:MM] [时区可选]"}
    dest, isa_s = toks[0].upper(), toks[1]
    if not DEST_RE.match(dest):
        return {"error": f"目的地「{toks[0]}」格式不像仓点代码（如 YEG2 / XCAB）"}
    if not ISA_RE.match(isa_s):
        return {"error": f"ISA「{isa_s}」应为 8-15 位数字"}
    time_norm = norm_time(toks[2] + " " + toks[3])
    if not time_norm:
        return {"error": f"预约时间「{toks[2]} {toks[3]}」无法解析 — "
                         "需要 MM/DD/YYYY HH:MM（如 08/06/2026 20:00）"}
    tz = None
    if len(toks) == 5:
        if not TZ_RE.match(toks[4]):
            return {"error": f"时区「{toks[4]}」无法识别（如 PDT / MDT）"}
        tz = toks[4]
    return {"raw": raw, "dest": dest, "isa": int(isa_s), "time": time_norm, "tz": tz}


def parse_batch(text):
    rows = []
    for i, raw in enumerate((text or "").splitlines()):
        if not raw.strip():
            continue
        p = parse_line(raw)
        p["line_no"] = i + 1
        p.setdefault("raw", raw)
        rows.append(p)
    return rows


# ===========================================================================
# 2. PLAN — read-only existence pass
# ===========================================================================

def plan(warehouse, text, progress=None):
    """Decide create / exists / dup / block per line. READ-ONLY."""
    tick = progress or (lambda **_: None)
    warehouse = (warehouse or "").strip()
    wh = WAREHOUSES.get(warehouse)
    if wh is None:
        raise lark.LarkError(f"未知仓库供应商「{warehouse}」")
    account = wh.get("account")

    tick(stage="读取 5.6 字段选项")
    t56 = lark.table_id("5.6")
    fm56 = lark.field_meta(t56)["by_name"]
    valid_dests = set(((fm56.get(F56["dest"]) or {}).get("options") or {}).values())
    valid_accounts = set(((fm56.get(F56["account"]) or {}).get("options") or {}).values())

    parsed = parse_batch(text)

    # ---- layer 1: in-batch grouping --------------------------------------
    # leader line creates; later same-ISA lines are 'dup'. A group whose
    # lines disagree on 目的地/时间 is blocked entirely (never guess).
    leaders, conflicts = {}, set()
    for p in parsed:
        if "error" in p:
            continue
        lead = leaders.get(p["isa"])
        if lead is None:
            leaders[p["isa"]] = p
        elif (lead["dest"], lead["time"]) != (p["dest"], p["time"]):
            conflicts.add(p["isa"])

    # ---- layer 2+3: existence per unique ISA (parallel) ------------------
    tick(stage="检查 ISA 是否已存在", done=0, total=len(leaders))
    existing = {}            # isa -> {'source', 'record_id', dest/time/account}
    done = {"n": 0}
    lock = threading.Lock()

    def check(isa):
        hit = _recent_get(isa)
        if hit:
            res = {"source": "recent", **{k: hit[k] for k in
                                          ("record_id", "dest", "time", "account")}}
        else:
            res = None
            hits = _search(t56, [
                {"field_name": F56["isa"], "operator": "is", "value": [str(isa)]},
            ], [F56["isa"], F56["time"], F56["dest"], F56["account"]])
            if hits:
                f = hits[0].get("fields") or {}
                res = {"source": "search", "record_id": hits[0]["record_id"],
                       "dest": f.get(F56["dest"]),
                       "time": norm_time(lark.flat_text(f.get(F56["time"]))),
                       "account": f.get(F56["account"]),
                       "count": len(hits)}
        with lock:
            existing[isa] = res
            done["n"] += 1
            tick(done=done["n"], total=len(leaders), current=f"ISA {isa}")
        return res

    isas = list(leaders)
    if len(isas) <= 1:
        for i in isas:
            check(i)
    else:
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            list(pool.map(check, isas))

    # ---- assemble per-line results ----------------------------------------
    rows = []
    for p in parsed:
        row = {"line_no": p["line_no"], "raw": p.get("raw"),
               "parsed": None, "action": None, "existing": None,
               "warnings": [], "blockers": [], "notes": []}
        if "error" in p:
            row["action"] = "block"
            row["blockers"].append(p["error"])
            row["sig"] = _sig([])
            rows.append(row)
            continue
        row["parsed"] = {k: p[k] for k in ("dest", "isa", "time", "tz")}
        ex = existing.get(p["isa"])

        if p["isa"] in conflicts:
            row["action"] = "block"
            row["blockers"].append("同一 ISA 在本批内目的地/时间不一致 — 请统一后重试")
        elif ex:
            row["action"] = "exists"
            row["existing"] = ex
            if ex.get("count", 1) > 1:
                row["warnings"].append(f"表内 ISA {p['isa']} 已有多条记录 — 请人工清理")
            diffs = []
            if ex.get("dest") and ex["dest"] != p["dest"]:
                diffs.append(f"目的地 表内={ex['dest']} vs 输入={p['dest']}")
            if ex.get("time") and ex["time"] != p["time"]:
                diffs.append(f"时间 表内={ex['time']} vs 输入={p['time']}")
            if diffs:
                row["warnings"].append("已存在但内容不同（" + "；".join(diffs)
                                       + "）— 本界面不做修改，请去「预约同步」处理")
            else:
                row["notes"].append("已存在，内容一致 — 跳过"
                                    + ("（刚在本机创建）" if ex["source"] == "recent" else ""))
        elif leaders[p["isa"]] is not p:
            row["action"] = "dup"
            row["notes"].append(f"与第 {leaders[p['isa']]['line_no']} 行重复 — 只创建一次")
        else:
            if p["dest"] not in valid_dests:
                row["action"] = "block"
                row["blockers"].append(f"目的地「{p['dest']}」不是 5.6 的现有选项 — "
                                       "绝不自动新增选项")
            elif not account:
                row["action"] = "block"
                row["blockers"].append(f"仓库 {warehouse} 未配置预约账号，无法创建")
            elif account not in valid_accounts:
                row["action"] = "block"
                row["blockers"].append(f"预约账号「{account}」不是 5.6 的现有选项")
            else:
                row["action"] = "create"
                # p["time"] is canonical; 复制时间列 STORES month-first
                row["fields"] = {F56["isa"]: p["isa"], F56["dest"]: p["dest"],
                                 F56["account"]: account,
                                 F56["time"]: store_time(p["time"])}
        row["sig"] = _sig([row["action"], row.get("fields") or {}])
        rows.append(row)

    summary = {"lines": len(rows),
               "create": sum(1 for r in rows if r["action"] == "create"),
               "exists": sum(1 for r in rows if r["action"] == "exists"),
               "dup": sum(1 for r in rows if r["action"] == "dup"),
               "block": sum(1 for r in rows if r["action"] == "block"),
               "warnings": sum(len(r["warnings"]) for r in rows)}
    return {"env": lark.env(), "warehouse": warehouse, "account": account,
            "rows": rows, "summary": summary}


# ===========================================================================
# 3. COMMIT — create the approved missing ISAs, then verify + measure latency
# ===========================================================================

def commit(warehouse, text, approvals, client_env, progress=None):
    if client_env != lark.env():
        raise lark.LarkError(
            f"环境不匹配：页面为 {client_env}，服务端为 {lark.env()} — 请刷新页面")
    tick = progress or (lambda **_: None)
    waited = 0.0
    while not lark.WRITE_LOCK.acquire(timeout=1.0):
        waited += 1.0
        tick(stage=f"等待另一个写入完成…（已等 {int(waited)} 秒）")
        if waited >= 300:
            raise lark.LarkError("等待写入锁超过 5 分钟 — 请检查服务端后重试")
    try:
        return _commit_locked(warehouse, text, approvals, tick)
    finally:
        lark.WRITE_LOCK.release()


def _commit_locked(warehouse, text, approvals, tick):
    def replan_tick(**kw):
        if kw.get("stage"):
            kw["stage"] = "复检 · " + kw["stage"]
        tick(**kw)

    tick(stage="复检（重新检查 ISA 是否已存在）")
    result = plan(warehouse, text, progress=replan_tick)
    by_line = {r["line_no"]: r for r in result["rows"]}

    creates = []
    for a in approvals or []:
        r = by_line.get(a.get("line_no"))
        if r is None:
            continue
        r["approved"] = True
        if r["action"] == "create" and _sig([r["action"], r.get("fields") or {}]) \
                == a.get("sig"):
            creates.append(r)
        elif r["action"] == "exists":
            # the freshness re-plan found it meanwhile (another wave / another
            # operator) — exactly the dedup working; report, don't create
            r["commit"] = {"done": True, "skipped": "复检时发现已存在 — 未重复创建"}
        elif r["action"] in ("dup", "block"):
            r["commit"] = {"done": False, "skipped": "同批重复或拦截，未执行"}
        else:
            r["commit"] = {"done": False,
                           "skipped": "情况已变化（与预检时不同）— 请重新查询后再执行"}

    t56, base = lark.table_id("5.6"), _base()
    if creates:
        tick(stage=f"写入 · 新建 {len(creates)} 条预约（5.6）", done=0,
             total=len(creates), current="")
        payload = {"records": [{"fields": r["fields"]} for r in creates]}
        try:
            made = lark._api(
                "POST",
                f"/open-apis/bitable/v1/apps/{base}/tables/{t56}/records/batch_create",
                payload=payload, query={"client_token": str(uuid.uuid4())},
            ).get("records", [])
            for r, rec in zip(creates, made):
                rid = rec.get("record_id")
                r["commit"] = {"done": True, "record_id": rid}
                p = r["parsed"]
                # layer 3: visible to the NEXT wave immediately, even if the
                # Bitable search index lags this record
                _recent_put(p["isa"], rid, p["dest"], p["time"], result["account"])
        except Exception as e:  # noqa
            for r in creates:
                r["commit"] = {"done": False, "error": f"5.6 创建失败：{e}"}
            creates = []

    # ---- read-back verification + index-latency measurement ---------------
    lat = []
    for i, r in enumerate(creates):
        isa = r["parsed"]["isa"]
        tick(stage="核实 · 回读校验（并测量索引延迟）", done=i, total=len(creates),
             current=f"ISA {isa}")
        t0 = time.time()
        visible = None
        delay = 0.3
        while time.time() - t0 < VERIFY_WAIT_CAP:
            hits = _search(t56, [
                {"field_name": F56["isa"], "operator": "is", "value": [str(isa)]},
            ], [F56["isa"]])
            if hits:
                visible = round(time.time() - t0, 2)
                break
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
        c = r["commit"]
        c["verified"] = visible is not None
        c["search_visible_after"] = visible          # None = not within cap
        if visible is None:
            c["note"] = (f"已创建（record {c.get('record_id')}），但 {VERIFY_WAIT_CAP:.0f} 秒内"
                         "搜索仍不可见 — 本机缓存会兜底去重，但请留意")
        lat.append(visible)
    tick(stage="完成", current="")

    good = [v for v in lat if v is not None]
    result["latency"] = {
        "created": len(lat),
        "visible": len(good),
        "min": min(good) if good else None,
        "max": max(good) if good else None,
        "avg": round(sum(good) / len(good), 2) if good else None,
    }
    ws = []
    for r in result["rows"]:
        for w in r["warnings"]:
            ws.append(f"第{r['line_no']}行 ISA {(r.get('parsed') or {}).get('isa', '?')}: {w}")
        c = r.get("commit") or {}
        if c.get("error"):
            ws.append(f"第{r['line_no']}行: {c['error']}")
        if c.get("note"):
            ws.append(f"第{r['line_no']}行: {c['note']}")
    result["warnings_summary"] = ws
    result["committed"] = True
    return result
