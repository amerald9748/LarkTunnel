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

const state = { tables: [], views: [], fieldKey: "awb", mode: "contains", tz: "", parse: null };

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

// ---- bootstrap ------------------------------------------------------------
async function boot() {
  try {
    const r = await fetch("/api/tables").then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "加载表失败");
    state.tables = r.tables;
    state.tz = r.tz;
    $("#tzNote").textContent = "时间显示时区 " + r.tz;
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

async function parseFile(file) {
  const out = $("#parseOut");
  out.hidden = false;
  out.innerHTML = "";
  out.append(el("div", { class: "filemeta" }, el("span", { class: "spinner" }),
    ` 正在解析 ${file.name} …`));
  try {
    const content_b64 = await fileToBase64(file);
    const r = await fetch("/api/parse", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content_b64, table: $("#tableSel").value }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "解析失败");
    renderParsed(r);
  } catch (e) {
    out.innerHTML = "";
    out.append(el("div", { class: "filemeta" }, "⚠ " + e.message));
  }
}

const FIELD_ORDER = ["awb", "batch", "warehouse", "route", "appointment", "isa"];

function renderParsed(r) {
  const out = $("#parseOut");
  state.parse = r;                 // keep for dry-run / commit
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

  // ---- 5.6 upload controls (dry-run first) ----
  const controls = el("div", { class: "up56" });
  const dryBtn = el("button", { class: "primary" }, "预演上传到 5.6（dry-run）");
  dryBtn.addEventListener("click", () => dryRun56(dryBtn));
  controls.append(dryBtn, el("span", { class: "up56note" },
    "按 ISA 去重 · 校验目的地/预约账号 · 预约账号＝上方所选「仓库供应商」"));
  out.append(controls);
  out.append(el("div", { id: "manifest56", class: "manifest" }));

  const cols = ["柜号", "目的地路线", "预约号 / ISA", "预约时间", "板数", "操作"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  for (const d of details) {
    const qBtn = el("button", { class: "mini" }, "查询");
    qBtn.addEventListener("click", () => useParsed("awb", d.awb));
    const upBtn = el("button", { class: "mini up" }, "上传");
    const upStat = el("span", { class: "upstat" });
    upBtn.addEventListener("click", () => uploadRecord(d, upBtn, upStat));
    const tr = el("tr", {},
      el("td", {}, el("b", {}, d.awb)),
      el("td", {}, d.route || nullSpan()),
      el("td", {}, d.isa ? el("span", {},
        String(d.isa), d.grouped ? el("span", { class: "gtag" }, "分组") : null) : nullSpan()),
      el("td", {}, d.appointment || nullSpan()),
      el("td", {}, palletText(d)),
      el("td", {}, el("div", { class: "ops" }, qBtn, upBtn, upStat)));
    tbody.append(tr);
  }
  out.append(el("div", { class: "dethint" },
    "⬆ 上传目标：飞书「5.6 预约表」（真实写入）· 按 ISA 去重、校验目的地/预约账号 · 建议先「预演」"));
  out.append(el("div", { class: "tablewrap dettable" }, el("table", {}, thead, tbody)));
}

function whValue() {
  const s = document.querySelector(".whsel");
  return s ? s.value : "";
}

// Upload ONE record to 5.6 (guarded). Shows created / skip-exists / block.
async function uploadRecord(d, btn, stat) {
  if (btn.dataset.done === "1") return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "上传中…";
  stat.className = "upstat";
  stat.textContent = "";
  try {
    const r = await fetch("/api/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record: d, warehouse: whValue() }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "上传失败");
    const it = r.item || {};
    if (it.action === "create" && it.record_id) {
      btn.dataset.done = "1"; btn.textContent = "已写入"; btn.classList.add("ok");
      stat.className = "upstat ok"; stat.textContent = "✓ " + it.record_id;
    } else if (it.action === "skip") {
      console.log("当前派送记录已存在");
      btn.dataset.done = "1"; btn.textContent = "已存在"; btn.classList.add("skip");
      stat.className = "upstat"; stat.textContent = "⏭ 当前派送记录已存在";
    } else {
      btn.disabled = false; btn.textContent = prev;
      stat.className = "upstat err"; stat.textContent = "⛔ " + (it.reason || "已拦截");
      stat.title = it.reason || "";
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = prev;
    stat.className = "upstat err"; stat.textContent = "✗ " + e.message;
  }
}

// ---- 5.6 dry-run manifest (read-only) + commit ----
async function dryRun56(btn) {
  if (!state.parse || !(state.parse.details || []).length) return;
  btn.disabled = true; const prev = btn.textContent; btn.textContent = "预演中…";
  const mount = $("#manifest56"); mount.innerHTML = "";
  mount.append(el("div", { class: "maninfo" }, el("span", { class: "spinner" }), " 预演中…"));
  try {
    const r = await fetch("/api/dryrun_56", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: state.parse.details, warehouse: whValue() }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "预演失败");
    (r.items || []).filter((it) => it.exists).forEach(() => console.log("当前派送记录已存在"));
    renderManifest(mount, r, false);
  } catch (e) {
    mount.innerHTML = ""; mount.append(el("div", { class: "maninfo err" }, "⚠ " + e.message));
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

async function commit56(btn) {
  const s = state.parse;
  if (!s) return;
  if (!confirm("将向飞书「5.6 预约表」真实写入新建的预约记录。已存在的会自动跳过。确认写入？")) return;
  btn.disabled = true; btn.textContent = "写入中…";
  const mount = $("#manifest56");
  try {
    const r = await fetch("/api/commit_56", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: s.details, warehouse: whValue() }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "写入失败");
    renderManifest(mount, r, true);
  } catch (e) {
    btn.disabled = false; btn.textContent = "确认写入";
    mount.append(el("div", { class: "maninfo err" }, "⚠ 写入失败：" + e.message));
  }
}

function renderManifest(mount, r, committed) {
  mount.innerHTML = "";
  const s = r.summary || { create: 0, skip: 0, block: 0 };
  const acc = r.account ? `预约账号=${r.account}` : `⚠ ${r.account_reason || "预约账号未定"}`;
  mount.append(el("div", { class: "maninfo" },
    (committed ? "✅ 写入结果：" : "🔎 预演（未写入）：") +
    `新建 ${s.create} · 已存在跳过 ${s.skip} · 拦截 ${s.block} · ${acc}`));

  const cols = ["柜号", "路线", "ISA", "时间", "动作", "说明"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  for (const it of r.items || []) {
    let cls = "neu", label = it.action;
    if (it.action === "create") { cls = committed && it.record_id ? "ok" : "brand"; label = committed && it.record_id ? "已写入" : "待新建"; }
    else if (it.action === "skip") { cls = "warn"; label = "跳过"; }
    else if (it.action === "block") { cls = "bad"; label = "拦截"; }
    const note = committed && it.record_id ? ("✓ " + it.record_id)
      : (it.reason || "") + (it.existing_dest ? `（现${it.existing_dest}）` : "");
    tbody.append(el("tr", {},
      el("td", {}, it.awb || "-"), el("td", {}, it.route || "-"),
      el("td", {}, it.isa || "-"), el("td", {}, it.time || "-"),
      el("td", {}, el("span", { class: "chip " + cls }, label)),
      el("td", {}, note)));
  }
  mount.append(el("div", { class: "tablewrap" }, el("table", {}, thead, tbody)));

  if (!committed && s.create > 0) {
    const cbtn = el("button", { class: "primary commit" }, `确认写入 5.6（新建 ${s.create} 条）`);
    cbtn.addEventListener("click", () => commit56(cbtn));
    mount.append(cbtn);
  }
}

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
    sel.addEventListener("change", () =>
      console.log("仓库供应商 已选择:", sel.value || "(未选)"));
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
  try {
    const r = await fetch("/api/query", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "查询失败");
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
function showError(msg) { setStatus([el("span", {}, "⚠ " + msg)], true); }

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

boot();
