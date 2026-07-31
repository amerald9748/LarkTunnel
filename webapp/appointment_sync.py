# -*- coding: utf-8 -*-
"""
appointment_sync.py — 到仓核对 & 预约/派送计划同步（batch, explicit-commit）
================================================================================

Implements `docs/40 Workflows/Appointment Sync Runbook.md` as the webapp's
primary flow:

    parse pasted lines  ->  plan() [READ-ONLY]  ->  operator reviews & ticks
    rows  ->  commit() [writes ONLY the approved rows, re-planned freshly]

INPUT FORMAT (one shipment per line, whitespace/tab separated)
---------------------------------------------------------------
    柜号/AWB  目的地路线  实际板数  箱数  [ISA  MM/DD/YYYY HH:MM [TZ]]

    OOCU9020713B  YEG2  4  141                              <- basic (search + 板数)
    OOCU9020713B  YEG2  4  141  7403350996  07/30/2026 13:00 PDT   <- full (预约同步)

  * The two trailing numbers of the basic form are REQUIRED to be
    [实际板数, 箱数] — a line like "CSGU6249922  YVR4    96" (only one
    number) is ambiguous and is REJECTED until the operator fixes it.
  * The optional tail must be BOTH the ISA and the appointment time —
    one without the other is rejected (nothing could be safely done with it).
  * The TZ token (PDT/MDT/...) is accepted and shown, but NOT stored:
    5.6 复制时间列 stores plain text 'YYYY/MM/DD HH:MM' (verified against
    live rows 2026-07-31) with no timezone, so the clock time is written and
    compared as-is. If two sources quote different timezones for the same
    appointment, the operator must reconcile manually.

CORE LOGIC MAP (for review)
---------------------------
    parse_line()   — input validation (rejects, never guesses)
    plan()         — batch READ-ONLY planner; groups lines that share an ISA
    _plan_row()    — the per-row decision tree (runbook steps 2–4):
                       step 2: unique 3.1 row by 柜号+路线+仓库
                       step 3: 实际板数 fill-if-empty (+W1/W2 warnings)
                       step 4: delivery-plan wiring (4A match/mismatch,
                               4B link / create-trip / create-ISA+trip)
    commit()       — executes approved rows in 5 phases (see banner below)

ENVIRONMENTS
------------
    3.1 / 5.6 resolve through lark.table_id() — LARK_ENV=dev targets the
    user's dev copies. The 5.x delivery-plan tables are SHARED between
    environments; only the 5.x-side link FIELD NAMES differ (config.js
    prodTripLinkFields / devTripLinkFields / devTripIsaFields). Writing a
    duplex link from EITHER side fills both sides automatically.

SAFETY
------
    * plan() performs only reads. commit() is the sole writer.
    * commit() re-plans from scratch and compares each row's action
      signature against what the operator approved — if the Base changed
      in between, the row is SKIPPED with "情况已变化", never blind-written.
    * All writes hold lark.WRITE_LOCK (process-wide serialization) and send
      deterministic client_tokens, so an ambiguous network failure that is
      retried cannot double-create records.
"""

import re
import json
import time
import uuid
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
import lark_client as lark

# Per-row planning fans out across a small worker pool: each row is 2–5
# sequential Feishu reads (~0.3–1 s each), and rows are independent — the
# only shared state is the read-through ISA cache (locked) and the
# precomputed group map (read-only). 4 workers ≈ 3–4× faster real batches.
PLAN_WORKERS = 4

# ---------------------------------------------------------------------------
# Field names (3.1 / 5.6) — identical in prod and the dev copies
# (verified against both schemas 2026-07-31).
# ---------------------------------------------------------------------------
F31 = {
    "awb": "柜号/AWB",            # Text
    "warehouse": "仓库供应商",     # SingleSelect
    "route": "目的地路线",         # SingleSelect
    "actual": "实际板数",          # Text  (never a number column — human notes exist)
    "estimated": "预计板数",       # Formula (read-only)
    "boxes": "箱数",              # Number
    "batch": "客户批次号",         # Text
    "plan_formula": "派送计划",    # Formula (read-only, display only)
}
F56 = {
    "isa": "ISA",                 # Number
    "time": "复制时间列",          # Text 'YYYY/MM/DD HH:MM'
    "dest": "目的地",              # SingleSelect (only ~23 live options!)
    "account": "预约账号",         # SingleSelect
}

# 仓库供应商 -> appointment routing. Mirrors config.js `warehouses` — keep in
# sync. CAL-5505 routes through BESTAR (user-confirmed 2026-07-22).
# TOR-1140 has NO appointment account / plan table: rows for it get the
# search + 实际板数 reconciliation, and the plan step reports 不适用.
WAREHOUSES = {   # dict order == UI button order (mirrors the user's list)
    "TOR-1140": {"account": None,      "plan_table": None},
    "CAL-5505": {"account": "BESTAR", "plan_table": "5.2 BESTAR-CAL"},
    "BESTAR":   {"account": "BESTAR", "plan_table": "5.2 BESTAR-CAL"},
    "WBLL":     {"account": "WBLL",   "plan_table": "5.3 WBLL-EDM"},
    "VAST":     {"account": "元浩",    "plan_table": "5.4 VAST-VAN-01"},
    "GFL":      {"account": "GFL",    "plan_table": "5.5 GFL-VAN-02"},
}

# 5.6-side duplex link to each 5.x table — SAME names in prod and dev 5.6
# (verified 2026-07-31). Used to answer "does this ISA already have a trip?".
LINK_ON_56 = {
    "5.2 BESTAR-CAL": "5.2 出库计划 卡尔加里",
    "5.3 WBLL-EDM": "5.3 出库计划 埃德蒙顿",
    "5.4 VAST-VAN-01": "5.4 出库计划 温哥华",
    "5.5 GFL-VAN-02": "5.5 出库计划-GFL-预约信息",
}

# Thresholds (mirror config.js `thresholds`).
PALLET_DIFF_WARN = 2   # |提供板数 - 预计板数| > 2  => W1/W2 warning
TRIP_PALLET_CAP = 28   # trip total pallets > 28    => W3 warning

DEST_RE = re.compile(r"^(YEG|YYC|YVR)[1-9]$")      # the routes this tool serves
ISA_RE = re.compile(r"^\d{8,15}$")                  # live ISAs are 9–12 digits
AWB_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{4,}$")   # container / AWB, loose sanity
TIME_RE = re.compile(  # 'MM/DD/YYYY HH:MM' or 'YYYY/MM/DD HH:MM', '-' or '/'
    r"^(\d{1,4})[/-](\d{1,2})[/-](\d{1,4})\s+(\d{1,2}):(\d{2})$")
TZ_RE = re.compile(r"^[A-Z]{2,4}$")                 # PDT / MDT / MST ... (kept, not stored)


# ===========================================================================
# 1. PARSING — reject ambiguity, never guess
# ===========================================================================

def norm_time(s):
    """Normalize an appointment time to the 5.6 stored format
    'YYYY/MM/DD HH:MM'. Accepts month-first (07/30/2026 13:00 — the
    operator's paste format) and year-first (2026/07/30 13:00 — the stored
    format), so the same function canonicalizes BOTH sides of a comparison.
    Returns None when unparseable."""
    if not s:
        return None
    m = TIME_RE.match(str(s).strip())
    if not m:
        return None
    a, b, c, hh, mi = m.groups()
    if len(a) == 4:                      # YYYY/MM/DD
        y, mo, d = int(a), int(b), int(c)
    elif len(c) == 4:                    # MM/DD/YYYY
        y, mo, d = int(c), int(a), int(b)
    else:
        return None                      # two-digit years: refuse, don't guess
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= int(hh) <= 23):
        return None
    return f"{y}/{mo:02d}/{d:02d} {int(hh):02d}:{mi}"


def parse_line(raw):
    """Parse ONE pasted line -> dict. On any ambiguity returns
    {'error': <中文原因>} and the caller must NOT process the line.

    Token layout (after collapsing all whitespace runs):
        [0] AWB   [1] 目的地路线   [2] 实际板数   [3] 箱数
        optional: [4] ISA   [5] 日期   [6] 时间   [7] 时区
    """
    out = {"raw": raw}
    # normalize exotic whitespace (NBSP / full-width space) before splitting
    line = raw.replace(" ", " ").replace("　", " ").strip()
    if not line:
        return {"error": "空行"}

    # Excel pastes are tab-separated: an EMPTY cell between two filled ones
    # (e.g. "CSGU6249922\tYVR4\t\t96" — 板数 missing) must be called out
    # explicitly rather than silently collapsing into a shorter token list.
    if "\t" in line:
        cells = [c.strip() for c in line.split("\t")]
        while cells and cells[-1] == "":
            cells.pop()
        if "" in cells:
            return {"error": "存在空列：最后两列必须是 [实际板数] [箱数] 两个数字，"
                             "请补全缺失的数值"}

    toks = line.split()
    if len(toks) < 4:
        return {"error": f"只有 {len(toks)} 列 — 需要 [柜号] [路线] [实际板数] [箱数]"
                         "（可选再加 [ISA] [MM/DD/YYYY HH:MM] [时区]）"}
    if len(toks) in (5, 6) or len(toks) > 8:
        return {"error": f"列数 {len(toks)} 无法解析：带预约信息时应为 "
                         "[柜号] [路线] [板数] [箱数] [ISA] [日期] [时间] [可选时区]"}

    awb, dest, pallets_s, boxes_s = toks[0].upper(), toks[1].upper(), toks[2], toks[3]

    if not AWB_RE.match(awb):
        return {"error": f"柜号「{toks[0]}」格式不像柜号/AWB"}
    if not DEST_RE.match(dest):
        return {"error": f"路线「{toks[1]}」不合法 — 只接受 YEG1-9 / YYC1-9 / YVR1-9"}

    # The two trailing numbers of the basic form are [实际板数, 箱数] — BOTH
    # must be plain positive integers. A ≥9-digit number here almost certainly
    # means the pallet count was omitted and an ISA slid into its place.
    for label, s in (("实际板数", pallets_s), ("箱数", boxes_s)):
        if not re.fullmatch(r"\d+", s):
            return {"error": f"{label}「{s}」不是数字 — 最后两列必须是 "
                             "[实际板数] [箱数] 两个数字"}
    if len(boxes_s) >= 9 or len(pallets_s) >= 9:
        return {"error": "板数/箱数缺失？检测到疑似 ISA 出现在板数/箱数位置，"
                         "请确认格式为 [柜号] [路线] [实际板数] [箱数] [ISA] [时间]"}
    pallets, boxes = int(pallets_s), int(boxes_s)
    if pallets < 1 or pallets > 99:
        return {"error": f"实际板数 {pallets} 超出合理范围 (1-99)"}
    if boxes < 1:
        return {"error": "箱数必须大于 0"}

    isa = time_norm = tz = None
    if len(toks) >= 7:                       # full form with appointment info
        isa_s = toks[4]
        if not ISA_RE.match(isa_s):
            return {"error": f"ISA「{isa_s}」应为 8-15 位数字"}
        isa = int(isa_s)
        time_norm = norm_time(toks[5] + " " + toks[6])
        if not time_norm:
            return {"error": f"预约时间「{toks[5]} {toks[6]}」无法解析 — "
                             "需要 MM/DD/YYYY HH:MM（如 07/30/2026 13:00）"}
        if len(toks) == 8:
            if not TZ_RE.match(toks[7]):
                return {"error": f"时区「{toks[7]}」无法识别（如 PDT / MDT）"}
            tz = toks[7]

    out.update(awb=awb, dest=dest, pallets=pallets, boxes=boxes,
               isa=isa, time=time_norm, tz=tz)
    return out


def parse_batch(text):
    """Split pasted text into parsed lines (skipping blank lines) and refuse
    in-batch duplicates of the same 柜号+路线 — two lines writing one 3.1 row
    would conflict."""
    rows, seen = [], {}
    for i, raw in enumerate((text or "").splitlines()):
        if not raw.strip():
            continue
        p = parse_line(raw)
        p["line_no"] = i + 1
        key = (p.get("awb"), p.get("dest"))
        if "error" not in p:
            if key in seen:
                p = {"raw": raw, "line_no": i + 1,
                     "error": f"与第 {seen[key]} 行重复（同柜号+路线）— 请删去一行"}
            else:
                seen[key] = i + 1
        rows.append(p)
    return rows


# ===========================================================================
# 2. READ HELPERS
# ===========================================================================

def _base():
    return lark.config_values()["base_token"]


def _search(table_id_, conditions, field_names, page_size=20):
    """POST /records/search with AND conditions. READ."""
    payload = {"filter": {"conjunction": "and", "conditions": conditions},
               "automatic_fields": False}
    if field_names:
        payload["field_names"] = field_names
    return lark._api(
        "POST",
        f"/open-apis/bitable/v1/apps/{_base()}/tables/{table_id_}/records/search",
        payload=payload, query={"page_size": page_size},
    ).get("items", [])


def _batch_get(table_id_, record_ids, field_names=None):
    """POST /records/batch_get -> {record_id: fields}. READ."""
    if not record_ids:
        return {}
    payload = {"record_ids": list(record_ids), "automatic_fields": False}
    data = lark._api(
        "POST",
        f"/open-apis/bitable/v1/apps/{_base()}/tables/{table_id_}/records/batch_get",
        payload=payload)
    out = {}
    for rec in data.get("records", []):
        f = rec.get("fields") or {}
        if field_names:
            f = {k: v for k, v in f.items() if k in field_names}
        out[rec["record_id"]] = f
    return out


def _find_31(t31, awb, route, warehouse, plan_link_31=None):
    """3.1 rows for 柜号+路线+仓库. Exact 柜号 first; fall back to a contains
    search tightened to prefix+1char matches (拆柜后缀 …A/…B), same rule as
    upload_56._find_31. READ."""
    fields = [F31["awb"], F31["actual"], F31["estimated"], F31["boxes"],
              F31["batch"], F31["plan_formula"]]
    if plan_link_31:
        fields.append(plan_link_31)              # env's 3.1-side plan link field

    def go(op, val):
        return _search(t31, [
            {"field_name": F31["awb"], "operator": op, "value": [val]},
            {"field_name": F31["warehouse"], "operator": "is", "value": [warehouse]},
            {"field_name": F31["route"], "operator": "is", "value": [route]},
        ], fields)

    hits = go("is", awb)
    if not hits:
        # 拆柜/截断后缀 = 恰好多 1 个字符（…A/…B 或补回的第 8 位数字）。收紧到
        # 该模式，避免把「MATU…/ZCSU…」这类合并柜号行误当成本柜号的行。
        # (Same rule as upload_56._find_31.)
        pat = re.compile(re.escape(awb) + r"[A-Z0-9]")
        hits = [h for h in go("contains", awb)
                if pat.fullmatch(lark.flat_text((h.get("fields") or {}).get(F31["awb"])))]
    return hits


def _env_wiring(plan_table):
    """The complete duplex-link wiring for one warehouse's plan table in the
    CURRENT environment — the ONLY place the dev/prod field-name split lives.

        enabled       — can this env write trips at all? In dev, ONLY plan
                        tables with a dev copy (config.js devTables) qualify;
                        dev mode NEVER writes into the shared prod 5.x tables.
        plan_link_31  — field ON 3.1 linking to the trip table
        link_on_56    — field ON 5.6 linking to the trip table
        isa_field     — field ON the trip table linking to 5.6
        inv_field     — field ON the trip table linking to 3.1
    """
    if not plan_table:
        return {"enabled": False}
    cfg = lark.config_values()
    if lark.env() == "dev":
        if plan_table not in cfg["dev_tables"]:
            return {"enabled": False}     # no dev copy -> trips OFF in dev
        return {"enabled": True,
                "plan_link_31": cfg["dev_plan_link_fields_31"][plan_table],
                "link_on_56": cfg["dev_link_on_56"][plan_table],
                "isa_field": cfg["dev_trip_isa_fields"][plan_table],
                "inv_field": cfg["dev_trip_link_fields"][plan_table]}
    return {"enabled": True,
            "plan_link_31": plan_table,   # prod column is named like the label
            "link_on_56": LINK_ON_56[plan_table],
            "isa_field": "预约信息",
            "inv_field": cfg["prod_trip_link_fields"][plan_table]}


def _trip_total_pallets(t31, inv_ids, extra_by_rid=None):
    """Total pallets currently on a trip = Σ over its linked 3.1 rows of
    (实际板数 if numeric else 预计板数 else 0).

    Computed instead of reading the 5.x rollup column because (a) the rollup
    only sums the PROD link column so it is blind in dev mode, and (b) its
    name differs per table (出库板数 / 出库板数-元浩 / 出库板数-GFL). One
    computed code path is verifiable in both environments.

    `extra_by_rid` lets the caller substitute the ABOUT-TO-BE-WRITTEN 实际板数
    for rows whose fill action is part of this same batch."""
    if not inv_ids:
        return 0.0
    rows = _batch_get(t31, inv_ids, [F31["actual"], F31["estimated"]])
    total = 0.0
    for rid, f in rows.items():
        if extra_by_rid and rid in extra_by_rid:
            total += extra_by_rid[rid]
            continue
        v = lark.num_of(f.get(F31["actual"]))
        if v is None:
            v = lark.num_of(f.get(F31["estimated"])) or 0.0
        total += v
    return total


def _sig(actions):
    """Stable signature of a row's planned actions. commit() recomputes it
    and refuses rows whose plan changed after the operator reviewed them."""
    return hashlib.sha1(
        json.dumps(actions, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


# ===========================================================================
# 3. PLAN — the READ-ONLY decision pass
# ===========================================================================

def plan(warehouse, text, progress=None):
    """Parse + decide the whole batch. Returns
    {'env', 'warehouse', 'account', 'plan_table', 'rows': [...], 'summary'}.
    Performs ONLY reads. `progress(stage, done, total, current)` (optional)
    is called as rows finish — the webapp shows it live."""
    tick = progress or (lambda **_: None)
    wh = WAREHOUSES.get((warehouse or "").strip())
    if wh is None:
        raise lark.LarkError(f"未知仓库供应商「{warehouse}」")
    warehouse = (warehouse or "").strip()
    tick(stage="读取 5.6 字段选项", current="")
    t31, t56 = lark.table_id("3.1"), lark.table_id("5.6")

    # Live single-select options on the env's 5.6 — creates must never invent
    # a new 目的地/预约账号 option.
    fm56 = lark.field_meta(t56)["by_name"]
    valid_dests = set(((fm56.get(F56["dest"]) or {}).get("options") or {}).values())
    valid_accounts = set(((fm56.get(F56["account"]) or {}).get("options") or {}).values())

    parsed = parse_batch(text)

    # ---- ISA groups -------------------------------------------------------
    # Lines sharing an ISA are ONE appointment covering several containers:
    # the group must agree on the time, materialize at most ONE new 5.6
    # record and ONE new trip, and every member links to the SAME trip.
    groups = {}
    for p in parsed:
        if "error" in p or p.get("isa") is None:
            continue
        g = groups.setdefault(p["isa"], {"lines": [], "times": set(), "dests": set(),
                                         "pallet_sum": 0})
        g["lines"].append(p["line_no"])
        g["times"].add(p["time"])
        g["dests"].add(p["dest"])
        g["pallet_sum"] += p["pallets"]     # a group shares ONE trip -> cap check

    ctx = {
        "warehouse": warehouse, "wh": wh, "t31": t31, "t56": t56,
        "valid_dests": valid_dests, "valid_accounts": valid_accounts,
        "wiring": _env_wiring(wh.get("plan_table")),   # env duplex-link names
        "groups": groups,
        # per-ISA resolution cache so N lines of one group plan consistently;
        # guarded by a lock because rows plan in parallel
        "isa_cache": {},
        "isa_lock": threading.Lock(),
    }

    # Plan rows CONCURRENTLY (independent reads), keeping input order in the
    # output and streaming progress as each row completes.
    done_count = {"n": 0}
    count_lock = threading.Lock()
    tick(stage="逐行核对", done=0, total=len(parsed))

    def plan_one(p):
        r = _plan_row(ctx, p)
        # The signature freezes what the operator approves; commit() recomputes
        # it from fresh reads and refuses rows whose situation changed.
        r["sig"] = _sig(r["actions"])
        with count_lock:
            done_count["n"] += 1
            awb = (r.get("parsed") or {}).get("awb") or ""
            tick(done=done_count["n"], total=len(parsed),
                 current=f"第{r['line_no']}行 {awb}")
        return r

    if len(parsed) <= 1:
        rows = [plan_one(p) for p in parsed]
    else:
        with ThreadPoolExecutor(max_workers=PLAN_WORKERS) as pool:
            rows = list(pool.map(plan_one, parsed))   # map preserves order

    summary = {
        "lines": len(rows),
        "parse_errors": sum(1 for r in rows if r.get("parse_error")),
        "match_errors": sum(1 for r in rows if r.get("match_error")),
        "actionable": sum(1 for r in rows if r["actions"] and not r["blockers"]),
        "warnings": sum(len(r["warnings"]) for r in rows),
        "blocked": sum(1 for r in rows if r["blockers"]),
    }
    return {"env": lark.env(), "warehouse": warehouse,
            "account": wh.get("account"), "plan_table": wh.get("plan_table"),
            "rows": rows, "summary": summary}


def _resolve_isa(ctx, isa):
    """Find the ISA in the env 5.6 (cached per batch). Returns
    {'rec_id', 'fields', 'multi': bool} or None when the ISA doesn't exist.

    When several 5.6 rows share the ISA (the live table has a 重复预约 flag
    for a reason), prefer — in order — a row already linked to THIS
    warehouse's plan table, then a row whose 预约账号 matches, then the
    first; the caller surfaces a warning either way.

    Serialized under ctx['isa_lock']: rows plan in parallel, and all lines of
    one ISA group MUST resolve to the same cached record."""
    with ctx["isa_lock"]:
        if isa in ctx["isa_cache"]:
            return ctx["isa_cache"][isa]
        link56 = ctx["wiring"].get("link_on_56")     # env's 5.6-side trip link
        fields = [F56["isa"], F56["time"], F56["dest"], F56["account"]]
        if link56:
            fields.append(link56)
        hits = _search(ctx["t56"], [
            {"field_name": F56["isa"], "operator": "is", "value": [str(isa)]},
        ], fields)
        res = None
        if hits:
            best = None
            if link56:
                best = next((h for h in hits
                             if lark.link_ids((h.get("fields") or {}).get(link56))), None)
            if best is None:
                best = next((h for h in hits if (h.get("fields") or {})
                             .get(F56["account"]) == ctx["wh"]["account"]), None)
            if best is None:
                best = hits[0]
            res = {"rec_id": best["record_id"], "fields": best.get("fields") or {},
                   "multi": len(hits) > 1}
        ctx["isa_cache"][isa] = res
        return res


def _plan_row(ctx, p):
    """Decision tree for ONE parsed line — runbook steps 2/3/4.

    Returns a row dict:
        actions[]  — typed writes commit() will perform, in order
        warnings[] — W1/W2/W3 + informational flags (NEVER stop the row)
        blockers[] — reasons the row cannot be executed (stop THIS row only)
    Action types:
        fill_pallets     {record_id, value}
        update_isa_time  {isa_record_id, isa, time}         (4A mismatch)
        link_trip        {record_id, plan_link_field, trip_id|'@group'}
        set_trip_isa     {trip_id, isa_record_id|'@group'}  (edge: trip w/o ISA)
        create_isa       {group_isa}   -> one 5.6 create per ISA GROUP
        create_trip      {group_isa}   -> one 5.x create per ISA GROUP
    '@group' placeholders resolve at commit time to the group's materialized
    record ids (see commit() phases)."""
    row = {"line_no": p.get("line_no"), "raw": p.get("raw"),
           "parsed": None, "parse_error": None, "match": None,
           "match_error": None, "pallet": {}, "boxes": {}, "plan": {},
           "actions": [], "warnings": [], "blockers": [], "notes": []}

    if "error" in p:
        row["parse_error"] = p["error"]
        return row
    row["parsed"] = {k: p.get(k) for k in
                     ("awb", "dest", "pallets", "boxes", "isa", "time", "tz")}

    W = row["warnings"].append
    N = row["notes"].append
    wh, warehouse = ctx["wh"], ctx["warehouse"]

    # ---- Step 2 — the unique 3.1 row ------------------------------------
    try:
        hits = _find_31(ctx["t31"], p["awb"], p["dest"], warehouse,
                        ctx["wiring"].get("plan_link_31"))
    except lark.LarkError as e:
        row["match_error"] = f"3.1 查询失败：{e}"
        return row
    if not hits:
        row["match_error"] = "3.1 无匹配行（柜号+路线+仓库）"
        return row
    if len(hits) > 1:
        awbs = [lark.flat_text((h.get("fields") or {}).get(F31["awb"])) for h in hits]
        row["match_error"] = f"3.1 匹配到 {len(hits)} 行（{' / '.join(awbs)}）— 请人工处理"
        return row

    rec = hits[0]
    f = rec.get("fields") or {}
    rid = rec["record_id"]
    est = lark.num_of(f.get(F31["estimated"]))
    existing = lark.flat_text(f.get(F31["actual"]))
    boxes31 = lark.num_of(f.get(F31["boxes"]))
    row["match"] = {
        "record_id": rid,
        "awb": lark.flat_text(f.get(F31["awb"])),
        "batch": lark.flat_text(f.get(F31["batch"])),
        "estimated": est,
        "actual_existing": existing or None,
        "boxes": boxes31,
        "plan_formula": lark.flat_text(f.get(F31["plan_formula"])) or None,
    }

    # ---- Step 3 — 实际板数 reconciliation --------------------------------
    # Empty  -> fill with the provided count (W1 if it strays >2 from 预计).
    # Filled -> NEVER overwrite (not even numerically-different values);
    #           W2 if the provided count strays >2 from 预计.
    provided = p["pallets"]
    diff = abs(provided - est) if est is not None else None
    if not existing:
        row["pallet"] = {"do": "fill", "value": str(provided), "estimated": est}
        row["actions"].append({"type": "fill_pallets", "record_id": rid,
                               "value": str(provided)})
        if diff is not None and diff > PALLET_DIFF_WARN:
            W(f"W1 板数差异：提供 {provided} vs 预计 {_n(est)}（差 {_n(diff)} > "
              f"{PALLET_DIFF_WARN}）— 已按提供值填入，请人工复核")
        elif est is None:
            N("预计板数为空，未校验差异")
    else:
        row["pallet"] = {"do": "keep", "existing": existing, "estimated": est}
        if diff is not None and diff > PALLET_DIFF_WARN:
            W(f"W2 实际板数已有值「{existing}」（未覆盖）；提供 {provided} vs "
              f"预计 {_n(est)} 差 {_n(diff)} > {PALLET_DIFF_WARN} — 请人工复核")
        else:
            N(f"实际板数已有值「{existing}」，未覆盖")

    # 箱数 is display-only verification — the tool never writes it.
    if boxes31 is not None and int(boxes31) != p["boxes"]:
        W(f"箱数不一致：提供 {p['boxes']} vs 3.1 记录 {_n(boxes31)}")
    row["boxes"] = {"provided": p["boxes"], "in_31": boxes31}

    # ---- Step 4 — delivery-plan wiring -----------------------------------
    wiring = ctx["wiring"]
    if not wh.get("plan_table"):
        row["plan"] = {"status": "no_plan_table"}
        N(f"仓库 {warehouse} 未配置出库计划表 — 跳过预约流程")
        return row
    if not wiring["enabled"]:
        # dev env without a dev copy of this warehouse's trip table: the
        # tool must NOT write into the shared prod 5.x tables from dev, so
        # the whole appointment/trip step is disabled (pallet checks above
        # still ran). Duplicate the 5.x table + extend config.js to enable.
        row["plan"] = {"status": "no_dev_plan_table"}
        N(f"DEV 环境无「{wh['plan_table']}」副本表 — 预约/行程操作已停用（仅板数核对）")
        return row

    plan_table = wh["plan_table"]
    plan_link_field = wiring["plan_link_31"]  # env's 3.1-side link column
    isa_field, inv_field = wiring["isa_field"], wiring["inv_field"]
    t5x = lark.table_id(plan_table)           # dev resolves to the dev copy

    linked_trips = lark.link_ids(f.get(plan_link_field))
    has_plan = bool(linked_trips)

    # Group coordination (one appointment covering several containers).
    grp = ctx["groups"].get(p["isa"]) if p.get("isa") is not None else None
    if grp:
        if len(grp["times"]) > 1:
            row["blockers"].append("同一 ISA 在本批内时间不一致 — 请统一后重试")
        if len(grp["dests"]) > 1:
            W(f"同一 ISA 在本批内目的地不一致（{' / '.join(sorted(grp['dests']))}）")

    if has_plan:
        # ---- 4A: the row already has a trip -> verify ISA/time -----------
        if len(linked_trips) > 1:
            W(f"该行挂了 {len(linked_trips)} 个出库计划，按第一个核对")
        trip_id = linked_trips[0]
        trip = _batch_get(t5x, [trip_id], [isa_field, inv_field]).get(trip_id, {})
        trip_inv_ids = lark.link_ids(trip.get(inv_field))
        isa_ids = lark.link_ids(trip.get(isa_field))

        total = _trip_total_pallets(
            ctx["t31"], trip_inv_ids,
            # if we're filling 实际板数 in this same batch, count the NEW value
            extra_by_rid={rid: float(provided)} if row["pallet"].get("do") == "fill"
            and rid in trip_inv_ids else None)
        if rid not in trip_inv_ids:
            # 3.1->5.x and 5.x->3.1 are the SAME duplex link, so this should
            # be impossible; surface it rather than "fixing" silently.
            W("数据异常：3.1 行挂了行程，但行程的库存关联里没有这一行")
            total += provided
        if total > TRIP_PALLET_CAP:
            W(f"W3 行程总板数 {_n(total)} 超过 {TRIP_PALLET_CAP} 板上限")

        if not isa_ids:
            # Edge: a trip exists but has no appointment linked.
            row["plan"] = {"status": "has_plan_no_isa", "trip_id": trip_id,
                           "trip_total": total}
            if p.get("isa") is not None:
                ex = _resolve_isa(ctx, p["isa"])
                if ex:
                    if ex["multi"]:
                        W(f"5.6 中 ISA {p['isa']} 存在多条，按最匹配的一条处理")
                    row["actions"].append({"type": "set_trip_isa", "trip_id": trip_id,
                                           "isa_record_id": ex["rec_id"]})
                    _check_time_update(row, ctx, ex, p, W)
                else:
                    _plan_create_isa(row, ctx, p, W)
                    row["actions"].append({"type": "set_trip_isa", "trip_id": trip_id,
                                           "isa_record_id": "@group"})
            else:
                N("行程未关联预约（未提供 ISA，无法核对）")
            return row

        isa_rec_id = isa_ids[0]
        isa_rec = _batch_get(ctx["t56"], [isa_rec_id],
                             [F56["isa"], F56["time"], F56["dest"], F56["account"]]
                             ).get(isa_rec_id, {})
        cur_isa = lark.num_of(isa_rec.get(F56["isa"]))
        cur_time = norm_time(lark.flat_text(isa_rec.get(F56["time"])))
        row["plan"] = {"status": "has_plan", "trip_id": trip_id,
                       "current_isa": int(cur_isa) if cur_isa is not None else None,
                       "current_time": cur_time, "trip_total": total,
                       "isa_record_id": isa_rec_id}

        if p.get("isa") is None:
            N("已有派送计划（未提供 ISA，仅展示）")
            return row

        same_isa = cur_isa is not None and int(cur_isa) == p["isa"]
        same_time = cur_time == p["time"]
        if same_isa and same_time:
            row["plan"]["status"] = "has_plan_match"
            N("派送计划的 ISA+时间与提供值一致 — 无需改动")
        else:
            # 4A mismatch: EDIT THE LINKED 5.6 RECORD to the session values.
            # ⚠ This updates the appointment itself, so it propagates to every
            #   other shipment on the same trip — that is the intended
            #   behaviour for a rescheduled appointment (per runbook 4A).
            row["plan"]["status"] = "has_plan_mismatch"
            row["actions"].append({"type": "update_isa_time",
                                   "isa_record_id": isa_rec_id,
                                   "isa": p["isa"], "time": p["time"]})
            W(f"派送计划不一致（现 ISA={_n(cur_isa)} 时间={cur_time or '空'} → "
              f"新 ISA={p['isa']} 时间={p['time']}）— 更新将影响该行程下所有货件")
        return row

    # ---- 4B: no plan on the row ------------------------------------------
    if p.get("isa") is None:
        row["plan"] = {"status": "no_plan"}
        N("无派送计划（未提供 ISA — 补充 ISA+时间后可一键建立）")
        return row

    ex = _resolve_isa(ctx, p["isa"])
    if ex:
        if ex["multi"]:
            W(f"5.6 中 ISA {p['isa']} 存在多条，按最匹配的一条处理")
        acct = ex["fields"].get(F56["account"])
        if acct and acct != wh["account"]:
            W(f"该 ISA 的预约账号为「{acct}」，与仓库账号「{wh['account']}」不同")
        link56 = wiring["link_on_56"]
        trips56 = lark.link_ids(ex["fields"].get(link56))
        if trips56:
            # -- 4B(i): ISA already has a trip -> link this shipment into it
            trip_id = trips56[0]
            if len(trips56) > 1:
                W(f"该 ISA 关联了 {len(trips56)} 个出库计划，挂靠第一个")
            trip = _batch_get(t5x, [trip_id], [inv_field]).get(trip_id, {})
            trip_inv_ids = lark.link_ids(trip.get(inv_field))
            if rid in trip_inv_ids:
                row["plan"] = {"status": "already_on_trip", "trip_id": trip_id}
                N("该行程已包含本货件 — 无需改动")
            else:
                # Cap check counts the whole ISA group: every group line joins
                # this same trip in this batch (single-line groups sum to just
                # this row's pallets).
                grp_sum = (ctx["groups"].get(p["isa"]) or {}).get("pallet_sum", provided)
                total = _trip_total_pallets(ctx["t31"], trip_inv_ids) + grp_sum
                row["plan"] = {"status": "link_existing", "trip_id": trip_id,
                               "trip_total": total}
                row["actions"].append({"type": "link_trip", "record_id": rid,
                                       "plan_link_field": plan_link_field,
                                       "trip_id": trip_id})
                if total > TRIP_PALLET_CAP:
                    W(f"W3 挂靠后行程总板数约 {_n(total)} 超过 {TRIP_PALLET_CAP} 板上限")
            _check_time_update(row, ctx, ex, p, W)
        else:
            # -- 4B(ii): ISA exists, no trip -> ONE new trip for the group
            row["plan"] = {"status": "create_trip", "isa_record_id": ex["rec_id"]}
            row["actions"].append({"type": "create_trip", "group_isa": p["isa"]})
            row["actions"].append({"type": "link_trip", "record_id": rid,
                                   "plan_link_field": plan_link_field,
                                   "trip_id": "@group"})
            _check_time_update(row, ctx, ex, p, W)
            _warn_group_cap(ctx, p, W)
    else:
        # -- 4B(iii): ISA not in 5.6 -> create appointment + trip + link ----
        _plan_create_isa(row, ctx, p, W)
        if not row["blockers"]:
            row["plan"] = {"status": "create_isa_and_trip"}
            row["actions"].append({"type": "create_trip", "group_isa": p["isa"]})
            row["actions"].append({"type": "link_trip", "record_id": rid,
                                   "plan_link_field": plan_link_field,
                                   "trip_id": "@group"})
            _warn_group_cap(ctx, p, W)
    return row


def _plan_create_isa(row, ctx, p, W):
    """Queue the ISA-group's 5.6 create (validated against LIVE select
    options — a route like YEG3 that is not a 5.6 目的地 option BLOCKS the
    row rather than inventing an option)."""
    wh = ctx["wh"]
    if p["dest"] not in ctx["valid_dests"]:
        row["blockers"].append(
            f"目的地「{p['dest']}」不是 5.6 的现有选项 — 无法创建预约（绝不自动新增选项）")
        return
    if wh["account"] not in ctx["valid_accounts"]:
        row["blockers"].append(f"预约账号「{wh['account']}」不是 5.6 的现有选项")
        return
    row["actions"].append({"type": "create_isa", "group_isa": p["isa"],
                           "fields": {F56["isa"]: p["isa"], F56["dest"]: p["dest"],
                                      F56["account"]: wh["account"],
                                      F56["time"]: p["time"]}})


def _check_time_update(row, ctx, ex, p, W):
    """When an EXISTING 5.6 record serves this line, its stored time may still
    disagree with the pasted time -> same 4A-mismatch treatment (update the
    linked record; ISA number already matches by construction)."""
    cur_time = norm_time(lark.flat_text(ex["fields"].get(F56["time"])))
    if cur_time != p["time"]:
        row["actions"].append({"type": "update_isa_time",
                               "isa_record_id": ex["rec_id"],
                               "isa": p["isa"], "time": p["time"]})
        W(f"预约时间不一致（5.6 现值 {cur_time or '空'} → {p['time']}）— 将更新预约记录")


def _warn_group_cap(ctx, p, W):
    """W3 for a NEW trip: it starts with exactly the ISA group's shipments."""
    grp = ctx["groups"].get(p.get("isa"))
    if grp and grp.get("pallet_sum", 0) > TRIP_PALLET_CAP:
        W(f"W3 新行程总板数 {_n(grp['pallet_sum'])} 超过 {TRIP_PALLET_CAP} 板上限")


def _n(x):
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else ("%g" % x)


# ===========================================================================
# 4. COMMIT — the sole writer
# ===========================================================================

def _ctoken(*_parts):
    """client_token for a write — a FRESH uuid4 per request.

    Two constraints discovered live (2026-07-31):
      * it must be UUID v4 format (code 1254037 otherwise);
      * it must NOT repeat across logically distinct batches — Feishu's
        replay cache outlives the records themselves, so a deterministic
        token replayed after a delete fails with 1254608 ("Same API
        requests are submitted repeatedly").
    So the token only dedupes Feishu-internal/http retries of THIS request.
    Operator-level retry safety comes from commit()'s Phase 0 instead: a
    re-click re-plans from fresh reads, sees whatever the first attempt
    actually created, and plans link/backfill actions rather than creates."""
    return str(uuid.uuid4())


def commit(warehouse, text, approvals, client_env, progress=None):
    """Execute the operator-approved rows. `approvals` = [{line_no, sig}].

    PHASES (order matters — later phases need record ids from earlier ones):
      0. re-plan everything from fresh reads; a row whose action signature no
         longer equals the approved `sig` is SKIPPED ("情况已变化").
      1. batch_create 5.6      — one appointment per approved ISA group
      2. batch_create 5.x      — one trip per approved ISA group, created WITH
                                 its 预约信息 duplex link already set (the 5.6
                                 back-link fills itself)
      2b. batch_update 5.x     — set_trip_isa edge rows (trip existed w/o ISA)
      3. batch_update 3.1      — per row: 实际板数 fill + plan link, ONE update
                                 per record (link写在 3.1 侧 => 5.x 库存信息
                                 back-link fills itself, both environments)
      4. batch_update 5.6      — 4A mismatch ISA/time edits (deduped by record;
                                 conflicting values refuse BOTH sides)
      5. read-back verification — re-read what we wrote and annotate each row
                                 verified=True/False. Nothing is trusted blind.
    Every phase failure is captured per-row; a failed prerequisite stops the
    dependent actions of that row only (never the whole batch).

    `progress` (optional) receives stage/done/total updates throughout —
    including while WAITING for the process-wide write lock, which used to be
    an invisible, indefinite queue (the reported "hang")."""
    if client_env != lark.env():
        raise lark.LarkError(
            f"环境不匹配：页面为 {client_env}，服务端为 {lark.env()} — 请刷新页面")
    tick = progress or (lambda **_: None)

    # Visible lock acquisition: report every second instead of blocking mute.
    waited = 0.0
    while not lark.WRITE_LOCK.acquire(timeout=1.0):
        waited += 1.0
        tick(stage=f"等待另一个写入完成…（已等 {int(waited)} 秒）", current="")
        if waited >= 300:   # 5 min: something is truly wedged — fail loud
            raise lark.LarkError("等待写入锁超过 5 分钟 — 可能有卡住的写入任务，"
                                 "请检查服务端后重试")
    try:
        return _commit_locked(warehouse, text, approvals, tick)
    finally:
        lark.WRITE_LOCK.release()


def _commit_locked(warehouse, text, approvals, tick):
    warehouse = (warehouse or "").strip()

    def replan_tick(**kw):                    # prefix the re-plan's stages
        if kw.get("stage"):
            kw["stage"] = "复检 · " + kw["stage"]
        tick(**kw)

    tick(stage="复检（重新预检所有行）")
    result = plan(warehouse, text, progress=replan_tick)   # Phase 0 — fresh reads
    by_line = {r["line_no"]: r for r in result["rows"]}
    approved = {}
    for a in approvals or []:
        r = by_line.get(a.get("line_no"))
        if r is None:
            continue
        r["approved"] = True
        if r.get("parse_error") or r.get("match_error"):
            r["commit"] = {"done": False, "skipped": "行存在错误，未执行"}
        elif r["blockers"]:
            r["commit"] = {"done": False, "skipped": "存在拦截项，未执行"}
        elif _sig(r["actions"]) != a.get("sig"):
            r["commit"] = {"done": False,
                           "skipped": "情况已变化（与预检时不同）— 请重新查询后再执行"}
        elif not r["actions"]:
            r["commit"] = {"done": True, "skipped": "无需改动"}
        else:
            approved[r["line_no"]] = r
    rows = list(approved.values())

    wh = WAREHOUSES[warehouse]
    plan_table = wh.get("plan_table")
    t31, t56 = lark.table_id("3.1"), lark.table_id("5.6")
    t5x = lark.table_id(plan_table) if plan_table else None
    wiring = _env_wiring(plan_table)
    isa_field = wiring.get("isa_field")   # None when trips are disabled — no
    base = _base()                        # trip actions get planned then anyway

    def api_write(op, path, payload, token_parts):
        return lark._api("POST", path, payload=payload,
                         query={"client_token": _ctoken(op, lark.env(), *token_parts)})

    # ---- Phase 1: one 5.6 create per ISA group ----------------------------
    # Collect create_isa actions; several lines can carry the same group's
    # create — materialize each group exactly once.
    tick(stage="写入 1/5 · 新建预约（5.6）", done=0, total=0, current="")
    group_isa_rec = {}     # isa -> record_id (newly created)
    create_jobs = {}       # isa -> (fields, [rows])
    for r in rows:
        for a in r["actions"]:
            if a["type"] == "create_isa":
                create_jobs.setdefault(a["group_isa"], (a["fields"], []))[1].append(r)
    if create_jobs:
        isas = sorted(create_jobs)
        payload = {"records": [{"fields": create_jobs[i][0]} for i in isas]}
        try:
            made = api_write("c56", f"/open-apis/bitable/v1/apps/{base}/tables/{t56}"
                             "/records/batch_create", payload,
                             [t56, ",".join(map(str, isas))]).get("records", [])
            for isa, rec in zip(isas, made):
                group_isa_rec[isa] = rec.get("record_id")
                for r in create_jobs[isa][1]:
                    r.setdefault("commit", {})["isa_record_id"] = rec.get("record_id")
        except Exception as e:
            for _, rs in create_jobs.values():
                for r in rs:
                    r.setdefault("commit", {})["error"] = f"5.6 创建失败：{e}"

    # ---- Phase 2: one trip create per ISA group ---------------------------
    tick(stage="写入 2/5 · 新建出库计划行程（5.x）")
    group_trip_rec = {}    # isa -> trip record_id
    trip_jobs = {}         # isa -> (isa_record_id, [rows])
    for r in rows:
        if (r.get("commit") or {}).get("error"):
            continue       # its 5.6 create failed — do not build on top of it
        for a in r["actions"]:
            if a["type"] != "create_trip":
                continue
            isa = a["group_isa"]
            isa_rec = (r["plan"].get("isa_record_id")     # 4B(ii): existing 5.6
                       or group_isa_rec.get(isa))         # 4B(iii): just created
            if not isa_rec:
                r.setdefault("commit", {})["error"] = "预约记录缺失，无法创建出库计划"
                continue
            trip_jobs.setdefault(isa, (isa_rec, []))[1].append(r)
    if trip_jobs:
        isas = sorted(trip_jobs)
        payload = {"records": [{"fields": {isa_field: [trip_jobs[i][0]]}} for i in isas]}
        try:
            made = api_write("ctrip", f"/open-apis/bitable/v1/apps/{base}/tables/{t5x}"
                             "/records/batch_create", payload,
                             [t5x, ",".join(map(str, isas))]).get("records", [])
            for isa, rec in zip(isas, made):
                group_trip_rec[isa] = rec.get("record_id")
                for r in trip_jobs[isa][1]:
                    r.setdefault("commit", {})["trip_record_id"] = rec.get("record_id")
        except Exception as e:
            for _, rs in trip_jobs.values():
                for r in rs:
                    r.setdefault("commit", {})["error"] = f"出库计划创建失败：{e}"

    # ---- Phase 2b: attach an ISA to a pre-existing, ISA-less trip ---------
    tick(stage="写入 2b/5 · 行程补挂预约")
    set_jobs = []
    for r in rows:
        if (r.get("commit") or {}).get("error"):
            continue
        for a in r["actions"]:
            if a["type"] != "set_trip_isa":
                continue
            isa_rec = a["isa_record_id"]
            if isa_rec == "@group":
                isa_rec = group_isa_rec.get(r["parsed"]["isa"])
            if not isa_rec:
                r.setdefault("commit", {})["error"] = "预约记录缺失，无法挂到行程"
                continue
            set_jobs.append((r, {"record_id": a["trip_id"],
                                 "fields": {isa_field: [isa_rec]}}))
    if set_jobs:
        payload = {"records": [j for _, j in set_jobs]}
        try:
            api_write("strip", f"/open-apis/bitable/v1/apps/{base}/tables/{t5x}"
                      "/records/batch_update", payload,
                      [t5x, ",".join(j["record_id"] for _, j in set_jobs)])
            for r, _ in set_jobs:
                r.setdefault("commit", {})["trip_isa_set"] = True
        except Exception as e:
            for r, _ in set_jobs:
                r.setdefault("commit", {})["error"] = f"行程挂预约失败：{e}"

    # ---- Phase 3: 3.1 updates (实际板数 fill + plan link, merged) ----------
    tick(stage="写入 3/5 · 更新 3.1（实际板数 + 关联行程）")
    upd31 = {}             # record_id -> (fields, row)
    for r in rows:
        if (r.get("commit") or {}).get("error"):
            continue
        fields = {}
        for a in r["actions"]:
            if a["type"] == "fill_pallets":
                fields[F31["actual"]] = a["value"]
            elif a["type"] == "link_trip":
                trip_id = a["trip_id"]
                if trip_id == "@group":
                    trip_id = group_trip_rec.get(r["parsed"]["isa"])
                if not trip_id:
                    r.setdefault("commit", {})["error"] = "行程记录缺失，无法关联 3.1"
                    fields = None
                    break
                # The row reached here via the no-plan branch, so the link
                # field is empty — writing [trip_id] cannot drop other links.
                fields[a["plan_link_field"]] = [trip_id]
        if fields:
            upd31[r["match"]["record_id"]] = (fields, r)
    if upd31:
        payload = {"records": [{"record_id": rid, "fields": fl}
                               for rid, (fl, _) in sorted(upd31.items())]}
        try:
            api_write("u31", f"/open-apis/bitable/v1/apps/{base}/tables/{t31}"
                      "/records/batch_update", payload, [t31] + sorted(upd31))
            for _, r in upd31.values():
                r.setdefault("commit", {})["updated_31"] = True
        except Exception as e:
            for _, r in upd31.values():
                r.setdefault("commit", {})["error"] = f"3.1 更新失败：{e}"

    # ---- Phase 4: 4A mismatch — edit the LINKED 5.6 record ----------------
    tick(stage="写入 4/5 · 更新预约 ISA/时间（5.6）")
    upd56 = {}             # isa_record_id -> (fields, [rows])
    for r in rows:
        if (r.get("commit") or {}).get("error"):
            continue
        for a in r["actions"]:
            if a["type"] != "update_isa_time":
                continue
            fields = {F56["isa"]: a["isa"], F56["time"]: a["time"]}
            prev = upd56.get(a["isa_record_id"])
            if prev and prev[0] != fields:
                # Two rows demand different values for ONE appointment —
                # refuse both; never write an arbitrary winner.
                for rr in prev[1] + [r]:
                    rr.setdefault("commit", {})["error"] = \
                        "同批对同一预约给出不同 ISA/时间，均未更新"
                upd56.pop(a["isa_record_id"], None)
            elif prev:
                prev[1].append(r)
            else:
                upd56[a["isa_record_id"]] = (fields, [r])
    if upd56:
        payload = {"records": [{"record_id": rid, "fields": fl}
                               for rid, (fl, _) in sorted(upd56.items())]}
        try:
            api_write("u56", f"/open-apis/bitable/v1/apps/{base}/tables/{t56}"
                      "/records/batch_update", payload, [t56] + sorted(upd56))
            for _, rs in upd56.values():
                for r in rs:
                    r.setdefault("commit", {})["updated_56"] = True
        except Exception as e:
            for _, rs in upd56.values():
                for r in rs:
                    r.setdefault("commit", {})["error"] = f"5.6 更新失败：{e}"

    # ---- Phase 5: read-back verification ----------------------------------
    tick(stage="核实 5/5 · 回读校验写入结果")
    _verify(rows, t31, t56, group_trip_rec)
    tick(stage="完成", current="")

    # Roll up the end-of-run warning list the operator asked for.
    all_warnings = []
    for r in result["rows"]:
        for w in r["warnings"]:
            all_warnings.append(f"第{r['line_no']}行 {r['parsed']['awb'] if r.get('parsed') else ''}: {w}")
    result["warnings_summary"] = all_warnings
    result["committed"] = True
    return result


def _verify(rows, t31, t56, group_trip_rec):
    """Re-read the records we just wrote and confirm every value landed.
    Marks each committed row verified=True/False with details — the UI shows
    this, and the integration tests assert on it."""
    rids31 = [r["match"]["record_id"] for r in rows
              if r.get("commit", {}).get("updated_31") and r.get("match")]
    fields31 = {}
    if rids31:
        # verify exactly the columns the actions wrote — the plan-link column
        # name is env-specific, so take it from the actions themselves
        link_cols = {a["plan_link_field"] for r in rows for a in r["actions"]
                     if a["type"] == "link_trip"}
        fields31 = _batch_get(t31, rids31, [F31["actual"], *link_cols])
    rids56 = set()
    for r in rows:
        c = r.get("commit") or {}
        if c.get("updated_56"):
            for a in r["actions"]:
                if a["type"] == "update_isa_time":
                    rids56.add(a["isa_record_id"])
        if c.get("isa_record_id"):
            rids56.add(c["isa_record_id"])
    fields56 = _batch_get(t56, list(rids56), [F56["isa"], F56["time"]]) if rids56 else {}

    for r in rows:
        c = r.get("commit") or {}
        if c.get("error") or c.get("skipped"):
            continue
        checks = []
        for a in r["actions"]:
            if a["type"] == "fill_pallets":
                got = lark.flat_text(fields31.get(a["record_id"], {}).get(F31["actual"]))
                checks.append(("实际板数", got == a["value"], got))
            elif a["type"] == "link_trip":
                want = a["trip_id"]
                if want == "@group":
                    want = group_trip_rec.get(r["parsed"]["isa"])
                got = lark.link_ids(fields31.get(a["record_id"], {})
                                    .get(a["plan_link_field"]))
                checks.append(("派送计划关联", want in got, ",".join(got) or "空"))
            elif a["type"] == "update_isa_time":
                f56 = fields56.get(a["isa_record_id"], {})
                got_isa = lark.num_of(f56.get(F56["isa"]))
                got_time = norm_time(lark.flat_text(f56.get(F56["time"])))
                ok = got_isa is not None and int(got_isa) == a["isa"] \
                    and got_time == a["time"]
                checks.append(("预约ISA/时间", ok, f"{_n(got_isa)} {got_time or '空'}"))
            elif a["type"] == "create_isa":
                rec_id = c.get("isa_record_id")
                f56 = fields56.get(rec_id, {}) if rec_id else {}
                got_isa = lark.num_of(f56.get(F56["isa"]))
                checks.append(("新建预约", got_isa is not None
                               and int(got_isa) == a["group_isa"], _n(got_isa)))
            elif a["type"] == "create_trip":
                checks.append(("新建行程", bool(c.get("trip_record_id")),
                               c.get("trip_record_id") or "无"))
            elif a["type"] == "set_trip_isa":
                checks.append(("行程挂预约", bool(c.get("trip_isa_set")), ""))
        c["done"] = True
        c["verified"] = all(ok for _, ok, _ in checks) if checks else True
        c["checks"] = [{"what": w, "ok": ok, "got": g} for w, ok, g in checks]
        r["commit"] = c
