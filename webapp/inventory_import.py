# -*- coding: utf-8 -*-
"""
inventory_import.py — 库存导入（收货派送计划 Excel → 3.1 新建记录）
================================================================================

Webapp port of the validated CLI flow (src/workflow/create-inventory-records.js,
first production run 2026-07-22, container BEAU6279991) with parsing logic
originally extracted from West Pipeline's pod_generator.py:

    upload 收货派送计划 → aggregate 箱数/重量/体积 per 目的地路线 →
    READ-ONLY plan (guards + preview) → operator ticks rows → commit
    (batch_create with client_token) → read-back verify via batch_get.

TABLE OWNERSHIP: this tab only CREATES 3.1 records. It never updates or
deletes anything — ②计划同步 stays the only updater of existing 3.1 rows.

GUARDS (each surfaces as a per-row block or plan-level warning; the operator
decides — the tool never overrides on its own):
  * 仓库供应商 must be a LIVE select option on 3.1 (plan aborts otherwise).
  * A destination that is not a live 目的地路线 option is BLOCKED — creating a
    record with a new value would silently add a select option.
  * An existing 3.1 record with the same 柜号 + 目的地路线 marks that row
    'exists' (skipped); same-柜号 records on other routes become a warning.
  * UPS/courier-looking plan rows (keyword or 1Z tracking code) are included
    in the sums but reported loudly.

Read-back verification uses records/batch_get by record_id — immune to the
measured ~5.5 s Bitable search-index lag (see appointment_create.py).
"""

import io
import re
import base64
import uuid
import zipfile
import xml.etree.ElementTree as ET

import lark_client as lark
import file_parse
from file_parse import as_text
from appointment_sync import F31, _search, _sig, _base

# 3.1 columns written by the import (F31 lacks the two weight/volume fields
# because the sync flow never touches them).
F31I = dict(F31, weight="重量", volume="体积")

# ---------------------------------------------------------------------------
# Column aliases — ported from src/workflow/parse-plan.js (itself extracted
# from awb-batch-processor/src/pod_generator.py). Uppercased at build time.
# ---------------------------------------------------------------------------
COLUMN_ALIASES = {
    "派送目的地": ["派送目的地", "收件人邮编*", "仓库代码"],
    "重量": ["重量", "实际重量", "实际重量(KG)"],
    "箱数/件数": ["箱数/件数", "件数*", "件数", "箱数"],
    "体积": ["体积", "材积", "立方", "CBM"],
    "FBA NO.": ["FBA NO.", "FBA", "AMAZON REFERENCE ID*", "扩展单号"],
    "PO#": ["PO#", "PO", "REFERENCE ID"],
    "板数": ["板数", "托盘数", "托盘", "PLTS", "PALLETS"],
    "派送方式": ["派送方式", "METHOD"],
}
ESSENTIAL_COLUMNS = ("派送目的地", "箱数/件数")

UPS_KEYWORDS = ("UPS", "快递", "COURIER", "EXPRESS")
PREFERRED_SHEET_KEYWORDS = ("加西", "WEST", "卡派", "卡车", "LTL", "派送", "清单",
                            "PLAN", "UNLOADING")

# Merged quantity columns hold the GROUP total in the top cell; sub-rows get 0.
QUANTITY_CANONICALS = {"箱数/件数", "重量", "板数", "体积"}
QUANTITY_RAW_HINTS = ("体积", "材积")

RE_UPS_TRACKING = re.compile(r"^1Z[0-9A-Z]{8,}$")
RE_UPS_ANYWHERE = re.compile(r"\b1Z[0-9A-Z]{12,}\b")

MAX_HEADER_SCAN = 20


def _lookup():
    out = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            out[a.strip().upper()] = canonical
    return out


def normalize_dest(value):
    """'ca-yvr4 ' -> 'YVR4' (matches pod_generator.normalize_destination)."""
    d = as_text(value).strip().upper()
    return d[3:] if d.startswith("CA-") else d


# ---------------------------------------------------------------------------
# Merged-range extraction (stdlib zip+XML; independent of which reader
# produced the row matrices, so it works with or without openpyxl).
# Returns {sheet_name: [(r1, c1, r2, c2), ...]} — all 0-based, inclusive.
# ---------------------------------------------------------------------------

def _q(tag):
    return "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}" + tag


_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _ref_rc(ref):
    m = _CELL_REF.match(ref or "")
    if not m:
        return 0, 0
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)) - 1, col - 1


def read_merges(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {}
    names = set(z.namelist())
    rel_map = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            rel_map[rel.get("Id")] = rel.get("Target")
    merges = {}
    if "xl/workbook.xml" not in names:
        return {}
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sh in wb.iter(_q("sheet")):
        target = (rel_map.get(sh.get(rid)) or "").lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        if path not in names:
            continue
        out = []
        for mc in ET.fromstring(z.read(path)).iter(_q("mergeCell")):
            ref = mc.get("ref") or ""
            if ":" not in ref:
                continue
            a, b = ref.split(":", 1)
            r1, c1 = _ref_rc(a)
            r2, c2 = _ref_rc(b)
            out.append((r1, c1, r2, c2))
        merges[sh.get("name") or "sheet"] = out
    return merges


# ---------------------------------------------------------------------------
# Sheet / header detection (port of parse-plan.js)
# ---------------------------------------------------------------------------

def scan_header_row(rows, lookup=None):
    """(header_row_index, matches) — row in the first 20 with most alias hits."""
    lookup = lookup or _lookup()
    best_row, best = 0, 0
    for i, row in enumerate(rows[:MAX_HEADER_SCAN]):
        vals = {as_text(c).strip().upper() for c in (row or [])}
        hits = sum(1 for a in lookup if a in vals)
        if hits > best:
            best_row, best = i, hits
    return best_row, best


def select_sheet(sheets):
    """Pick the data sheet: skip UPS-named, need >=2 alias hits, prefer
    加西/WEST/卡派… names, then strongest header. -> (sheet_dict, header_row)."""
    lookup = _lookup()
    candidates = []
    for order, sheet in enumerate(sheets):
        name_u = str(sheet.get("name", "")).upper()
        if any(k in name_u for k in UPS_KEYWORDS):
            continue
        hdr, hits = scan_header_row(sheet.get("rows") or [], lookup)
        if hits < 2:
            continue
        rank = len(PREFERRED_SHEET_KEYWORDS)
        for i, kw in enumerate(PREFERRED_SHEET_KEYWORDS):
            if kw in name_u:
                rank = i
                break
        candidates.append((rank, -hits, order, sheet, hdr))
    if not candidates:
        return None, None
    candidates.sort(key=lambda t: t[:3])
    _, _, _, sheet, hdr = candidates[0]
    return sheet, hdr


def fill_merges(rows, merges, header_row):
    """Expand single-column vertical merges below the header row.
    Quantity columns: sub-rows -> 0 (top cell holds the group total).
    Identity columns: top value copied down (sub-rows keep their destination).
    Mutates and returns rows."""
    if not merges:
        return rows
    lookup = _lookup()
    headers = rows[header_row] if header_row < len(rows) else []
    for (r1, c1, r2, c2) in merges:
        if c1 != c2 or r1 == r2 or r1 <= header_row:
            continue
        raw = as_text(headers[c1]).strip() if c1 < len(headers) else ""
        canonical = lookup.get(raw.upper(), raw)
        quantity = (canonical in QUANTITY_CANONICALS
                    or any(k in raw for k in QUANTITY_RAW_HINTS))
        top = rows[r1][c1] if r1 < len(rows) and c1 < len(rows[r1]) else None
        for r in range(r1 + 1, min(r2, len(rows) - 1) + 1):
            row = rows[r]
            while len(row) <= c1:            # ragged stdlib rows
                row.append("")
            if quantity:
                row[c1] = 0
            elif as_text(row[c1]) == "":
                row[c1] = top
    return rows


def is_ups_row(rec):
    """UPS keyword / bare tracking number in 派送方式|派送目的地, or a 1Z code
    anywhere in the row."""
    for col in ("派送方式", "派送目的地"):
        v = as_text(rec.get(col)).strip().upper()
        if v and (any(k in v for k in UPS_KEYWORDS) or RE_UPS_TRACKING.match(v)):
            return True
    return any(isinstance(v, str) and RE_UPS_ANYWHERE.search(v.upper())
               for v in rec.values())


def load_plan(sheets, merges):
    """-> {'sheet', 'header_row', 'records'} with canonical column names;
    rows without a destination (汇总/footer) dropped. Raises ValueError."""
    sheet, hdr = select_sheet(sheets)
    if sheet is None:
        raise ValueError("未能识别表头 — 没有工作表包含可识别的列名"
                         "（需要 派送目的地/仓库代码 与 件数/箱数 等）")
    rows = fill_merges(sheet.get("rows") or [], merges.get(sheet["name"]) or [], hdr)
    lookup = _lookup()
    headers = [as_text(h).strip() for h in (rows[hdr] or [])]
    canon = [lookup.get(h.upper(), h) for h in headers]
    seen, use = set(), []
    for c in canon:
        use.append(bool(c) and c not in seen)
        seen.add(c)
    for need in ESSENTIAL_COLUMNS:
        if need not in canon:
            raise ValueError(f"缺少必要列：{need}")

    records = []
    for r in range(hdr + 1, len(rows)):
        row = rows[r] or []
        rec = {}
        for c, name in enumerate(canon):
            if use[c] and c < len(row) and as_text(row[c]) != "":
                rec[name] = row[c]
        if as_text(rec.get("派送目的地")).strip():
            records.append(rec)
    return {"sheet": sheet["name"], "header_row": hdr, "records": records}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def aggregate(records):
    """Group by normalized destination; sum 箱数/重量/体积 (2-decimal rounds)."""
    aggs, totals = {}, {"boxes": 0, "weight": 0.0, "volume": 0.0, "rows": 0}
    ups = sum(1 for r in records if is_ups_row(r))
    for rec in records:
        d = normalize_dest(rec["派送目的地"])
        a = aggs.setdefault(d, {"boxes": 0, "weight": 0.0, "volume": 0.0,
                                "rows": 0, "ups_rows": 0})
        boxes = _num(rec.get("箱数/件数"))
        a["boxes"] += int(boxes) if boxes == int(boxes) else boxes
        a["weight"] += _num(rec.get("重量"))
        a["volume"] += _num(rec.get("体积"))
        a["rows"] += 1
        if is_ups_row(rec):
            a["ups_rows"] += 1
        totals["boxes"] += int(boxes) if boxes == int(boxes) else boxes
        totals["weight"] += _num(rec.get("重量"))
        totals["volume"] += _num(rec.get("体积"))
        totals["rows"] += 1
    for a in aggs.values():
        a["weight"] = round(a["weight"], 2)
        a["volume"] = round(a["volume"], 2)
    totals["weight"] = round(totals["weight"], 2)
    totals["volume"] = round(totals["volume"], 2)
    return aggs, totals, ups


# ===========================================================================
# PLAN — parse + aggregate + read-only guards
# ===========================================================================

def plan(payload, progress=None):
    tick = progress or (lambda **_: None)
    filename = (payload.get("filename") or "uploaded.xlsx").strip()
    awb = (payload.get("awb") or "").strip().upper()
    batch = (payload.get("batch") or "").strip()
    warehouse = (payload.get("warehouse") or "").strip()
    b64 = payload.get("content_b64") or ""
    if not awb or not batch or not warehouse:
        raise lark.LarkError("柜号 / 客户批次号 / 仓库供应商 都必须填写")
    if not b64:
        raise lark.LarkError("没有收到文件内容 — 请重新选择文件")

    tick(stage="解析文件")
    try:
        data = base64.b64decode(b64)
    except Exception as e:                                    # noqa
        raise lark.LarkError(f"文件解码失败: {e}")
    sheets = file_parse.read_file(filename, data)
    merges = read_merges(data) if filename.lower().endswith(
        (".xlsx", ".xlsm", ".xltx")) else {}
    try:
        parsed = load_plan(sheets, merges)
    except ValueError as e:
        raise lark.LarkError(str(e))
    aggs, totals, ups_total = aggregate(parsed["records"])
    if not aggs:
        raise lark.LarkError("文件里没有带目的地的明细行")

    # ---- live option guards -------------------------------------------------
    tick(stage="读取 3.1 字段选项")
    t31 = lark.table_id("3.1")
    fm = lark.field_meta(t31)["by_name"]
    wh_opts = set(((fm.get(F31I["warehouse"]) or {}).get("options") or {}).values())
    rt_opts = set(((fm.get(F31I["route"]) or {}).get("options") or {}).values())
    if warehouse not in wh_opts:
        raise lark.LarkError(f"仓库供应商「{warehouse}」不是 3.1 的现有选项 — "
                             "绝不自动新增选项，请先在飞书确认")

    # ---- duplicate check ----------------------------------------------------
    tick(stage=f"查重 · 3.1 已有 {awb} 记录？")
    hits = _search(t31, [
        {"field_name": F31I["awb"], "operator": "contains", "value": [awb]},
    ], [F31I["awb"], F31I["route"], F31I["batch"], F31I["warehouse"],
        F31I["boxes"]], page_size=100)
    existing_by_dest, existing_other = {}, []
    for h in hits:
        f = h.get("fields") or {}
        h_awb = lark.flat_text(f.get(F31I["awb"])).upper()
        h_dest = normalize_dest(lark.flat_text(f.get(F31I["route"])))
        info = {"record_id": h["record_id"], "awb": h_awb, "dest": h_dest,
                "batch": lark.flat_text(f.get(F31I["batch"])),
                "boxes": lark.num_of(f.get(F31I["boxes"]))}
        if h_awb == awb and h_dest in aggs:
            existing_by_dest.setdefault(h_dest, []).append(info)
        else:
            existing_other.append(info)

    # ---- per-destination rows ----------------------------------------------
    rows = []
    for dest in sorted(aggs):
        a = aggs[dest]
        row = {"dest": dest, "boxes": a["boxes"], "weight": a["weight"],
               "volume": a["volume"], "plan_rows": a["rows"],
               "action": None, "existing": None,
               "warnings": [], "blockers": [], "notes": []}
        if a["ups_rows"]:
            row["warnings"].append(
                f"{a['ups_rows']} 条明细疑似 UPS/快递（关键词或 1Z 单号）— "
                "已计入合计，请人工确认是否应剔除")
        ex = existing_by_dest.get(dest)
        if ex:
            row["action"] = "exists"
            row["existing"] = ex[0]
            row["notes"].append(
                f"3.1 已有同柜号同路线记录（{ex[0]['record_id']}"
                + (f"，箱数 {int(ex[0]['boxes'])}" if ex[0]["boxes"] is not None else "")
                + "）— 跳过，不重复创建")
            if ex[0]["boxes"] is not None and int(ex[0]["boxes"]) != int(a["boxes"]):
                row["warnings"].append(
                    f"已有记录箱数 {int(ex[0]['boxes'])} ≠ 文件合计 {a['boxes']} — 请人工核对")
            if len(ex) > 1:
                row["warnings"].append(f"同柜号同路线已有 {len(ex)} 条记录 — 请人工清理")
        elif dest not in rt_opts:
            row["action"] = "block"
            row["blockers"].append(
                f"目的地「{dest}」不是 3.1 目的地路线的现有选项 — 绝不自动新增选项；"
                "如确属新仓点请先在飞书添加选项后重试")
        else:
            row["action"] = "create"
            row["fields"] = {
                F31I["awb"]: awb,
                F31I["batch"]: batch,
                F31I["warehouse"]: warehouse,
                F31I["route"]: dest,
                F31I["boxes"]: a["boxes"],
                F31I["weight"]: a["weight"],
                F31I["volume"]: a["volume"],
            }
        row["sig"] = _sig([row["action"], row.get("fields") or {}])
        rows.append(row)

    warnings_top = []
    if existing_other:
        show = "、".join(f"{e['awb']}→{e['dest'] or '?'}" for e in existing_other[:8])
        warnings_top.append(
            f"3.1 另有 {len(existing_other)} 条相近柜号记录（{show}"
            + ("…" if len(existing_other) > 8 else "") + "）— 与本次路线不重叠，仅提示")

    summary = {"dests": len(rows),
               "create": sum(1 for r in rows if r["action"] == "create"),
               "exists": sum(1 for r in rows if r["action"] == "exists"),
               "block": sum(1 for r in rows if r["action"] == "block"),
               "warnings": sum(len(r["warnings"]) for r in rows) + len(warnings_top),
               "ups_rows": ups_total}
    return {"env": lark.env(), "awb": awb, "batch": batch, "warehouse": warehouse,
            "filename": filename, "sheet": parsed["sheet"],
            "header_row": parsed["header_row"] + 1,
            "totals": totals, "rows": rows, "summary": summary,
            "warnings_top": warnings_top}


# ===========================================================================
# COMMIT — batch_create the approved rows, verify via batch_get
# ===========================================================================

def commit(payload, approvals, client_env, progress=None):
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
        return _commit_locked(payload, approvals, tick)
    finally:
        lark.WRITE_LOCK.release()


def _commit_locked(payload, approvals, tick):
    def replan_tick(**kw):
        if kw.get("stage"):
            kw["stage"] = "复检 · " + kw["stage"]
        tick(**kw)

    # Re-plan under the lock: a record created since the preview (another
    # operator / another tab) flips its row to 'exists' and is skipped.
    result = plan(payload, progress=replan_tick)
    by_dest = {r["dest"]: r for r in result["rows"]}

    creates = []
    for a in approvals or []:
        r = by_dest.get(a.get("dest"))
        if r is None:
            continue
        r["approved"] = True
        if r["action"] == "create" and r["sig"] == a.get("sig"):
            creates.append(r)
        elif r["action"] == "exists":
            r["commit"] = {"done": True, "skipped": "复检时发现已存在 — 未重复创建"}
        else:
            r["commit"] = {"done": False,
                           "skipped": "情况与预检时不同（数据已变化）— 请重新预检"}

    t31, base = lark.table_id("3.1"), _base()
    if creates:
        tick(stage=f"写入 · 新建 {len(creates)} 条 3.1 记录", done=0,
             total=len(creates))
        try:
            made = lark._api(
                "POST",
                f"/open-apis/bitable/v1/apps/{base}/tables/{t31}/records/batch_create",
                payload={"records": [{"fields": r["fields"]} for r in creates]},
                query={"client_token": str(uuid.uuid4())},
            ).get("records", [])
            for r, rec in zip(creates, made):
                r["commit"] = {"done": True, "record_id": rec.get("record_id")}
        except Exception as e:                                # noqa
            for r in creates:
                r["commit"] = {"done": False, "error": f"3.1 创建失败：{e}"}
            creates = []

    # ---- read-back verification (batch_get: search-index independent) ------
    if creates:
        tick(stage="核实 · 回读校验", done=0, total=len(creates))
        ids = [r["commit"]["record_id"] for r in creates]
        try:
            got = lark._api(
                "POST",
                f"/open-apis/bitable/v1/apps/{base}/tables/{t31}/records/batch_get",
                payload={"record_ids": ids},
            ).get("records", [])
            by_id = {g.get("record_id"): (g.get("fields") or {}) for g in got}
        except Exception as e:                                # noqa
            by_id = {}
            for r in creates:
                r["commit"]["verified"] = False
                r["commit"]["note"] = f"已创建，但回读失败：{e}"
        for i, r in enumerate(creates):
            c = r["commit"]
            if c.get("note"):
                continue
            tick(stage="核实 · 回读校验", done=i + 1, total=len(creates),
                 current=r["dest"])
            f = by_id.get(c["record_id"])
            if f is None:
                c["verified"] = False
                c["note"] = "已创建，但回读未找到该记录 — 请人工确认"
                continue
            checks = []
            want = r["fields"]
            for name in (F31I["awb"], F31I["batch"], F31I["warehouse"], F31I["route"]):
                got_v = lark.flat_text(f.get(name))
                checks.append({"what": name, "ok": got_v == str(want[name]),
                               "got": got_v})
            for name in (F31I["boxes"], F31I["weight"], F31I["volume"]):
                got_n = lark.num_of(f.get(name))
                ok = got_n is not None and abs(got_n - float(want[name])) < 0.005
                checks.append({"what": name, "ok": ok, "got": got_n})
            c["checks"] = checks
            c["verified"] = all(k["ok"] for k in checks)

    ws = list(result["warnings_top"])
    for r in result["rows"]:
        for w in r["warnings"]:
            ws.append(f"{r['dest']}: {w}")
        c = r.get("commit") or {}
        if c.get("error"):
            ws.append(f"{r['dest']}: {c['error']}")
        if c.get("note"):
            ws.append(f"{r['dest']}: {c['note']}")
        if c.get("verified") is False:
            ws.append(f"{r['dest']}: 写入后核实未通过 — 请人工检查")
    result["warnings_summary"] = ws
    result["committed"] = True
    tick(stage="完成", current="")
    return result
