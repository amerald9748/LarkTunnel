/**
 * parse-plan.js — Unloading-plan (收货派送计划) Excel parser.
 *
 * Logic extracted/ported from the West Pipeline module
 * awb-batch-processor/src/pod_generator.py (battle-tested on real plans):
 *   - POD_COLUMN_ALIASES canonical column map (+ 体积 added for this use)
 *   - header-row auto-detection (scan first 20 rows for most alias hits)
 *   - data-sheet auto-selection (skip UPS-named sheets, prefer 加西/WEST/卡派…)
 *   - vertical-merge expansion (quantity columns keep the group total in the
 *     top row and get 0 in sub-rows so sums still match; identity columns are
 *     copied down so sub-rows keep their destination)
 *   - destination normalization ('ca-yvr4 ' -> 'YVR4')
 *   - UPS row detection (keywords or 1Z tracking codes anywhere in the row)
 *
 * Pure parsing — no Lark access here.
 */

const xlsx = require('xlsx');

// --- Canonical (Chinese) columns and their known aliases -------------------
// From pod_generator.py POD_COLUMN_ALIASES; 体积 added (the Python module only
// used volume as a merge hint, but the 3.1 import needs it as a real column).
const COLUMN_ALIASES = {
  '派送目的地': ['派送目的地', '收件人邮编*', '仓库代码'],
  '重量': ['重量', '实际重量', '实际重量(KG)'],
  '箱数/件数': ['箱数/件数', '件数*', '件数', '箱数'],
  '体积': ['体积', '材积', '立方', 'CBM'],
  'FBA NO.': ['FBA NO.', 'FBA', 'AMAZON REFERENCE ID*', '扩展单号'],
  'PO#': ['PO#', 'PO', 'REFERENCE ID'],
  '板数': ['板数', '托盘数', '托盘', 'PLTS', 'PALLETS'],
  '派送方式': ['派送方式', 'METHOD'],
};

// Columns that must be present for a sheet/header row to qualify
const ESSENTIAL_COLUMNS = ['派送目的地', '箱数/件数'];

const UPS_KEYWORDS = ['UPS', '快递', 'COURIER', 'EXPRESS'];
// Sheet-name keywords hinting at the truck/West data we want (priority order)
const PREFERRED_SHEET_KEYWORDS = ['加西', 'WEST', '卡派', '卡车', 'LTL', '派送', '清单', 'PLAN', 'UNLOADING'];

// Merged quantity columns hold the GROUP total in the top cell; sub-rows get 0
const QUANTITY_CANONICALS = new Set(['箱数/件数', '重量', '板数', '体积']);
const QUANTITY_RAW_HINTS = ['体积', '材积'];

// 1Z code appearing in ANY cell; requires 12+ chars after '1Z' so short PO#s
// starting with 1Z can't match (real UPS tracking numbers have 16).
const UPS_TRACKING_ANYWHERE_RE = /\b1Z[0-9A-Z]{12,}\b/;
const UPS_TRACKING_RE = /^1Z[0-9A-Z]{8,}$/;

/** alias (upper) -> canonical column name */
function buildLookup() {
  const lookup = {};
  for (const [canonical, aliases] of Object.entries(COLUMN_ALIASES)) {
    for (const alias of aliases) lookup[alias.trim().toUpperCase()] = canonical;
  }
  return lookup;
}

/** "'ca-yvr4 ' -> 'YVR4'" — strip CA- prefix, trim, uppercase. */
function normalizeDestination(value) {
  let dest = String(value).trim().toUpperCase();
  if (dest.startsWith('CA-')) dest = dest.slice(3);
  return dest;
}

/** Sheet -> row matrix (arrays, nulls for blanks). */
function sheetMatrix(ws) {
  return xlsx.utils.sheet_to_json(ws, { header: 1, defval: null });
}

/**
 * Returns { headerRow, matches } for the row within the first maxScanRows
 * that contains the most known column aliases.
 */
function scanHeaderRow(matrix, lookup, maxScanRows = 20) {
  let bestRow = 0;
  let maxMatches = 0;
  for (let i = 0; i < Math.min(matrix.length, maxScanRows); i++) {
    const rowValues = new Set((matrix[i] || []).map((x) => String(x).trim().toUpperCase()));
    let matches = 0;
    for (const alias of Object.keys(lookup)) if (rowValues.has(alias)) matches++;
    if (matches > maxMatches) {
      maxMatches = matches;
      bestRow = i;
    }
  }
  return { headerRow: bestRow, matches: maxMatches };
}

/**
 * Picks the sheet the data should come from.
 * UPS-named sheets are skipped. Among qualifying sheets (>=2 header matches),
 * sheet-name keywords win; otherwise the strongest header match.
 * Returns { sheetName, headerRow } or { sheetName: null }.
 */
function selectSheet(wb, upsKeywords = UPS_KEYWORDS) {
  const lookup = buildLookup();
  const candidates = []; // [rank, -matches, order, sheetName, headerRow]

  wb.SheetNames.forEach((sheet, order) => {
    const sheetUpper = String(sheet).toUpperCase();
    if (upsKeywords.some((k) => sheetUpper.includes(k))) return;
    const matrix = sheetMatrix(wb.Sheets[sheet]);
    const { headerRow, matches } = scanHeaderRow(matrix, lookup);
    if (matches < 2) return;
    let rank = PREFERRED_SHEET_KEYWORDS.length;
    for (let i = 0; i < PREFERRED_SHEET_KEYWORDS.length; i++) {
      if (sheetUpper.includes(PREFERRED_SHEET_KEYWORDS[i])) { rank = i; break; }
    }
    candidates.push([rank, -matches, order, sheet, headerRow]);
  });

  if (!candidates.length) return { sheetName: null, headerRow: null };
  candidates.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  const [, , , sheetName, headerRow] = candidates[0];
  return { sheetName, headerRow };
}

/**
 * Expands vertically merged cells that read as value-then-blanks.
 * Quantity columns: sub-rows get 0 (top cell already holds the group total).
 * Identity columns (destination, waybill…): top value is copied down so
 * sub-rows keep their destination and aren't dropped.
 * Mutates and returns the matrix.
 */
function fillVerticalMerges(matrix, ws, headerRow) {
  const lookup = buildLookup();
  const merges = ws['!merges'] || [];
  const headers = matrix[headerRow] || [];

  for (const rng of merges) {
    if (rng.s.c !== rng.e.c || rng.s.r === rng.e.r) continue; // single-column vertical only
    const col = rng.s.c;
    if (rng.s.r <= headerRow) continue; // merge belongs to a title/header area
    const rawHeader = String(headers[col] ?? '').trim();
    const canonical = lookup[rawHeader.toUpperCase()] || rawHeader;
    const isQuantity =
      QUANTITY_CANONICALS.has(canonical) || QUANTITY_RAW_HINTS.some((k) => rawHeader.includes(k));
    const topVal = (matrix[rng.s.r] || [])[col];
    for (let r = rng.s.r + 1; r <= Math.min(rng.e.r, matrix.length - 1); r++) {
      if (!matrix[r]) continue;
      if (isQuantity) matrix[r][col] = 0;
      else if (matrix[r][col] === null || matrix[r][col] === undefined) matrix[r][col] = topVal;
    }
  }
  return matrix;
}

/** A row counts as UPS when 派送方式/派送目的地 matches a UPS keyword or bare
 *  tracking number, OR any cell holds a 1Z... tracking code. */
function isUpsRow(record) {
  for (const col of ['派送方式', '派送目的地']) {
    if (record[col] !== undefined && record[col] !== null) {
      const val = String(record[col]).trim().toUpperCase();
      if (UPS_KEYWORDS.some((k) => val.includes(k)) || UPS_TRACKING_RE.test(val)) return true;
    }
  }
  for (const val of Object.values(record)) {
    if (typeof val === 'string' && UPS_TRACKING_ANYWHERE_RE.test(val.toUpperCase())) return true;
  }
  return false;
}

/**
 * Reads the unloading plan and returns { sheetName, headerRow, records } with
 * canonical column names, rows without a destination dropped.
 * Throws when no usable sheet/header can be found.
 */
function loadPlan(filepath, { sheetName = null, headerRow = null } = {}) {
  const wb = xlsx.readFile(filepath);
  const lookup = buildLookup();

  if (!sheetName) {
    const sel = selectSheet(wb);
    if (!sel.sheetName) throw new Error('未能识别表头 (no recognizable header row found in any sheet)');
    sheetName = sel.sheetName;
    if (headerRow === null) headerRow = sel.headerRow;
  } else if (headerRow === null) {
    const scan = scanHeaderRow(sheetMatrix(wb.Sheets[sheetName]), lookup);
    if (scan.matches < 2) throw new Error(`未能识别表头 (sheet '${sheetName}' has no recognizable header row)`);
    headerRow = scan.headerRow;
  }

  const ws = wb.Sheets[sheetName];
  const matrix = fillVerticalMerges(sheetMatrix(ws), ws, headerRow);
  const headers = (matrix[headerRow] || []).map((h) => String(h ?? '').trim());

  // canonical name per column; multiple aliases -> same canonical: keep first
  const colCanonical = headers.map((h) => lookup[h.toUpperCase()] || h);
  const seen = new Set();
  const useCol = colCanonical.map((c) => {
    if (!c || seen.has(c)) return false;
    seen.add(c);
    return true;
  });

  for (const essential of ESSENTIAL_COLUMNS) {
    if (!colCanonical.includes(essential)) {
      throw new Error(`缺少必要列 (missing column): ${essential}`);
    }
  }

  const records = [];
  for (let r = headerRow + 1; r < matrix.length; r++) {
    const row = matrix[r];
    if (!row) continue;
    const rec = {};
    for (let c = 0; c < colCanonical.length; c++) {
      if (useCol[c] && row[c] !== null && row[c] !== undefined) rec[colCanonical[c]] = row[c];
    }
    const dest = rec['派送目的地'];
    if (dest === undefined || dest === null || String(dest).trim() === '') continue;
    records.push(rec);
  }

  return { sheetName, headerRow, records };
}

const round2 = (n) => Math.round(n * 100) / 100;

/**
 * Groups parsed records by normalized destination and sums quantities.
 * Returns { aggregates: {dest: {boxes, weight, volume, rows}}, totals, upsRows }.
 */
function aggregateByDestination(records) {
  const aggregates = {};
  const totals = { boxes: 0, weight: 0, volume: 0, rows: 0 };
  const upsRows = records.filter(isUpsRow);

  for (const rec of records) {
    const dest = normalizeDestination(rec['派送目的地']);
    const a = (aggregates[dest] = aggregates[dest] || { boxes: 0, weight: 0, volume: 0, rows: 0 });
    a.boxes += Number(rec['箱数/件数']) || 0;
    a.weight += Number(rec['重量']) || 0;
    a.volume += Number(rec['体积']) || 0;
    a.rows++;
    totals.boxes += Number(rec['箱数/件数']) || 0;
    totals.weight += Number(rec['重量']) || 0;
    totals.volume += Number(rec['体积']) || 0;
    totals.rows++;
  }

  for (const a of Object.values(aggregates)) {
    a.weight = round2(a.weight);
    a.volume = round2(a.volume);
  }
  totals.weight = round2(totals.weight);
  totals.volume = round2(totals.volume);

  return { aggregates, totals, upsRows };
}

module.exports = {
  COLUMN_ALIASES,
  UPS_KEYWORDS,
  buildLookup,
  normalizeDestination,
  scanHeaderRow,
  selectSheet,
  fillVerticalMerges,
  isUpsRow,
  loadPlan,
  aggregateByDestination,
};
