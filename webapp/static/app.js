"use strict";
// LarkTunnel query UI — vanilla JS, no deps.

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(kid));
  return n;
};

const state = { tables: [], views: [], fieldKey: "awb", mode: "contains", tz: "",
  parse: null };

// ---- console reporting ------------------------------------------------------
// 任务状态/结果/报错统一实时打进浏览器 console（F12 打开跟踪）。
const clog = (tag, ...a) => console.log(`[${tag}]`, ...a);
const cerr = (tag, ...a) => console.error(`[${tag}]`, ...a);

// 预演/写入结果明细表（console.table 一行一条记录）。
function ctable(tag, items) {
  if (!items || !items.length) return;
  console.table(items.map((it) => {
    const t = it.trip || {};
    const p = it.pallet || {};
    return {
      柜号: it.awb || "-", ISA: it.isa || "-", 动作: it.action,
      预约记录: it.record_id || "",
      出库计划: (t.do || "-") + (t.table ? `→${t.table}` : ""),
      出库计划记录: t.record_id || "",
      实际板数: (p.status || "-") + (p.value !== undefined ? `→${p.value}` : ""),
      说明: it.reason || t.note || p.note || "",
      报错: it.error || t.error || p.error || "",
    };
  }));
}

// Columns preferred (in order) for the summary table when present.
const PREFERRED = ["柜号/AWB", "目的地路线", "仓库供应商", "客户", "客户批次号",
  "箱数", "重量", "体积", "预计板数", "实际板数", "总板数", "派送状态",
  "预约时间", "出库时间", "送达时间", "ISA", "派送计划", "POD附件"];

function statusChipClass(v) {
  const s = String(v);
  if (/完成|成功|已送达|送达/.test(s)) return "ok";
  if (/异常|失败|拒/.test(s)) return "bad";
  if (/待|未|hold|Hold|进行|排队/.test(s)) return "warn";
  return "neu";
}

// ---- theme (自动 / 浅色 / 深色) --------------------------------------------
// The <head> script already stamped data-theme for the first paint; this only
// handles cycling + persistence + reacting to OS changes while in 自动.
const THEME_KEY = "larkTheme";
const THEME_MODES = [["auto", "🌗 自动"], ["light", "☀️ 浅色"], ["dark", "🌙 深色"]];

function applyTheme(mode) {
  const dark = mode === "dark" || (mode === "auto"
    && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const label = (THEME_MODES.find((t) => t[0] === mode) || THEME_MODES[0])[1];
  const btn = $("#themeBtn");
  if (btn) btn.textContent = label;
}

function initTheme() {
  let mode = "auto";
  try { mode = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { /* private mode */ }
  applyTheme(mode);
  $("#themeBtn").addEventListener("click", () => {
    const i = THEME_MODES.findIndex((t) => t[0] === mode);
    mode = THEME_MODES[(i + 1) % THEME_MODES.length][0];
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* ignore */ }
    applyTheme(mode);
    clog("主题", mode);
  });
  window.matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => { if (mode === "auto") applyTheme("auto"); });
}

// ---- tabs -------------------------------------------------------------------
function initTabs() {
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    [...$("#tabs").children].forEach((x) => x.classList.toggle("on", x === b));
    for (const pane of document.querySelectorAll(".tabpane"))
      pane.hidden = pane.id !== "tab-" + b.dataset.tab;
  });
}
function switchTab(name) {
  const b = [...$("#tabs").children].find((x) => x.dataset.tab === name);
  if (b) b.click();
}

// ---- bootstrap ------------------------------------------------------------
async function boot() {
  initTheme();
  initTabs();
  bootSync();
  try {
    const r = await fetch("/api/tables").then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "加载表失败");
    state.tables = r.tables;
    state.tz = r.tz;
    $("#tzNote").textContent = "时间显示时区 " + r.tz;
    const badge = $("#envBadge");
    badge.hidden = false;
    badge.textContent = r.env === "dev" ? "DEV 测试环境" : "PROD 生产";
    badge.className = "envbadge " + (r.env === "dev" ? "dev" : "prod");
    const sel = $("#tableSel");
    sel.innerHTML = "";
    for (const t of r.tables) sel.append(el("option", { value: t.id }, `${t.label}`));
    // default to 3.1 if present
    const def = r.tables.find((t) => t.label === "3.1") || r.tables[0];
    if (def) sel.value = def.id;
    await loadViews();
  } catch (e) {
    showError(e.message);
  }
  wireEvents();
  updateModeTip();
}

async function loadViews() {
  const table = $("#tableSel").value;
  const vf = $("#viewFilter");
  vf.value = "";
  $("#viewCount").textContent = "…";
  try {
    const r = await fetch("/api/views?table=" + encodeURIComponent(table)).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "加载视图失败");
    state.views = r.views;
    $("#viewCount").textContent = `(${r.views.length})`;
    $("#viewFilter").placeholder = `输入关键字过滤 ${r.views.length} 个视图…`;
    renderViewOptions("");
  } catch (e) {
    $("#viewCount").textContent = "";
    showError(e.message);
  }
}

function renderViewOptions(filter) {
  const sel = $("#viewSel");
  const f = filter.trim().toLowerCase();
  const matches = state.views.filter((v) => !f || v.view_name.toLowerCase().includes(f));
  sel.innerHTML = "";
  sel.append(el("option", { value: "" }, "全表（不限视图）"));
  for (const v of matches) sel.append(el("option", { value: v.view_id }, v.view_name));
}

// ---- events ---------------------------------------------------------------
function wireEvents() {
  $("#tableSel").addEventListener("change", loadViews);
  $("#viewFilter").addEventListener("input", (e) => renderViewOptions(e.target.value));
  $("#fieldSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.fieldKey = b.dataset.k;
    [...$("#fieldSeg").children].forEach((x) => x.classList.toggle("on", x === b));
    // sensible default operator per field
    setMode(state.fieldKey === "awb" ? "contains" : "is");
    $("#valLabel").textContent = state.fieldKey === "awb" ? "柜号" : "ISA";
    $("#valInput").placeholder = state.fieldKey === "awb" ? "例如 CAJU5283296" : "例如 146246024984";
    updateModeTip();
  });
  $("#modeSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    setMode(b.dataset.m); updateModeTip();
  });
  $("#goBtn").addEventListener("click", runQuery);
  $("#valInput").addEventListener("keydown", (e) => { if (e.key === "Enter") runQuery(); });
  wireUpload();
}

// ---- upload + parse -------------------------------------------------------
function wireUpload() {
  const drop = $("#drop"), input = $("#fileInput");
  $("#pickBtn").addEventListener("click", (e) => { e.preventDefault(); input.click(); });
  input.addEventListener("change", () => { if (input.files[0]) parseFile(input.files[0]); });
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) parseFile(f);
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
    fr.onerror = () => reject(new Error("读取文件失败"));
    fr.readAsDataURL(file);
  });
}

// POST JSON with real upload-progress events (fetch cannot report them).
function postWithProgress(url, bodyObj, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
    });
    xhr.onload = () => {
      if (xhr.status !== 200) return reject(new Error(`HTTP ${xhr.status}`));
      if (!xhr.response) return reject(new Error("服务器返回了无法解析的内容"));
      resolve(xhr.response);
    };
    xhr.onerror = () => reject(new Error("网络错误，传输中断"));
    xhr.send(JSON.stringify(bodyObj));
  });
}

async function parseFile(file) {
  const out = $("#parseOut");
  out.hidden = false;
  out.innerHTML = "";
  const ptext = el("span", {}, `读取文件 ${file.name} …`);
  const fill = el("div", { class: "pbar-fill" });
  out.append(
    el("div", { class: "filemeta" }, el("span", { class: "spinner" }), " ", ptext),
    el("div", { class: "pbar" }, fill));
  const MB = (n) => (n / 1048576).toFixed(2) + " MB";
  clog("解析", "开始", file.name, `${(file.size / 1024).toFixed(1)} KB`);
  let lastQ = 0; // last logged quarter (25% steps) — keep console terse
  try {
    const content_b64 = await fileToBase64(file);
    const r = await postWithProgress("/api/parse",
      { filename: file.name, content_b64, table: $("#tableSel").value },
      (loaded, total) => {
        const pct = Math.floor((loaded / total) * 100);
        fill.style.width = pct + "%";
        if (loaded >= total) {
          ptext.textContent = "传输完成 100%，服务器解析中…";
          fill.classList.add("indet");
        } else {
          ptext.textContent = `传输中 ${pct}%（${MB(loaded)} / ${MB(total)}）`;
        }
        const q = Math.floor(pct / 25);
        if (q > lastQ) {
          lastQ = q;
          clog("解析", `传输进度 ${Math.min(q * 25, 100)}%（${MB(loaded)} / ${MB(total)}）`);
        }
      });
    if (!r.ok) throw new Error(r.error || "解析失败");
    clog("解析", "完成", r.filename,
      `明细 ${(r.details || []).length} 条`, "识别字段:", r.fields);
    renderParsed(r);
  } catch (e) {
    cerr("解析", "失败:", e.message);
    out.innerHTML = "";
    out.append(el("div", { class: "filemeta" }, "⚠ " + e.message));
  }
}

const FIELD_ORDER = ["awb", "batch", "warehouse", "route", "appointment", "isa"];

function renderParsed(r) {
  const out = $("#parseOut");
  state.parse = r;                 // kept for the hand-off buttons
  out.innerHTML = "";
  const sheetTxt = (r.sheets || []).map((s) => `${s.name}(${s.rows}行)`).join("、");
  out.append(el("div", { class: "filemeta" },
    `${r.filename} · ${(r.size / 1024).toFixed(1)} KB · 工作表：${sheetTxt || "—"}` +
    (r.headers && r.headers.length ? ` · 表头行：第 ${r.headers[0].header_row} 行` : " · 未找到表头，已用正则扫描")));

  const grid = el("div", { class: "fields" });
  for (const key of FIELD_ORDER) {
    const f = r.fields[key] || { value: null, all: [], count: 0, source: null };
    if (key === "warehouse") { grid.append(warehouseCard(f, r.warehouse_options)); continue; }
    const card = el("div", { class: "fcard" },
      el("div", { class: "lbl" }, (r.labels && r.labels[key]) || key));
    if (f.value === null || f.value === undefined) {
      card.append(el("div", { class: "val null" }, "Null"));
    } else {
      card.append(el("div", { class: "val" }, String(f.value)));
      if (f.source) card.append(el("div", { class: "src" }, f.source));
      if (f.count > 1) {
        const more = el("div", { class: "more" }, `▸ 共识别到 ${f.count} 个不同值，点击展开`);
        const all = el("div", { class: "allvals", hidden: "" }, f.all.join("、"));
        more.addEventListener("click", () => {
          const isHidden = all.hasAttribute("hidden");
          if (isHidden) { all.removeAttribute("hidden"); more.textContent = `▾ 共 ${f.count} 个不同值`; }
          else { all.setAttribute("hidden", ""); more.textContent = `▸ 共识别到 ${f.count} 个不同值，点击展开`; }
        });
        card.append(more, all);
      }
    }
    grid.append(card);
  }
  out.append(grid);

  // one-click: feed a parsed value straight into the query above
  const awb = r.fields.awb && r.fields.awb.value;
  const isa = r.fields.isa && r.fields.isa.value;
  const row = el("div", { class: "userow" });
  const bAwb = el("button", {}, awb ? `用柜号 ${awb} 查询` : "用柜号查询（未识别）");
  bAwb.disabled = !awb;
  bAwb.addEventListener("click", () => useParsed("awb", awb));
  const bIsa = el("button", {}, isa ? `用 ISA ${isa} 查询` : "用 ISA 查询（未识别）");
  bIsa.disabled = !isa;
  bIsa.addEventListener("click", () => useParsed("isa", isa));
  row.append(bAwb, bIsa);
  out.append(row);

  renderDetails(out, r.details || []);
}

// Per-container table: each 柜号 with its own 预约号/预约时间, including
// grouped rows where one appointment covers several containers.
function renderDetails(out, details) {
  if (!details.length) return;
  const groups = new Set(details.filter((d) => d.grouped).map((d) => d.isa || "-"));
  out.append(el("div", { class: "dethead" },
    `📦 柜号明细 · ${details.length} 条` +
    (groups.size ? `（其中 ${groups.size} 个预约号为多柜分组）` : "")));

  // ---- hand-off to ①/② (this tab performs NO writes of its own) ----
  // The old 预演/上传到 5.6 buttons lived here and were a THIRD writer of
  // 5.6 + 出库计划, overlapping ①/②. Removed on purpose: parsing feeds the
  // dedicated tabs, which own their tables.
  const controls = el("div", { class: "up56" });
  const toCreate = el("button", { class: "primary" }, "→ 送到「① 新建预约」");
  toCreate.addEventListener("click", () => {
    const lines = details.filter((d) => d.isa && d.route && d.appointment)
      .map((d) => `${d.route}\t${d.isa}\t${d.appointment}`);
    if (!lines.length) { alert("没有可用行：需要同时有 目的地路线 + ISA + 预约时间"); return; }
    clog("解析", `送出 ${lines.length} 行到 ①新建预约`);
    sendToTab("create", lines);
  });
  const toSync = el("button", { class: "primary" }, "→ 送到「② 计划同步」");
  toSync.addEventListener("click", () => {
    const lines = details.filter((d) => d.awb && d.route).map((d) => {
      const pal = d.pallets != null ? d.pallets : "";
      const tail = d.isa && d.appointment ? `\t${d.isa}\t${d.appointment}` : "";
      return `${d.awb}\t${d.route}\t${pal}\t\t${tail}`.replace(/\t+$/, "");
    });
    if (!lines.length) { alert("没有可用行：需要 柜号 + 目的地路线"); return; }
    clog("解析", `送出 ${lines.length} 行到 ②计划同步（请补齐板数/箱数）`);
    sendToTab("sync", lines);
  });
  controls.append(toCreate, toSync, el("span", { class: "up56note" },
    "本页不写入任何表；② 需要的「实际板数 / 箱数」请在那边补齐后再预检"));
  out.append(controls);

  const cols = ["柜号", "目的地路线", "预约号 / ISA", "预约时间", "板数", "操作"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  for (const d of details) {
    const qBtn = el("button", { class: "mini" }, "查询");
    qBtn.addEventListener("click", () => useParsed("awb", d.awb));
    const tr = el("tr", {},
      el("td", {}, el("b", {}, d.awb)),
      el("td", {}, d.route || nullSpan()),
      el("td", {}, d.isa ? el("span", {},
        String(d.isa), d.grouped ? el("span", { class: "gtag" }, "分组") : null) : nullSpan()),
      el("td", {}, d.appointment || nullSpan()),
      el("td", {}, palletText(d)),
      el("td", {}, el("div", { class: "ops" }, qBtn)));
    tbody.append(tr);
  }
  out.append(el("div", { class: "dethint" },
    "本页只解析；写入请用上方按钮送到「① 新建预约」/「② 计划同步」执行"));
  out.append(el("div", { class: "tablewrap dettable" }, el("table", {}, thead, tbody)));
}

// NOTE: the 5.6 upload/dry-run/commit code that used to live here was removed
// 2026-08-04. It was a THIRD writer of 5.6 + 出库计划, overlapping ①新建预约
// and ②计划同步. 文件解析 now only parses and hands rows to those tabs.
// (Server side: upload_56.py and /api/upload · /api/dryrun_56 · /api/commit_56
//  were retired at the same time.)

function nullSpan() { return el("span", { class: "nullcell" }, "Null"); }
function palletText(d) {
  if (d.pallets_raw && /[^\d.\s]/.test(d.pallets_raw)) return d.pallets_raw.trim(); // 13P-9 / 4箱
  if (d.pallets != null) return d.pallets + "P";
  if (d.pallets_raw) return d.pallets_raw.trim();
  return "—";
}

// 仓库供应商 card with a dropdown.
//   * parsed value is Null  -> dropdown ENABLED (manual pick) + console.log("Null")
//   * parsed value present   -> dropdown shows it and is DISABLED (locked from file)
function warehouseCard(f, options) {
  const isNull = f.value === null || f.value === undefined || f.value === "";
  const card = el("div", { class: "fcard" }, el("div", { class: "lbl" }, "仓库供应商"));

  const sel = el("select", { class: "whsel" });
  sel.append(el("option", { value: "" }, "（请选择仓库供应商）"));
  const opts = Array.isArray(options) ? options.slice() : [];
  if (!isNull && !opts.includes(f.value)) opts.unshift(f.value); // ensure it's selectable to show
  for (const o of opts) sel.append(el("option", { value: o }, o));

  if (isNull) {
    sel.disabled = false;          // 是 null -> 可选择
    sel.value = "";
    console.log("Null");           // 要求：仓库供应商为 null 时 console 返回 Null
    sel.addEventListener("change", () => {
      console.log("仓库供应商 已选择:", sel.value || "(未选)");
    });
    card.append(sel);
    card.append(el("div", { class: "src nullnote" }, "未从文件识别（Null）· 可手动选择"));
  } else {
    sel.value = f.value;
    sel.disabled = true;           // 不是 null -> 不可选择（来自文件，已锁定）
    card.append(sel);
    card.append(el("div", { class: "src" }, (f.source ? f.source + " · " : "") + "来自文件，已锁定"));
  }
  return card;
}

function useParsed(kind, value) {
  switchTab("query");   // 上传页的「查询」按钮跳到查询页执行
  [...$("#fieldSeg").children].find((b) => b.dataset.k === kind).click();
  $("#valInput").value = value;
  runQuery();
  $("#valInput").scrollIntoView({ behavior: "smooth", block: "center" });
}

function setMode(m) {
  state.mode = m;
  [...$("#modeSeg").children].forEach((x) => x.classList.toggle("on", x.dataset.m === m));
}

function updateModeTip() {
  const isAwb = state.fieldKey === "awb";
  let t = "";
  if (isAwb && state.mode === "contains")
    t = "提示：柜号在表里常带后缀（如 CAJU5283296A / …B），用「包含」可一并查到；要精确到某个柜号再切「精确」。";
  else if (isAwb) t = "精确匹配：仅返回柜号完全相等的行（不含带后缀的拆柜）。";
  else if (state.mode === "is") t = "按 ISA 精确匹配 3.1 中的记录。";
  else t = "按 ISA 模糊包含匹配。";
  $("#modeTip").textContent = t;
}

// ---- query + render -------------------------------------------------------
async function runQuery() {
  const value = $("#valInput").value.trim();
  if (!value) { $("#valInput").focus(); return; }
  const body = {
    table: $("#tableSel").value,
    view_id: $("#viewSel").value || null,
    field_key: state.fieldKey,
    value,
    mode: state.mode,
  };
  const btn = $("#goBtn");
  btn.disabled = true; btn.textContent = "查询中…";
  setStatus([el("span", {}, el("span", { class: "spinner" }), " 正在从飞书拉取…")], false);
  $("#results").innerHTML = "";
  clog("查询", "开始", `${state.fieldKey}=${value}`, `模式=${state.mode}`);
  try {
    const r = await fetch("/api/query", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "查询失败");
    clog("查询", "完成", `命中 ${r.count} 行（全表 ${r.total_found}）`,
      r.matched && r.matched.length ? `匹配值: ${r.matched.join(", ")}` : "");
    renderResults(r);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false; btn.textContent = "查询";
  }
}

function setStatus(nodes, isErr) {
  const s = $("#status");
  s.hidden = false; s.className = "status" + (isErr ? " err" : "");
  s.innerHTML = ""; nodes.forEach((n) => s.append(n));
}
function showError(msg) { cerr("错误", msg); setStatus([el("span", {}, "⚠ " + msg)], true); }

function renderResults(r) {
  const viewName = viewLabel(r.query.view_id);
  const bits = [
    el("span", { class: "pill" }, "命中 ", el("b", {}, String(r.count)), " 行"),
    el("span", { class: "pill" }, el("span", { class: "k" }, "字段 "), `${r.query.field} · ${r.query.operator === "contains" ? "包含" : "精确"} · “${r.query.value}”`),
    el("span", { class: "pill" }, el("span", { class: "k" }, "视图 "), viewName),
  ];
  if (r.view_filter) {
    bits.push(el("span", { class: "pill" }, el("span", { class: "k" }, "视图过滤 "),
      `全表 ${r.total_found} → 视图内 ${r.count}（排除 ${r.view_filter.excluded}）`));
    if (r.view_filter.unsupported && r.view_filter.unsupported.length) {
      bits.push(el("span", { class: "pill warnpill" },
        "⚠ 无法本地判定的条件：" + r.view_filter.unsupported.join("、") + "（结果可能偏宽）"));
    }
  }
  if (r.matched && r.matched.length)
    bits.push(el("span", { class: "pill" }, el("span", { class: "k" }, "匹配值 "), r.matched.join(" , ")));
  setStatus(bits, false);

  const results = $("#results");
  results.innerHTML = "";
  if (!r.count) {
    const why = r.view_filter && r.total_found
      ? `全表命中 ${r.total_found} 行，但都不在视图「${viewName}」的过滤条件内。换成「全表」或其它视图再试。`
      : "没有命中记录。可尝试：切换「包含」匹配，或确认柜号/ISA 是否正确。";
    results.append(el("div", { class: "empty" }, why));
    return;
  }

  // build name->display map per row
  const rows = r.rows.map((row) => {
    const map = {};
    for (const f of row.fields) map[f.name] = f.display;
    return { record_id: row.record_id, fields: row.fields, map };
  });

  // choose columns
  let cols = PREFERRED.filter((c) => rows.some((row) => row.map[c] !== undefined));
  if (cols.length < 3) {
    const freq = {};
    rows.forEach((row) => row.fields.forEach((f) => (freq[f.name] = (freq[f.name] || 0) + 1)));
    cols = Object.keys(freq).sort((a, b) => freq[b] - freq[a]).slice(0, 8);
  }

  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  rows.forEach((row, i) => {
    const tr = el("tr", { class: "row" });
    cols.forEach((c) => {
      const val = row.map[c];
      let cell;
      if (c === "派送状态" && val) cell = el("td", {}, el("span", { class: "chip " + statusChipClass(val) }, val));
      else if (c === "POD附件") cell = el("td", {}, val ? el("span", { class: "pod" }, "📎 有") : el("span", { class: "nopod" }, "—"));
      else cell = el("td", {}, val !== undefined ? clip(val) : "—");
      tr.append(cell);
    });
    const detail = el("tr", { class: "detail", hidden: "" },
      el("td", { colspan: String(cols.length) },
        el("div", { class: "detail-inner" },
          el("div", { class: "rid" }, "record_id: " + row.record_id + " · 全部非空字段 " + row.fields.length),
          el("div", { class: "dl" }, ...row.fields.map((f) =>
            el("div", { class: "item" }, el("span", { class: "n" }, f.name), el("span", { class: "v" }, f.display)))))));
    tr.addEventListener("click", () => {
      const open = detail.hasAttribute("hidden") ? false : true;
      if (open) { detail.setAttribute("hidden", ""); tr.classList.remove("open"); }
      else { detail.removeAttribute("hidden"); tr.classList.add("open"); }
    });
    tbody.append(tr, detail);
  });

  results.append(el("div", { class: "tablewrap" }, el("table", {}, thead, tbody)));
}

function viewLabel(id) {
  if (!id) return "全表";
  const v = state.views.find((x) => x.view_id === id);
  return v ? v.view_name : id;
}
function clip(s) { s = String(s); return s.length > 60 ? s.slice(0, 58) + "…" : s; }

/* ═══════════════════════════════════════════════════════════════════════════
   ⚡ 预约同步 (appointment sync) — batch paste -> READ-ONLY plan -> explicit
   execute. Client-side validation mirrors webapp/appointment_sync.parse_line
   for INSTANT feedback; the server re-validates authoritatively.
   ═══════════════════════════════════════════════════════════════════════════ */
const sync = { meta: null, warehouse: null, plan: null, busy: false };

// Destination: SHAPE check only — the real list is the LIVE 5.6 目的地
// options delivered by /api/sync/meta (sync.meta.dest_options). Never
// hardcode destinations here: XCAB etc. are managed in Lark, not in code.
const RE_DEST = /^[A-Z][A-Z0-9•\-]{1,7}$/;
const RE_ISA = /^\d{8,15}$/;
const RE_TIME = /^(\d{1,4})[/-](\d{1,2})[/-](\d{1,4})\s+\d{1,2}:\d{2}$/;

// Membership test against the live options; degrades to the shape check
// while meta is still loading (server re-validates at 预检 regardless).
function destKnown(d) {
  const opts = sync.meta && sync.meta.dest_options;
  return opts && opts.length ? opts.includes(d) : true;
}

// Mirror of parse_line(): returns {ok, msg} per line — same rejection rules.
function checkLine(raw) {
  const line = raw.replace(/ |　/g, " ").trim();
  if (!line) return null;                       // blank lines are skipped
  if (raw.includes("\t")) {
    const cells = raw.split("\t").map((c) => c.trim());
    while (cells.length && cells[cells.length - 1] === "") cells.pop();
    if (cells.includes(""))
      return { ok: false, msg: "存在空列 — 最后两列必须是 [实际板数] [箱数]" };
  }
  const t = line.split(/\s+/);
  if (t.length < 4) return { ok: false, msg: `只有 ${t.length} 列，需要 [柜号] [路线] [板数] [箱数]` };
  if (t.length === 5 || t.length === 6 || t.length > 8)
    return { ok: false, msg: "带预约时格式应为 [柜号] [路线] [板数] [箱数] [ISA] [日期] [时间] [时区可选]" };
  if (!RE_DEST.test(t[1].toUpperCase()))
    return { ok: false, msg: `路线「${t[1]}」格式不像仓点代码（如 YEG2 / XCAB）` };
  if (!/^\d+$/.test(t[2]) || !/^\d+$/.test(t[3]))
    return { ok: false, msg: "最后两列必须是 [实际板数] [箱数] 两个数字" };
  if (t[2].length >= 9 || t[3].length >= 9)
    return { ok: false, msg: "疑似 ISA 出现在板数/箱数位置 — 板数或箱数缺失？" };
  if (+t[2] < 1 || +t[2] > 99) return { ok: false, msg: `实际板数 ${t[2]} 超出 1-99` };
  if (t.length >= 7) {
    if (!RE_ISA.test(t[4])) return { ok: false, msg: `ISA「${t[4]}」应为 8-15 位数字` };
    if (!RE_TIME.test(t[5] + " " + t[6]))
      return { ok: false, msg: `预约时间「${t[5]} ${t[6]}」应为 MM/DD/YYYY HH:MM` };
    if (t.length === 8 && !/^[A-Z]{2,4}$/i.test(t[7]))
      return { ok: false, msg: `时区「${t[7]}」无法识别` };
    return { ok: true, msg: `✓ ${t[0]} → ${t[1].toUpperCase()} · ${t[2]}板 ${t[3]}箱 · ISA ${t[4]} @ ${t[5]} ${t[6]}${t[7] ? " " + t[7] : ""}` };
  }
  return { ok: true, msg: `✓ ${t[0]} → ${t[1].toUpperCase()} · ${t[2]}板 ${t[3]}箱（无预约信息 — 仅核对）` };
}

async function bootSync() {
  try {
    const r = await fetch("/api/sync/meta").then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "加载失败");
    sync.meta = r;
    const seg = $("#whSeg");
    seg.innerHTML = "";
    for (const w of r.warehouses) {
      const note = !w.mapped ? "未配置预约流程（仅查询+板数核对）"
        : !w.trip_enabled ? `DEV 环境无 ${w.plan_table} 副本 — 预约/行程停用（仅板数核对）`
        : `预约账号 ${w.account} · ${w.plan_table}`;
      const b = el("button", { "data-w": w.key, title: note },
        w.label + (w.mapped && w.trip_enabled ? "" : " ⚠"));
      seg.append(b);
    }
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      sync.warehouse = b.dataset.w;
      [...seg.children].forEach((x) => x.classList.toggle("on", x === b));
      const w = r.warehouses.find((x) => x.key === sync.warehouse);
      $("#whTip").textContent = !w.mapped
        ? "⚠ 该仓库未配置预约账号/出库计划表 — 本次仅执行 3.1 查询与实际板数核对"
        : !w.trip_enabled
        ? `⚠ DEV 环境无「${w.plan_table}」副本表 — 预约/行程操作停用，仅板数核对（复制该表后可启用）`
        : `预约账号：${w.account} · 出库计划表：${w.plan_table}`;
      gateSyncPlan();
    });

    // ---- 新建预约 tab: its own warehouse selector (账号 mapping only) ----
    const seg56 = $("#whSeg56");
    seg56.innerHTML = "";
    for (const w of r.warehouses) {
      const b = el("button", { "data-w": w.key, title: w.account
        ? `预约账号 ${w.account}` : "未配置预约账号 — 无法创建" },
        w.label + (w.account ? "" : " ⚠"));
      seg56.append(b);
    }
    seg56.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      create56.warehouse = b.dataset.w;
      [...seg56.children].forEach((x) => x.classList.toggle("on", x === b));
      const w = r.warehouses.find((x) => x.key === create56.warehouse);
      $("#whTip56").textContent = w.account
        ? `将以预约账号「${w.account}」创建缺失的 5.6 预约`
        : "⚠ 该仓库未配置预约账号 — 所有行将被拦截";
      gateCreatePlan();
    });

    // ---- ③核对 tab: warehouse selector ----
    const segV = $("#whSegV");
    segV.innerHTML = "";
    for (const w of r.warehouses) {
      segV.append(el("button", { "data-w": w.key, title: w.plan_table || "无出库计划表" },
        w.label + (w.trip_enabled ? "" : " ⚠")));
    }
    segV.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      verifyState.warehouse = b.dataset.w;
      [...segV.children].forEach((x) => x.classList.toggle("on", x === b));
      const w = r.warehouses.find((x) => x.key === verifyState.warehouse);
      $("#whTipV").textContent = w.trip_enabled
        ? `核对链路：3.1 → ${w.plan_table} → 5.6（应有账号 ${w.account}）`
        : `⚠ ${w.plan_table ? "DEV 环境无该出库计划表副本" : "该仓库无出库计划表"} — 无法核对`;
      gateVerify();
    });
  } catch (e) {
    $("#whTip").textContent = "⚠ " + e.message;
  }
  const ta = $("#syncInput");
  ta.addEventListener("input", () => { renderLineChecks(); gateSyncPlan(); });
  // Tab key inserts a real \t (matching Excel-pasted rows) instead of moving focus.
  ta.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    const [s, epos] = [ta.selectionStart, ta.selectionEnd];
    ta.value = ta.value.slice(0, s) + "\t" + ta.value.slice(epos);
    ta.selectionStart = ta.selectionEnd = s + 1;
    renderLineChecks(); gateSyncPlan();
  });
  $("#syncPlanBtn").addEventListener("click", planSync);
  $("#syncExecBtn").addEventListener("click", execSync);
  $("#chkAll").addEventListener("change", (e) => {
    document.querySelectorAll("#syncResults input.rowchk:not(:disabled)")
      .forEach((c) => { c.checked = e.target.checked; });
    updateExecCount();
  });

  // ---- 新建预约 tab wiring ----
  const cta = $("#createInput");
  cta.addEventListener("input", () => { renderCreateLineChecks(); gateCreatePlan(); });
  cta.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    const [s, epos] = [cta.selectionStart, cta.selectionEnd];
    cta.value = cta.value.slice(0, s) + "\t" + cta.value.slice(epos);
    cta.selectionStart = cta.selectionEnd = s + 1;
    renderCreateLineChecks(); gateCreatePlan();
  });
  $("#createPlanBtn").addEventListener("click", planCreate56);
  $("#createExecBtn").addEventListener("click", execCreate56);

  // ---- ③核对 tab wiring ----
  $("#verifyInput").addEventListener("input", gateVerify);
  $("#verifyBtn").addEventListener("click", runVerify);

  reattachJob();   // resume a job that was running when the page reloaded
}

function lineChecks() {
  return $("#syncInput").value.split("\n")
    .map((raw, i) => ({ n: i + 1, raw, res: checkLine(raw) }))
    .filter((x) => x.res !== null);
}

function renderLineChecks() {
  const mount = $("#syncLines");
  mount.innerHTML = "";
  for (const { n, res } of lineChecks()) {
    mount.append(el("div", { class: "linechk " + (res.ok ? "ok" : "bad") },
      el("span", { class: "ln" }, "第" + n + "行"), res.msg));
  }
}

// 查询按钮门槛：已选仓库 + 至少一行 + 所有行通过校验（按规范：格式修好前不处理）。
function gateSyncPlan() {
  const checks = lineChecks();
  const bad = checks.filter((c) => !c.res.ok).length;
  const ok = !!sync.warehouse && checks.length > 0 && bad === 0 && !sync.busy;
  $("#syncPlanBtn").disabled = !ok;
  $("#syncGoTip").textContent = !sync.warehouse ? "请先选择仓库供应商"
    : checks.length === 0 ? "请粘贴到仓明细"
    : bad ? `有 ${bad} 行格式错误 — 修正或删除后才能查询` : "只读预检，不写入任何数据";
}

/* ---- job polling & progress panel ----------------------------------------
   plan/commit run as SERVER-SIDE background jobs (they are long chains of
   Feishu calls). The POST returns a job id immediately; we poll ~2×/s for
   {stage, done/total, current row, elapsed} so the operator always sees that
   — and what — is running. Poll failures don't kill the job: we keep trying
   and say so. */
const POLL_MS = 500;

function showProgress(kind) {
  $("#syncProgress").hidden = false;
  $("#progStage").textContent = kind === "commit" ? "提交写入任务…" : "启动预检…";
  $("#progCount").textContent = "";
  $("#progElapsed").textContent = "";
  $("#progCurrent").textContent = "";
  $("#progFill").style.width = "0%";
  $("#progFill").classList.add("indet");
}

function updateProgress(job, netTrouble) {
  $("#progStage").textContent = job.stage || "…";
  const { done, total } = job;
  if (total > 0) {
    $("#progFill").classList.remove("indet");
    $("#progFill").style.width = Math.round((done / total) * 100) + "%";
    $("#progCount").textContent = `${done} / ${total} 行`;
  } else {
    $("#progFill").classList.add("indet");   // batch write phases: no row count
    $("#progCount").textContent = "";
  }
  $("#progElapsed").textContent = `已用 ${Math.round(job.elapsed)} 秒`;
  $("#progCurrent").textContent = netTrouble
    ? "⚠ 服务器暂时无响应，继续重试中…（任务仍在服务端运行）"
    : (job.current || "");
}

function hideProgress() { $("#syncProgress").hidden = true; }

// Poll until the job reaches a terminal state. Tolerates transient poll
// failures (server busy / brief network blips) — the job itself is unaffected.
async function pollJob(jobId, kind) {
  let failures = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    let r;
    try {
      r = await fetch("/api/sync/job?id=" + encodeURIComponent(jobId))
        .then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "poll failed");
      failures = 0;
    } catch (e) {
      failures++;
      if (failures >= 120)   // ~1 min of continuous failure: give up the WAIT
        throw new Error("与服务器失去联系超过 1 分钟 — 任务可能仍在执行，"
          + "请稍后刷新页面查看结果");
      updateProgress({ stage: "连接中断，重试…", done: 0, total: 0,
        elapsed: 0, current: "" }, true);
      continue;
    }
    const job = r.job;
    updateProgress(job, false);
    if (job.state === "done") return job;
    if (job.state === "error") throw new Error(job.error || "任务失败");
  }
}

function rememberJob(id, kind) { sessionStorage.setItem("larkSyncJob", JSON.stringify({ id, kind })); }
function forgetJob() { sessionStorage.removeItem("larkSyncJob"); }

function renderPlanOutcome(r) {
  sync.plan = r;
  const s = r.summary;
  clog("预检", "完成（只读）", `行=${s.lines} 可执行=${s.actionable} 警告=${s.warnings} 拦截=${s.blocked}`);
  setSyncStatus([
    el("span", { class: "pill" }, "共 ", el("b", {}, String(s.lines)), " 行"),
    el("span", { class: "pill" }, "可执行 ", el("b", {}, String(s.actionable))),
    el("span", { class: "pill" + (s.warnings ? " warnpill" : "") }, `⚠ 警告 ${s.warnings}`),
    el("span", { class: "pill" + (s.blocked || s.match_errors || s.parse_errors ? " warnpill" : "") },
      `⛔ 拦截/错误 ${s.blocked + s.match_errors + s.parse_errors}`),
    el("span", { class: "pill" }, `账号 ${r.account || "—"} · 计划表 ${r.plan_table || "—"} · 环境 ${r.env.toUpperCase()}`),
  ]);
  renderSyncRows(r, false);
}

async function planSync() {
  const btn = $("#syncPlanBtn");
  sync.busy = true; btn.disabled = true; btn.textContent = "查询中…";
  $("#execBar").hidden = true;
  $("#syncWarnings").hidden = true;
  $("#syncResults").innerHTML = "";
  $("#syncStatus").hidden = true;
  showProgress("plan");
  clog("预检", "开始", `仓库=${sync.warehouse}`);
  try {
    const r = await fetch("/api/sync/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warehouse: sync.warehouse, text: $("#syncInput").value }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "预检失败");
    rememberJob(r.job.id, "plan");
    const job = await pollJob(r.job.id, "plan");
    forgetJob();
    hideProgress();
    renderPlanOutcome(job.result);
  } catch (e) {
    hideProgress();
    cerr("预检", "失败:", e.message);
    setSyncStatus([el("span", {}, "⚠ " + e.message)], true);
  } finally {
    sync.busy = false;
    btn.textContent = "查询并预检（只读）";
    gateSyncPlan();
    // renderSyncRows() ran while busy was still true, so updateExecCount()
    // disabled 执行. Re-sync it now that busy is cleared — otherwise the
    // button stays greyed out until a checkbox is clicked.
    updateExecCount();
  }
}

function setSyncStatus(nodes, isErr) {
  const s = $("#syncStatus");
  s.hidden = false; s.className = "status" + (isErr ? " err" : "");
  s.innerHTML = ""; nodes.forEach((n) => s.append(n));
}

// Action chips. NOTE: these are 出库计划 (delivery-plan) operations — this tab
// never creates 预约/appointments (that's ①新建预约). Wording matters: an
// earlier draft said 「新建行程」 here, which read as "creating appointments".
const ACTION_LABEL = {
  fill_pallets: (a) => `填 3.1 实际板数=${a.value}`,
  update_isa_time: (a) => `更新已关联预约 → ISA ${a.isa} @ ${a.time}`,
  link_trip: () => "挂到出库计划",
  create_trip: () => "新建出库计划(5.x)",
  // attach = the 出库计划 had no appointment; relink = newest-wins repoint
  // onto an EXISTING 5.6 record carrying the pasted ISA
  set_trip_isa: (a) => a.mode === "relink" ? "改挂到已有预约" : "出库计划补挂预约",
};
const PLAN_LABEL = {
  has_plan_match: ["ok", "✓ 计划一致"],
  has_plan_mismatch: ["warn", "计划不一致"],
  has_plan: ["neu", "已有出库计划"],
  has_plan_no_isa: ["warn", "出库计划缺预约"],
  no_plan: ["bad", "无出库计划"],
  no_plan_table: ["neu", "不适用"],
  no_dev_plan_table: ["neu", "DEV 停用"],
  isa_missing: ["bad", "预约不存在"],
  link_existing: ["brand", "挂到现有出库计划"],
  create_trip: ["brand", "建出库计划(预约已存在)"],
  already_on_trip: ["ok", "✓ 已在出库计划中"],
};

function planCell(row) {
  const p = row.plan || {};
  const [cls, label] = PLAN_LABEL[p.status] || ["neu", p.status || "—"];
  const bits = [el("span", { class: "chip " + cls }, label)];
  if (p.current_isa) bits.push(el("div", { class: "sub" },
    `现挂 ISA ${p.current_isa} @ ${p.current_time || "无时间"}`));
  if (p.isa_record_id && !p.current_isa) bits.push(el("div", { class: "sub" },
    "复用已存在的预约"));
  if (p.trip_total !== undefined && p.trip_total !== null)
    bits.push(el("div", { class: "sub" }, `出库计划合计约 ${p.trip_total} 板`));
  return el("div", {}, ...bits);
}

function renderSyncRows(r, committed) {
  const mount = $("#syncResults");
  mount.innerHTML = "";
  const cols = ["", "行", "柜号", "路线", "板数", "箱数", "ISA / 时间", "3.1 匹配", "派送计划", "动作", "警告 / 说明"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");

  for (const row of r.rows) {
    const p = row.parsed || {};
    const m = row.match || {};
    const err = row.parse_error || row.match_error;
    const actionable = row.actions.length > 0 && row.blockers.length === 0 && !err;

    // ---- checkbox ----
    const chk = el("input", { type: "checkbox", class: "rowchk", "data-line": row.line_no });
    if (!actionable || committed) chk.disabled = true;
    else chk.checked = true;
    chk.addEventListener("change", updateExecCount);

    // ---- pallets cell: 提供 / 预计 / 现有 ----
    const palBits = [String(p.pallets ?? "—")];
    if (m.estimated != null) palBits.push(`预计 ${m.estimated}`);
    if (m.actual_existing) palBits.push(`现值「${m.actual_existing}」`);
    const palletCellNode = el("div", {}, palBits[0],
      ...palBits.slice(1).map((t) => el("div", { class: "sub" }, t)));

    // ---- boxes cell ----
    const boxNode = el("div", {}, String(p.boxes ?? "—"),
      m.boxes != null ? el("div", { class: "sub" + (Number(m.boxes) !== p.boxes ? " subbad" : "") },
        `3.1: ${m.boxes}`) : null);

    // ---- ISA/time ----
    const isaNode = p.isa
      ? el("div", {}, String(p.isa), el("div", { class: "sub" }, `${p.time}${p.tz ? " " + p.tz : ""}`))
      : el("span", { class: "nullcell" }, "—");

    // ---- match cell ----
    const matchNode = err
      ? el("span", { class: "chip bad", title: err }, row.parse_error ? "格式错误" : "匹配失败")
      : el("div", {}, el("span", { class: "chip ok" }, "✓ " + (m.awb || "")),
          m.batch ? el("div", { class: "sub" }, m.batch) : null);

    // ---- actions ----
    let actNode;
    const c = row.commit || {};
    if (committed && row.approved) {
      if (c.error) actNode = el("span", { class: "chip bad", title: c.error }, "✗ 失败");
      else if (c.skipped) actNode = el("span", { class: "chip warn", title: c.skipped }, "跳过");
      else if (c.verified === true) actNode = el("span", { class: "chip ok" }, "✓ 已写入并回读核实");
      else if (c.verified === false) actNode = el("span", { class: "chip bad" }, "⚠ 写入后核实未通过");
      else actNode = el("span", { class: "chip neu" }, "无需改动");
    } else if (err) {
      actNode = el("span", { class: "nullcell" }, "—");
    } else if (row.blockers.length) {
      actNode = el("div", {}, ...row.blockers.map((b) => el("div", { class: "chip bad", title: b }, "⛔ " + clip(b))));
    } else if (!row.actions.length) {
      actNode = el("span", { class: "chip neu" }, "无需改动");
    } else {
      actNode = el("div", { class: "actlist" }, ...row.actions.map((a) =>
        el("div", { class: "chip brand" }, (ACTION_LABEL[a.type] || (() => a.type))(a))));
    }

    // ---- warnings / notes ----
    const wNode = el("div", { class: "warnlist" },
      ...row.warnings.map((w) => el("div", { class: "wline" }, "⚠ " + w)),
      ...(row.notes || []).map((n) => el("div", { class: "nline" }, n)),
      err ? el("div", { class: "wline bad" }, "✗ " + err) : null,
      ...(committed && c.checks ? c.checks.map((ck) =>
        el("div", { class: ck.ok ? "nline" : "wline" },
          `${ck.ok ? "✓" : "✗"} ${ck.what}${ck.ok ? "" : `（回读=${ck.got}）`}`)) : []));

    const tr = el("tr", { class: err ? "rowbad" : row.warnings.length ? "rowwarn" : "" },
      el("td", {}, chk),
      el("td", {}, String(row.line_no)),
      el("td", {}, el("b", {}, p.awb || "?")),
      el("td", {}, p.dest || "—"),
      el("td", {}, palletCellNode),
      el("td", {}, boxNode),
      el("td", {}, isaNode),
      el("td", {}, matchNode),
      el("td", {}, err ? el("span", { class: "nullcell" }, "—") : planCell(row)),
      el("td", {}, actNode),
      el("td", { class: "wcol" }, wNode));
    tbody.append(tr);
  }
  mount.append(el("div", { class: "tablewrap" }, el("table", { class: "synctable" }, thead, tbody)));

  if (!committed) {
    const n = r.rows.filter((x) => x.actions.length && !x.blockers.length
      && !x.parse_error && !x.match_error).length;
    $("#execBar").hidden = n === 0;
    updateExecCount();
  } else {
    $("#execBar").hidden = true;
  }
}

function updateExecCount() {
  const n = document.querySelectorAll("#syncResults input.rowchk:checked:not(:disabled)").length;
  $("#syncExecBtn").textContent = `执行选中更新（${n} 行）`;
  $("#syncExecBtn").disabled = n === 0 || sync.busy;
  $("#execTip").textContent = n ? "将按左侧勾选执行 — 双重确认后才写入" : "没有勾选可执行的行";
}

function execSync() {
  if (!sync.plan) return;
  const approvals = [...document.querySelectorAll("#syncResults input.rowchk:checked:not(:disabled)")]
    .map((c) => Number(c.dataset.line))
    .map((n) => {
      const row = sync.plan.rows.find((x) => x.line_no === n);
      return { line_no: n, sig: row.sig };
    });
  if (!approvals.length) return;

  // Confirmation summary: exactly what will be written, grouped by type.
  const counts = {};
  for (const a of approvals) {
    const row = sync.plan.rows.find((x) => x.line_no === a.line_no);
    for (const act of row.actions)
      counts[act.type] = (counts[act.type] || 0) + 1;
  }
  const NAMES = { fill_pallets: "填 3.1 实际板数", update_isa_time: "更新已关联预约ISA/时间",
    link_trip: "挂到出库计划", create_trip: "新建出库计划(5.x)",
    set_trip_isa: "出库计划挂/改挂预约" };
  const lines = Object.entries(counts).map(([k, v]) => `  · ${NAMES[k] || k} × ${v}`);
  const envLabel = sync.plan.env === "dev" ? "DEV 测试环境（dev 副本表）" : "‼ PROD 生产环境";
  if (!confirm(`确认执行以下写入？\n\n环境：${envLabel}\n仓库：${sync.plan.warehouse}` +
    `（账号 ${sync.plan.account || "—"}）\n\n${lines.join("\n")}\n\n共 ${approvals.length} 行。`))
    return;

  runCommit(approvals);
}

function renderCommitOutcome(r) {
  sync.plan = r;
  const done = r.rows.filter((x) => (x.commit || {}).done && !(x.commit || {}).skipped).length;
  const verified = r.rows.filter((x) => (x.commit || {}).verified === true).length;
  const failed = r.rows.filter((x) => (x.commit || {}).error).length;
  const skipped = r.rows.filter((x) => (x.commit || {}).skipped).length;
  (failed ? cerr : clog)("执行", "完成", `成功=${done} 回读核实=${verified} 失败=${failed} 跳过=${skipped}`);
  setSyncStatus([
    el("span", { class: "pill" }, "✅ 已执行 ", el("b", {}, String(done))),
    el("span", { class: "pill" }, "回读核实 ", el("b", {}, String(verified))),
    failed ? el("span", { class: "pill warnpill" }, `✗ 失败 ${failed}`) : null,
    skipped ? el("span", { class: "pill warnpill" }, `跳过 ${skipped}`) : null,
  ].filter(Boolean), failed > 0);
  renderSyncRows(r, true);
  renderWarningsSummary(r);
}

// warn before closing the page while a COMMIT is in flight (the job keeps
// running server-side; reopening re-attaches, but the operator should know)
window.addEventListener("beforeunload", (e) => {
  const j = sessionStorage.getItem("larkSyncJob");
  const kind = j ? JSON.parse(j).kind : null;
  if ((kind === "commit" && sync.busy) || (kind === "c56commit" && create56.busy)) {
    e.preventDefault();
    e.returnValue = "";
  }
});

async function runCommit(approvals) {
  const btn = $("#syncExecBtn");
  sync.busy = true; btn.disabled = true; btn.textContent = "执行中…";
  $("#syncStatus").hidden = true;
  showProgress("commit");
  clog("执行", "开始", `行=${approvals.length}`, `环境=${sync.plan.env}`);
  try {
    const r = await fetch("/api/sync/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warehouse: sync.warehouse, text: $("#syncInput").value,
        approvals, env: sync.plan.env }),
    }).then((x) => x.json());
    if (!r.ok) {
      if (r.busy) {
        // another commit is running (other tab / earlier click) — attach to
        // it for visibility instead of failing into the void
        cerr("执行", r.error);
        setSyncStatus([el("span", {}, "⚠ " + r.error + " — 已跟踪其进度")], true);
        const latest = await fetch("/api/sync/job?id=latest").then((x) => x.json());
        if (latest.ok && latest.job.state === "running")
          await pollJob(latest.job.id, "commit").catch(() => {});
        hideProgress();
        sync.busy = false; updateExecCount();
        return;
      }
      throw new Error(r.error || "执行失败");
    }
    rememberJob(r.job.id, "commit");
    const job = await pollJob(r.job.id, "commit");
    forgetJob();
    hideProgress();
    renderCommitOutcome(job.result);
  } catch (e) {
    hideProgress();
    cerr("执行", "失败:", e.message);
    setSyncStatus([el("span", {}, "⚠ 执行失败：" + e.message)], true);
    sync.busy = false; updateExecCount();
    return;
  }
  sync.busy = false;
}

// Page-refresh re-attach: if a job was in flight when the tab reloaded,
// resume polling it and render its result when it lands.
async function reattachJob() {
  const raw = sessionStorage.getItem("larkSyncJob");
  if (!raw) return;
  let saved;
  try { saved = JSON.parse(raw); } catch { forgetJob(); return; }
  let r;
  try {
    r = await fetch("/api/sync/job?id=" + encodeURIComponent(saved.id))
      .then((x) => x.json());
  } catch { return; }        // server unreachable — keep the id for next load
  if (!r.ok) { forgetJob(); return; }
  const job = r.job;
  if (job.state === "running") {
    const isC56 = saved.kind.startsWith("c56");
    const isCommit = saved.kind.endsWith("commit");
    clog("恢复", `重新连接到进行中的${isCommit ? "写入" : "预检"}任务`, job.id);
    if (isC56) { switchTab("create"); create56.busy = true; }
    else sync.busy = true;
    showProgress(isCommit ? "commit" : "plan");
    updateProgress(job, false);
    try {
      const finished = await pollJob(job.id, saved.kind);
      hideProgress();
      if (saved.kind === "commit") renderCommitOutcome(finished.result);
      else if (saved.kind === "plan") renderPlanOutcome(finished.result);
      else renderC56(finished.result, saved.kind === "c56commit");
      const wkey = finished.result && finished.result.warehouse;
      if (wkey && !isC56) restoreWarehouse(wkey);
      if (wkey && isC56) {
        const b = [...$("#whSeg56").children].find((x) => x.dataset.w === wkey);
        if (b && !b.classList.contains("on")) b.click();
      }
    } catch (e) {
      hideProgress();
      (isC56 ? setCreateStatus : setSyncStatus)([el("span", {}, "⚠ " + e.message)], true);
    }
    forgetJob();
    if (isC56) create56.busy = false; else sync.busy = false;
    gateSyncPlan();
    gateCreatePlan();
    updateExecCount();       // clear the busy-time disable (see planSync)
    updateCreateExec();
  } else {
    forgetJob();             // finished while we were away — results were seen
  }
}

function restoreWarehouse(key) {
  const seg = $("#whSeg");
  const b = [...seg.children].find((x) => x.dataset.w === key);
  if (b && !b.classList.contains("on")) b.click();
}

/* ═══════════════════════════════════════════════════════════════════════════
   ➕ 新建预约（仅 5.6）— paste [目的地] [ISA] [时间] lines, create the ISAs
   that don't exist yet. Mirrors webapp/appointment_create.parse_line for
   instant validation; server re-validates. Reuses the shared job progress.
   ═══════════════════════════════════════════════════════════════════════════ */
const create56 = { warehouse: null, plan: null, busy: false };

function checkCreateLine(raw) {
  const line = raw.replace(/ |　/g, " ").trim();
  if (!line) return null;
  const t = line.split(/\s+/);
  if (t.length < 4 || t.length > 5)
    return { ok: false, msg: `列数 ${t.length} 无法解析 — 需要 [目的地] [ISA] [日期] [时间] [时区可选]` };
  if (!RE_DEST.test(t[0].toUpperCase()))
    return { ok: false, msg: `目的地「${t[0]}」格式不像仓点代码（如 YEG2 / XCAB）` };
  if (!destKnown(t[0].toUpperCase()))
    return { ok: false, msg: `目的地「${t[0]}」不是 5.6 的现有选项 — `
      + "如需新仓点请先在飞书 5.6 表的「目的地」字段添加选项" };
  if (!RE_ISA.test(t[1]))
    return { ok: false, msg: `ISA「${t[1]}」应为 8-15 位数字` };
  if (!RE_TIME.test(t[2] + " " + t[3]))
    return { ok: false, msg: `预约时间「${t[2]} ${t[3]}」应为 MM/DD/YYYY HH:MM` };
  if (t.length === 5 && !/^[A-Z]{2,4}$/i.test(t[4]))
    return { ok: false, msg: `时区「${t[4]}」无法识别` };
  return { ok: true, msg: `✓ ${t[0].toUpperCase()} · ISA ${t[1]} @ ${t[2]} ${t[3]}${t[4] ? " " + t[4] : ""}` };
}

function createLineChecks() {
  return $("#createInput").value.split("\n")
    .map((raw, i) => ({ n: i + 1, res: checkCreateLine(raw) }))
    .filter((x) => x.res !== null);
}

function renderCreateLineChecks() {
  const mount = $("#createLines");
  mount.innerHTML = "";
  for (const { n, res } of createLineChecks()) {
    mount.append(el("div", { class: "linechk " + (res.ok ? "ok" : "bad") },
      el("span", { class: "ln" }, "第" + n + "行"), res.msg));
  }
}

function gateCreatePlan() {
  const checks = createLineChecks();
  const bad = checks.filter((c) => !c.res.ok).length;
  const ok = !!create56.warehouse && checks.length > 0 && bad === 0 && !create56.busy;
  $("#createPlanBtn").disabled = !ok;
  $("#createGoTip").textContent = !create56.warehouse ? "请先选择仓库供应商"
    : checks.length === 0 ? "请粘贴预约明细"
    : bad ? `有 ${bad} 行格式错误 — 修正或删除后才能查询` : "只读预检：逐个检查 ISA 是否已存在";
}

function setCreateStatus(nodes, isErr) {
  const s = $("#createStatus");
  s.hidden = false; s.className = "status" + (isErr ? " err" : "");
  s.innerHTML = ""; nodes.forEach((n) => s.append(n));
}

const C56_LABEL = {
  create: ["brand", "待新建"],
  exists: ["ok", "已存在 · 跳过"],
  dup: ["neu", "同批重复"],
  block: ["bad", "拦截"],
};

function renderC56(r, committed) {
  create56.plan = r;
  const s = r.summary;
  setCreateStatus(committed ? [
    el("span", { class: "pill" }, "✅ 已创建 ",
      el("b", {}, String(r.rows.filter((x) => (x.commit || {}).record_id).length))),
    el("span", { class: "pill" }, "回读核实 ",
      el("b", {}, String(r.rows.filter((x) => (x.commit || {}).verified).length))),
    el("span", { class: "pill" }, `已存在跳过 ${s.exists} · 重复 ${s.dup} · 拦截 ${s.block}`),
    r.latency && r.latency.created ? el("span", { class: "pill" },
      `搜索索引延迟 均值 ${r.latency.avg}s（最大 ${r.latency.max}s）`) : null,
  ].filter(Boolean) : [
    el("span", { class: "pill" }, "共 ", el("b", {}, String(s.lines)), " 行"),
    el("span", { class: "pill" }, "待新建 ", el("b", {}, String(s.create))),
    el("span", { class: "pill" }, `已存在 ${s.exists} · 同批重复 ${s.dup}`),
    el("span", { class: "pill" + (s.block ? " warnpill" : "") }, `⛔ 拦截 ${s.block}`),
    el("span", { class: "pill" + (s.warnings ? " warnpill" : "") }, `⚠ 警告 ${s.warnings}`),
    el("span", { class: "pill" }, `账号 ${r.account || "—"} · 环境 ${r.env.toUpperCase()}`),
  ]);

  const mount = $("#createResults");
  mount.innerHTML = "";
  const cols = ["", "行", "目的地", "ISA", "预约时间", "状态", "说明"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  for (const row of r.rows) {
    const p = row.parsed || {};
    const chk = el("input", { type: "checkbox", class: "rowchk56", "data-line": row.line_no });
    if (row.action !== "create" || committed) chk.disabled = true;
    else chk.checked = true;
    chk.addEventListener("change", updateCreateExec);

    let stat;
    const c = row.commit || {};
    if (committed && c.record_id) {
      stat = el("span", { class: "chip ok" },
        c.verified ? "✓ 已创建并核实" : "✓ 已创建（索引未及时可见）");
    } else if (committed && c.error) {
      stat = el("span", { class: "chip bad", title: c.error }, "✗ 失败");
    } else if (committed && c.skipped) {
      stat = el("span", { class: "chip warn", title: c.skipped }, "跳过");
    } else {
      const [cls, label] = C56_LABEL[row.action] || ["neu", row.action];
      stat = el("span", { class: "chip " + cls }, label);
    }

    const notes = el("div", { class: "warnlist" },
      ...row.warnings.map((w) => el("div", { class: "wline" }, "⚠ " + w)),
      ...row.blockers.map((b) => el("div", { class: "wline bad" }, "⛔ " + b)),
      ...(row.notes || []).map((n) => el("div", { class: "nline" }, n)),
      row.existing ? el("div", { class: "nline" },
        `表内: ${row.existing.dest || "?"} @ ${row.existing.time || "?"} · `
        + `${row.existing.account || "?"} (${row.existing.record_id})`) : null,
      committed && c.record_id ? el("div", { class: "nline" },
        `record ${c.record_id}`
        + (c.search_visible_after != null ? ` · 搜索 ${c.search_visible_after}s 后可见` : "")) : null,
      committed && (c.error || c.note) ? el("div", { class: "wline bad" }, c.error || c.note) : null);

    tbody.append(el("tr", { class: row.action === "block" ? "rowbad" : row.warnings.length ? "rowwarn" : "" },
      el("td", {}, chk),
      el("td", {}, String(row.line_no)),
      el("td", {}, p.dest || "—"),
      el("td", {}, el("b", {}, p.isa != null ? String(p.isa) : "?")),
      el("td", {}, p.time ? p.time + (p.tz ? " " + p.tz : "") : "—"),
      el("td", {}, stat),
      el("td", { class: "wcol" }, notes)));
  }
  mount.append(el("div", { class: "tablewrap" }, el("table", { class: "synctable" }, thead, tbody)));

  $("#createExecBar").hidden = committed || s.create === 0;
  if (!committed) updateCreateExec();
  if (committed) renderCreate56Warnings(r);
}

function updateCreateExec() {
  const n = document.querySelectorAll("#createResults input.rowchk56:checked:not(:disabled)").length;
  $("#createExecBtn").textContent = `创建缺失的预约（${n} 条）`;
  $("#createExecBtn").disabled = n === 0 || create56.busy;
  $("#createExecTip").textContent = n ? "只创建勾选行 — 已存在的 ISA 绝不重复创建" : "没有待新建的行";
}

function renderCreate56Warnings(r) {
  const mount = $("#createWarnings");
  mount.innerHTML = "";
  const ws = r.warnings_summary || [];
  mount.hidden = false;
  if (!ws.length) {
    mount.append(el("div", { class: "warnhead ok" }, "✅ 创建完毕 — 无警告"));
    return;
  }
  mount.append(el("div", { class: "warnhead" }, `⚠ 警告汇总（${ws.length} 条）`));
  const list = el("div", { class: "warnitems" });
  for (const w of ws) list.append(el("div", { class: "wline" }, "⚠ " + w));
  mount.append(list);
}

/* ═══════════════════════════════════════════════════════════════════════════
   ③ 核对 — read-only audit: 3.1 → 出库计划 → 预约 for each pasted 柜号.
   ═══════════════════════════════════════════════════════════════════════════ */
const verifyState = { warehouse: null, busy: false };

const FLAG_LABEL = {
  ok: ["ok", "✓ 正确"],
  missing: ["bad", "无出库计划"],
  no_isa: ["bad", "出库计划无预约"],
  isa_diff: ["bad", "ISA 不符"],
  time_diff: ["warn", "时间不符"],
  dest_diff: ["bad", "目的地不符"],
  acct_diff: ["warn", "预约账号不符"],
  multi_trip: ["warn", "挂多个出库计划"],
  shared: ["neu", "与其它行共用预约"],
};

function gateVerify() {
  const n = $("#verifyInput").value.split("\n").filter((l) => l.trim()).length;
  const ok = !!verifyState.warehouse && n > 0 && !verifyState.busy;
  $("#verifyBtn").disabled = !ok;
  $("#verifyGoTip").textContent = !verifyState.warehouse ? "请先选择仓库供应商"
    : n === 0 ? "请粘贴要核对的柜号" : `将核对 ${n} 行（只读）`;
}

function setVerifyStatus(nodes, isErr) {
  const s = $("#verifyStatus");
  s.hidden = false; s.className = "status" + (isErr ? " err" : "");
  s.innerHTML = ""; nodes.forEach((n) => s.append(n));
}

function renderVerify(r) {
  const s = r.summary;
  setVerifyStatus([
    el("span", { class: "pill" }, "柜号 ", el("b", {}, String(s.lines)),
      " · 库存行 ", el("b", {}, String(s.rows))),
    el("span", { class: "pill" + (s.problems ? " warnpill" : "") },
      s.problems ? `✗ 问题 ${s.problems}` : "✓ 全部正确"),
    s.errors ? el("span", { class: "pill warnpill" }, `查询失败 ${s.errors}`) : null,
    el("span", { class: "pill" }, `链路 3.1 → ${r.plan_table || "—"} → 5.6 · ${r.env.toUpperCase()}`),
  ].filter(Boolean), s.problems > 0);

  const mount = $("#verifyResults");
  mount.innerHTML = "";
  const cols = ["柜号", "3.1 路线", "板数(实/预)", "出库计划", "挂到的预约", "结论"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");

  for (const res of r.results) {
    if (res.error) {
      tbody.append(el("tr", { class: "rowbad" },
        el("td", {}, el("b", {}, res.awb || "?")),
        el("td", { colspan: "5" }, "✗ " + res.error)));
      continue;
    }
    for (const row of res.rows) {
      const flags = row.flags.length ? row.flags : ["ok"];
      const a = row.appointment;
      const exp = res.expected || {};
      const worst = flags.some((f) => (FLAG_LABEL[f] || [])[0] === "bad") ? "rowbad"
        : flags.some((f) => (FLAG_LABEL[f] || [])[0] === "warn") ? "rowwarn" : "";
      tbody.append(el("tr", { class: worst },
        el("td", {}, el("b", {}, row.awb || res.awb),
          row.batch ? el("div", { class: "sub" }, row.batch) : null),
        el("td", {}, row.route || "—"),
        el("td", {}, `${row.actual || "空"} / ${row.estimated ?? "—"}`,
          row.boxes != null ? el("div", { class: "sub" }, `${row.boxes} 箱`) : null),
        el("td", {}, row.trip
          ? el("div", {}, el("span", { class: "chip neu" }, "✓ 已挂"),
              el("div", { class: "sub" }, row.trip.record_id))
          : el("span", { class: "chip bad" }, "无")),
        el("td", {}, a
          ? el("div", {}, el("b", {}, String(a.isa ?? "?")),
              el("div", { class: "sub" }, `${a.dest || "?"} @ ${a.time || "?"} · ${a.account || "?"}`),
              exp.isa && a.isa !== exp.isa
                ? el("div", { class: "sub subbad" }, `期望 ISA ${exp.isa}`) : null,
              exp.time && a.time !== exp.time
                ? el("div", { class: "sub subbad" }, `期望时间 ${exp.time}`) : null)
          : el("span", { class: "nullcell" }, "—")),
        el("td", {}, el("div", { class: "actlist" },
          ...flags.map((f) => {
            const [cls, label] = FLAG_LABEL[f] || ["neu", f];
            return el("span", { class: "chip " + cls }, label);
          }),
          row.shared_with && row.shared_with.length
            ? el("div", { class: "sub" }, `共用于 ${row.shared_with.length} 个其它库存行`) : null))));
    }
  }
  mount.append(el("div", { class: "tablewrap" }, el("table", { class: "synctable" }, thead, tbody)));
}

async function runVerify() {
  const btn = $("#verifyBtn");
  verifyState.busy = true; btn.disabled = true; btn.textContent = "核对中…";
  $("#verifyResults").innerHTML = "";
  $("#verifyStatus").hidden = true;
  showProgress("plan");
  clog("核对", "开始", `仓库=${verifyState.warehouse}`);
  try {
    const r = await fetch("/api/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warehouse: verifyState.warehouse,
        text: $("#verifyInput").value }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "核对失败");
    rememberJob(r.job.id, "verify");
    const job = await pollJob(r.job.id, "plan");
    forgetJob();
    hideProgress();
    renderVerify(job.result);
    clog("核对", "完成（只读）", job.result.summary);
  } catch (e) {
    hideProgress();
    cerr("核对", "失败:", e.message);
    setVerifyStatus([el("span", {}, "⚠ " + e.message)], true);
  } finally {
    verifyState.busy = false; btn.textContent = "开始核对（只读）"; gateVerify();
  }
}

// Hand-off from 文件解析: push parsed rows into ① or ② and switch tabs.
function sendToTab(which, lines) {
  if (!lines.length) return;
  if (which === "create") {
    $("#createInput").value = lines.join("\n");
    switchTab("create");
    renderCreateLineChecks(); gateCreatePlan();
  } else {
    $("#syncInput").value = lines.join("\n");
    switchTab("sync");
    renderLineChecks(); gateSyncPlan();
  }
}

async function planCreate56() {
  const btn = $("#createPlanBtn");
  create56.busy = true; btn.disabled = true; btn.textContent = "查询中…";
  $("#createExecBar").hidden = true;
  $("#createWarnings").hidden = true;
  $("#createResults").innerHTML = "";
  $("#createStatus").hidden = true;
  showProgress("plan");
  clog("新建预约·预检", "开始", `仓库=${create56.warehouse}`);
  try {
    const r = await fetch("/api/create56/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warehouse: create56.warehouse, text: $("#createInput").value }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "预检失败");
    rememberJob(r.job.id, "c56plan");
    const job = await pollJob(r.job.id, "plan");
    forgetJob();
    hideProgress();
    renderC56(job.result, false);
    clog("新建预约·预检", "完成（只读）", job.result.summary);
  } catch (e) {
    hideProgress();
    cerr("新建预约·预检", "失败:", e.message);
    setCreateStatus([el("span", {}, "⚠ " + e.message)], true);
  } finally {
    create56.busy = false;
    btn.textContent = "查询并预检（只读）";
    gateCreatePlan();
    updateCreateExec();      // same re-sync as planSync — see comment there
  }
}

async function execCreate56() {
  if (!create56.plan) return;
  const approvals = [...document.querySelectorAll("#createResults input.rowchk56:checked:not(:disabled)")]
    .map((c) => Number(c.dataset.line))
    .map((n) => ({ line_no: n, sig: create56.plan.rows.find((x) => x.line_no === n).sig }));
  if (!approvals.length) return;
  const envLabel = create56.plan.env === "dev" ? "DEV 测试环境" : "‼ PROD 生产环境";
  if (!confirm(`确认在 5.6 新建 ${approvals.length} 条预约？\n\n环境：${envLabel}\n`
    + `预约账号：${create56.plan.account}\n\n已存在的 ISA 会在写入前再次核查，绝不重复创建。`))
    return;
  const btn = $("#createExecBtn");
  create56.busy = true; btn.disabled = true; btn.textContent = "创建中…";
  $("#createStatus").hidden = true;
  showProgress("commit");
  clog("新建预约·执行", "开始", `行=${approvals.length}`);
  try {
    const r = await fetch("/api/create56/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warehouse: create56.warehouse, text: $("#createInput").value,
        approvals, env: create56.plan.env }),
    }).then((x) => x.json());
    if (!r.ok) {
      if (r.busy) {
        setCreateStatus([el("span", {}, "⚠ " + r.error + " — 已跟踪其进度")], true);
        const latest = await fetch("/api/sync/job?id=latest").then((x) => x.json());
        if (latest.ok && latest.job.state === "running")
          await pollJob(latest.job.id, "commit").catch(() => {});
        hideProgress();
        create56.busy = false; updateCreateExec();
        return;
      }
      throw new Error(r.error || "执行失败");
    }
    rememberJob(r.job.id, "c56commit");
    const job = await pollJob(r.job.id, "commit");
    forgetJob();
    hideProgress();
    renderC56(job.result, true);
  } catch (e) {
    hideProgress();
    cerr("新建预约·执行", "失败:", e.message);
    setCreateStatus([el("span", {}, "⚠ 执行失败：" + e.message)], true);
  }
  create56.busy = false; updateCreateExec(); gateCreatePlan();
}

// 规范要求：所有警告在最后集中呈现（W1/W2/W3 + 错误），一目了然可复制。
function renderWarningsSummary(r) {
  const mount = $("#syncWarnings");
  mount.innerHTML = "";
  const ws = r.warnings_summary || [];
  const errs = r.rows.filter((x) => (x.commit || {}).error)
    .map((x) => `第${x.line_no}行 ${(x.parsed || {}).awb || ""}: ${x.commit.error}`);
  const skips = r.rows.filter((x) => (x.commit || {}).skipped && x.approved)
    .map((x) => `第${x.line_no}行 ${(x.parsed || {}).awb || ""}: ${x.commit.skipped}`);
  if (!ws.length && !errs.length && !skips.length) {
    mount.hidden = false;
    mount.append(el("div", { class: "warnhead ok" }, "✅ 执行完毕 — 无警告"));
    return;
  }
  mount.hidden = false;
  mount.append(el("div", { class: "warnhead" },
    `⚠ 警告汇总（${ws.length + errs.length + skips.length} 条）— 请人工复核`));
  const list = el("div", { class: "warnitems" });
  for (const w of ws) list.append(el("div", { class: "wline" }, "⚠ " + w));
  for (const s of skips) list.append(el("div", { class: "wline" }, "⏭ " + s));
  for (const e2 of errs) list.append(el("div", { class: "wline bad" }, "✗ " + e2));
  mount.append(list);
  const btn = el("button", { class: "mini" }, "复制警告");
  btn.addEventListener("click", () => {
    navigator.clipboard.writeText([...ws, ...skips, ...errs].join("\n"));
    btn.textContent = "已复制";
  });
  mount.append(btn);
}

boot();
