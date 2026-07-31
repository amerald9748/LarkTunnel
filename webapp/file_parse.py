# -*- coding: utf-8 -*-
"""
file_parse.py — read an uploaded spreadsheet / text file and auto-detect the
six shipment identifiers used by this project:

    柜号(AWB) · 客户批次号 · 仓库供应商 · 目的地路线 · 预约时间 · ISA

Anything not present in the file comes back as None (JSON null).

Reading
-------
* .xlsx / .xlsm — openpyxl when installed (proper date cells), otherwise a
  stdlib zipfile + XML fallback so the app keeps working with no dependencies.
* .csv / .tsv / .txt — stdlib csv with delimiter sniffing and utf-8/GBK fallback.

Detection strategy
------------------
1. **Header-driven** (preferred): find the header row, map columns to fields by
   alias. Longest matching alias wins, so `仓库代码` is read as a *route*
   column rather than a *warehouse* one.
2. **Pattern fallback**: if a field has no column, scan every cell with regexes
   and against the LIVE select-option names from table 3.1.

READ ONLY — this module only parses bytes it is handed; it never touches Lark.
"""

import io
import re
import csv
import json
import zipfile
import datetime
import xml.etree.ElementTree as ET

MAX_SCAN_CELLS = 400_000  # safety cap for pattern scanning
MIN_HEADER_HITS = 2       # a header row must match >=2 aliases (see _find_header_row)

# Pallet-count column (kept out of the 6 summary fields; used only for details).
PALLET_ALIASES = ["总托数", "板数", "托数", "总板数", "托盘数", "托数量"]

# ---------------------------------------------------------------------------
# Header aliases (lowercased, spaces stripped). Longest match wins.
# ---------------------------------------------------------------------------
HEADER_ALIASES = {
    "awb": ["柜号/awb", "柜号", "货柜号", "集装箱号", "箱号", "awb", "container",
            "containerno", "提单号", "柜号/提单号", "装货明细", "装柜明细", "柜号明细"],
    "batch": ["客户批次号", "批次号", "客户批次", "批次", "batch", "batchno"],
    "warehouse": ["仓库供应商", "操作仓库", "供应商", "warehouse", "仓库名称"],
    # NOTE: bare 仓库 means the destination warehouse code (YYC4/YEG2/...) in the
    # 出库计划 sheets. Longest-alias-wins keeps 仓库供应商 on `warehouse`.
    "route": ["目的地路线", "派送目的地", "派送仓点", "派送仓库", "仓库代码",
              "目的地", "目的仓", "路线", "仓点", "仓库", "目的仓库", "destination", "dest"],
    "appointment": ["预约时间", "预约日期", "预约", "时间", "appointmenttime", "appointment"],
    # 预约号 IS the ISA (per operator). Longer than the 预约 appointment alias,
    # so longest-match keeps the two columns apart.
    "isa": ["isa", "isa号", "isa#", "isanumber", "isano", "预约号", "预约编号", "预约单号"],
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# ISO container. Deliberately NOT using \b: Chinese is a word character in
# Python's re, so \b fails in `UETU8792061转运` and the container is missed.
# Trailing guard excludes only letters, so `MATU45919300` still yields MATU4591930.
RE_CONTAINER = re.compile(r"(?<![A-Z0-9])([A-Z]{4}\d{7}[A-Z]?)(?![A-Z])")
RE_PALLET = re.compile(r"(\d{1,3})\s*P(?![A-Z])", re.I)        # -13P / 7P
RE_AWB_NUM = re.compile(r"\b(\d{3}-\d{7})\b")                  # 093-9992123
RE_ISA_LABELED = re.compile(r"ISA[^0-9A-Za-z]{0,4}(\d{6,})", re.I)
RE_ISA_DASH = re.compile(r"\b(ISA-\d{3,})\b", re.I)
RE_BATCH_A = re.compile(r"\b([A-Z]{2,5}-[A-Z]{2}\d{4,}[A-Z]?)\b")   # KQ-US26058B
RE_BATCH_B = re.compile(r"\b(\d{4}-[A-Z]{2,3}-\d{2,5})\b")          # 2026-LY-618
RE_DATETIME = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)\b")
# Month-first with optional time + North-American tz, e.g. 07/24/2026 07:00 MDT
RE_US_DATETIME = re.compile(
    r"\b(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?"
    r"(?:\s*(MDT|MST|PDT|PST|EDT|EST|CDT|CST|UTC|GMT))?", re.I)
RE_ROUTE_PREFIXED = re.compile(r"\b(?:CA-)?([A-Z]{3}\d{1,2})\b")     # CA-YVR4 -> YVR4
# 'label：value' inside a single cell, e.g. 客户批次号：JF-US26038B
RE_LABELED = re.compile(r"([一-鿿A-Za-z/#]+)\s*[：:]\s*([^，,；;]+)")

EXCEL_EPOCH = datetime.datetime(1899, 12, 30)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _decode(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace")


def _read_delimited(data, filename):
    text = _decode(data)
    sample = text[:8000]
    delim = "\t" if filename.lower().endswith((".tsv", ".tab")) else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except Exception:
            delim = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    return [{"name": filename, "rows": rows}]


def _read_xlsx_openpyxl(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheets = []
    try:
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else v for v in r])
            sheets.append({"name": ws.title, "rows": rows})
    finally:
        wb.close()
    return sheets


def _q(tag):
    return "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}" + tag


def _col_index(ref):
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _read_xlsx_stdlib(data):
    """Minimal xlsx reader: zip + XML, no third-party deps."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())

    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(_q("si")):
            shared.append("".join(t.text or "" for t in si.iter(_q("t"))))

    rel_map = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rroot = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot:
            rel_map[rel.get("Id")] = rel.get("Target")

    sheets = []
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sh in wb.iter(_q("sheet")):
        title = sh.get("name") or "sheet"
        target = rel_map.get(sh.get(rid_attr)) or ""
        target = target.lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        if path not in names:
            continue
        rows = []
        sroot = ET.fromstring(z.read(path))
        for row in sroot.iter(_q("row")):
            cells = {}
            for c in row.findall(_q("c")):
                idx = _col_index(c.get("r"))
                t = c.get("t")
                if t == "s":
                    v = c.find(_q("v"))
                    val = shared[int(v.text)] if v is not None and v.text else ""
                elif t == "inlineStr":
                    is_ = c.find(_q("is"))
                    val = "".join(x.text or "" for x in is_.iter(_q("t"))) if is_ is not None else ""
                else:
                    v = c.find(_q("v"))
                    val = v.text if v is not None else ""
                cells[idx] = val
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        sheets.append({"name": title, "rows": rows})
    return sheets


def read_file(filename, data):
    """-> [{'name': sheet, 'rows': [[cell, ...], ...]}]"""
    low = (filename or "").lower()
    if low.endswith((".xlsx", ".xlsm", ".xltx")):
        try:
            return _read_xlsx_openpyxl(data)
        except ImportError:
            return _read_xlsx_stdlib(data)
        except Exception:
            return _read_xlsx_stdlib(data)
    if low.endswith(".xls"):
        raise ValueError("旧版 .xls 不支持，请另存为 .xlsx 后重试")
    return _read_delimited(data, filename or "uploaded")


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------

def as_text(v):
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _norm_header(v):
    return re.sub(r"[\s　]+", "", as_text(v)).lower()


def to_datetime_text(v):
    """Normalise an appointment-ish cell to 'YYYY-MM-DD HH:MM' or None."""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 < float(v) < 80000:  # plausible Excel serial date
            return (EXCEL_EPOCH + datetime.timedelta(days=float(v))).strftime("%Y-%m-%d %H:%M")
        return None
    s = as_text(v)
    if not s:
        return None
    m = RE_DATETIME.search(s)
    if m:
        return m.group(1).replace("/", "-")
    m = RE_US_DATETIME.search(s)
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 and day <= 12:      # tolerate a DD/MM/YYYY source
            mo, day = day, mo
        if 1 <= mo <= 12 and 1 <= day <= 31:
            hh = int(m.group(4) or 0)
            mi = m.group(5) or "00"
            tz = (" " + m.group(6).upper()) if m.group(6) else ""
            return f"{yr:04d}-{mo:02d}-{day:02d} {hh:02d}:{mi}{tz}"
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        f = float(s)
        if 20000 < f < 80000:
            return (EXCEL_EPOCH + datetime.timedelta(days=f)).strftime("%Y-%m-%d %H:%M")
    return None


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------

def _match_field(header_text):
    """Return (field, alias_len) for the longest alias matching this header."""
    best = (None, 0)
    if not header_text:
        return best
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if header_text == a or a in header_text:
                if len(a) > best[1]:
                    best = (field, len(a))
    return best


def _is_labeled_cell(text):
    """True for 'label：value' cells (e.g. 客户批次号：JF-US26038B).
    Those are title-block metadata, NOT column headers."""
    return bool(re.search(r"[：:]\s*\S", text or ""))


def _find_header_row(rows, scan=30):
    """Row index with the most alias hits.

    Requires MIN_HEADER_HITS matches — the same rule the project's existing
    parse-plan.js uses. A single stray match (like a '客户批次号：X' title cell)
    must not be mistaken for a header row, or every column below it is read
    from the wrong place. Ties break toward the row with more non-empty cells,
    since a real header spans several columns.
    """
    best = (-1, 0, 0, {})  # (row_index, hits, non_empty_cells, mapping)
    for i, row in enumerate(rows[:scan]):
        mapping, seen = {}, set()
        for j, cell in enumerate(row):
            text = as_text(cell)
            if not text or _is_labeled_cell(text):
                continue
            field, _score = _match_field(_norm_header(cell))
            if field and field not in seen:
                mapping[field] = j
                seen.add(field)
        hits = len(mapping)
        nonempty = sum(1 for c in row if as_text(c))
        if hits > best[1] or (hits == best[1] and hits > 0 and nonempty > best[2]):
            best = (i, hits, nonempty, mapping)
    return (best[0], best[3]) if best[1] >= MIN_HEADER_HITS else (-1, {})


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _add(bucket, value, source):
    if value is None:
        return
    v = value.strip() if isinstance(value, str) else value
    if v == "" or v is None:
        return
    bucket.setdefault(v, {"count": 0, "source": source})
    bucket[v]["count"] += 1


def containers_with_pallets(text):
    """[(container, pallets|None)] for EVERY container in a cell.

    A 装货明细 cell often groups many containers (newline separated, or
    'MATU45919300-10P/ZCSU7624420-2P'), so this must find them all — using
    `search` here would silently keep only the first one per cell.
    """
    t = as_text(text).upper()
    if not t:
        return []
    matches = list(RE_CONTAINER.finditer(t))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        tail = t[m.end():end]          # only this container's own segment
        pm = RE_PALLET.search(tail)
        out.append((m.group(1), int(pm.group(1)) if pm else None))
    return out


def _clean_route(text, route_options_lower):
    """Normalise CA-YVR4 -> YVR4 and validate against live options."""
    t = as_text(text).strip()
    if not t:
        return None
    if t.lower() in route_options_lower:
        return route_options_lower[t.lower()]
    m = RE_ROUTE_PREFIXED.fullmatch(t) or RE_ROUTE_PREFIXED.search(t)
    if m:
        cand = m.group(1)
        if cand.lower() in route_options_lower:
            return route_options_lower[cand.lower()]
        return cand
    return None


def _row_isa(text):
    t = as_text(text)
    if not t:
        return None
    m = RE_ISA_LABELED.search(t) or RE_ISA_DASH.search(t)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"\d{6,}", t) else None


def _is_container_title(text):
    """A '柜号 CNTR：<id>' style marker row that governs the rows beneath it."""
    t = as_text(text)
    if not t:
        return False
    return bool(re.match(r"^\s*柜号\s*(?:cntr)?\s*[:：]", t, re.I)) or \
        ("柜号" in t and "CNTR" in t.upper())


def _container_in_row(row):
    for c in row:
        found = containers_with_pallets(c)
        if found:
            return found[0][0]
    return None


def _pallets_from(text):
    """Leading integer of a 总托数/板数 cell: 17 -> 17, '13P-9' -> 13, '4箱' -> 4."""
    m = re.match(r"\s*(\d{1,3})", as_text(text))
    return int(m.group(1)) if m else None


def _find_pallet_cols(header_row):
    cols = []
    for j, cell in enumerate(header_row):
        h = _norm_header(cell)
        if h and any(a in h for a in PALLET_ALIASES):
            cols.append(j)
    return cols


def extract_details(sheets, rt_lower, wh_lower):
    """One record per container×route, unified across both sheet layouts.

    Layout A — 出库计划: the container(s) live in the row's 装货明细 cell
        (possibly several, each with its own -13P); route/ISA/时间 are columns.
    Layout B — 派送计划: a '柜号 CNTR：<id>' title row sets the active container
        for every 派送仓点 row beneath it, until the next title row. Each such
        row carries its own 总托数 / 预约号 / 预约时间. Express / private-address
        rows (UPS, PUROLATOR, AST…) simply have no 预约号/时间 -> Null.
    """
    details = []
    for sheet in sheets:
        rows = sheet.get("rows") or []
        if not rows:
            continue
        hdr_i, colmap = _find_header_row(rows)
        header_row = rows[hdr_i] if hdr_i >= 0 else []
        pallet_cols = _find_pallet_cols(header_row) if header_row else []
        current_container = None

        for r_i, row in enumerate(rows):
            texts = [as_text(c) for c in row]
            if not any(texts):
                continue
            if hdr_i >= 0 and r_i == hdr_i:
                continue
            if _is_container_title(texts[0]):
                cid = _container_in_row(row)
                if cid:
                    current_container = cid
                continue

            def col(field):
                j = colmap.get(field)
                return row[j] if j is not None and j < len(row) else None

            # (container, pallets) pairs for this row
            awb_cell = col("awb")
            awb_found = containers_with_pallets(awb_cell) if awb_cell is not None else []
            row_pallet_raw = None
            for j in pallet_cols:
                if j < len(row) and as_text(row[j]):
                    row_pallet_raw = as_text(row[j])
                    break
            if awb_found:                                   # Layout A
                pairs = awb_found
            elif current_container:                          # Layout B
                pairs = [(current_container, _pallets_from(row_pallet_raw))]
            else:                                            # headerless fallback
                anyfound = []
                for c in row:
                    anyfound.extend(containers_with_pallets(c))
                pairs = anyfound
            if not pairs:
                continue

            # route: mapped column verbatim (keep UPS/AST/私人地址), normalise CA-YVR4->YVR4
            route = None
            if colmap.get("route") is not None:
                raw = as_text(col("route"))
                if raw:
                    route = _clean_route(raw, rt_lower) or raw
            if route is None:
                for t in texts:
                    cand = _clean_route(t, rt_lower)
                    if cand and cand.lower() in rt_lower:
                        route = cand
                        break

            wh = None
            if colmap.get("warehouse") is not None:
                wt = as_text(col("warehouse"))
                wh = wh_lower.get(wt.lower(), wt) if wt else None

            isa = _row_isa(col("isa")) if colmap.get("isa") is not None else None
            if isa is None:
                for t in texts:
                    m = RE_ISA_LABELED.search(t) or RE_ISA_DASH.search(t)
                    if m:
                        isa = m.group(1)
                        break

            appt = to_datetime_text(col("appointment")) if colmap.get("appointment") is not None else None
            if appt is None:
                for c in row:
                    d = to_datetime_text(c)
                    if d:
                        appt = d
                        break

            batch = as_text(col("batch")) or None if colmap.get("batch") is not None else None
            grouped = len(pairs) > 1
            for cid, pallets in pairs:
                praw = (f"{pallets}P" if pallets is not None else None) if awb_found else row_pallet_raw
                details.append({
                    "awb": cid, "route": route, "isa": isa, "appointment": appt,
                    "warehouse": wh, "batch": batch, "pallets": pallets,
                    "pallets_raw": praw, "grouped": grouped,
                    "sheet": sheet["name"], "row": r_i + 1,
                })
    return details


def extract(sheets, warehouse_options=None, route_options=None):
    """-> {field: {value, all, count, source} | None-valued dict}"""
    warehouse_options = warehouse_options or []
    route_options = route_options or []
    wh_lower = {w.lower(): w for w in warehouse_options if w}
    rt_lower = {r.lower(): r for r in route_options if r}

    buckets = {k: {} for k in HEADER_ALIASES}
    header_info = []
    scanned = 0

    for sheet in sheets:
        rows = sheet.get("rows") or []
        if not rows:
            continue
        hdr_i, colmap = _find_header_row(rows)
        if colmap:
            header_info.append({"sheet": sheet["name"], "header_row": hdr_i + 1,
                                "columns": {k: rows[hdr_i][v] if v < len(rows[hdr_i]) else ""
                                            for k, v in colmap.items()}})
            for row in rows[hdr_i + 1:]:
                for field, col in colmap.items():
                    if col >= len(row):
                        continue
                    raw = row[col]
                    src = f"列「{as_text(rows[hdr_i][col])}」· {sheet['name']}"
                    if field == "appointment":
                        _add(buckets[field], to_datetime_text(raw), src)
                    elif field == "route":
                        _add(buckets[field], _clean_route(raw, rt_lower) or None, src)
                    elif field == "warehouse":
                        t = as_text(raw)
                        _add(buckets[field], wh_lower.get(t.lower(), t) if t else None, src)
                    elif field == "awb":
                        t = as_text(raw)
                        found = containers_with_pallets(t)
                        if found:
                            for cid, _p in found:      # a cell may group many
                                _add(buckets[field], cid, src)
                        else:
                            m = RE_AWB_NUM.search(t)
                            _add(buckets[field], m.group(1) if m else (t or None), src)
                    elif field == "isa":
                        t = as_text(raw)
                        m = RE_ISA_LABELED.search(t) or RE_ISA_DASH.search(t)
                        if m:
                            _add(buckets[field], m.group(1), src)
                        elif re.fullmatch(r"\d{6,}", t):
                            _add(buckets[field], t, src)
                    else:
                        _add(buckets[field], as_text(raw) or None, src)

    # ---- inline 'label：value' metadata (title blocks) ---------------------
    # Higher confidence than a loose regex sweep, so run it first.
    need = [f for f, b in buckets.items() if not b]
    if need:
        for sheet in sheets:
            for row in sheet.get("rows") or []:
                for cell in row:
                    t = as_text(cell)
                    if not t or ("：" not in t and ":" not in t):
                        continue
                    for m in RE_LABELED.finditer(t):
                        field, _ = _match_field(_norm_header(m.group(1)))
                        val = (m.group(2) or "").strip()
                        if not field or field not in need or not val:
                            continue
                        src = f"行内标注「{m.group(1)}」· {sheet['name']}"
                        if field == "appointment":
                            _add(buckets[field], to_datetime_text(val), src)
                        elif field == "route":
                            _add(buckets[field], _clean_route(val, rt_lower), src)
                        elif field == "warehouse":
                            _add(buckets[field], wh_lower.get(val.lower(), val), src)
                        elif field == "awb":
                            mm = RE_CONTAINER.search(val.upper()) or RE_AWB_NUM.search(val)
                            _add(buckets[field], mm.group(1) if mm else val.split()[0], src)
                        elif field == "isa":
                            mm = RE_ISA_LABELED.search(t) or RE_ISA_DASH.search(t)
                            if mm:
                                _add(buckets[field], mm.group(1), src)
                            elif re.fullmatch(r"\d{6,}", val):
                                _add(buckets[field], val, src)
                        else:
                            _add(buckets[field], val.split()[0], src)

    # ---- pattern fallback for whatever is still empty ----------------------
    need = [f for f, b in buckets.items() if not b]
    if need:
        for sheet in sheets:
            for row in sheet.get("rows") or []:
                for cell in row:
                    scanned += 1
                    if scanned > MAX_SCAN_CELLS:
                        break
                    t = as_text(cell)
                    if not t:
                        continue
                    up = t.upper()
                    src = f"正则扫描 · {sheet['name']}"
                    if "awb" in need:
                        found = containers_with_pallets(t)
                        if found:
                            for cid, _p in found:      # every container, not just the first
                                _add(buckets["awb"], cid, src)
                        else:
                            m = RE_AWB_NUM.search(t)
                            if m:
                                _add(buckets["awb"], m.group(1), src)
                    if "isa" in need:
                        m = RE_ISA_LABELED.search(t) or RE_ISA_DASH.search(t)
                        if m:
                            _add(buckets["isa"], m.group(1), src)
                    if "batch" in need:
                        m = RE_BATCH_A.search(up) or RE_BATCH_B.search(up)
                        if m:
                            _add(buckets["batch"], m.group(1), src)
                    if "warehouse" in need and t.lower() in wh_lower:
                        _add(buckets["warehouse"], wh_lower[t.lower()], src)
                    if "route" in need:
                        r = _clean_route(t, rt_lower)
                        if r and r.lower() in rt_lower:
                            _add(buckets["route"], r, src)
                    if "appointment" in need:
                        d = to_datetime_text(cell) if not isinstance(cell, str) else None
                        if d is None and RE_DATETIME.search(t):
                            d = to_datetime_text(t)
                        if d:
                            _add(buckets["appointment"], d, src)

    out = {}
    for field, bucket in buckets.items():
        if not bucket:
            out[field] = {"value": None, "all": [], "count": 0, "source": None}
            continue
        ordered = sorted(bucket.items(), key=lambda kv: -kv[1]["count"])
        out[field] = {
            "value": ordered[0][0],
            "all": [k for k, _ in ordered[:50]],
            "count": len(ordered),
            "source": ordered[0][1]["source"],
        }
    details = extract_details(sheets, rt_lower, wh_lower)
    return {"fields": out, "headers": header_info, "details": details}
