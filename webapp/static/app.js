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
  parse: null, commitWarehouse: null, lastPlan: null };

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
  state.parse = r;                 // keep for dry-run / commit
  state.commitWarehouse = null;    // new file -> stale dry-run pins are void
  state.lastPlan = null;
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
  updateUploadGate();   // no 仓库供应商 chosen yet -> upload/dry-run stay disabled
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
  const dryBtn = el("button", { class: "primary", id: "dry56Btn" }, "预演上传到 5.6（dry-run）");
  dryBtn.addEventListener("click", () => dryRun56(dryBtn));
  controls.append(dryBtn, el("span", { class: "up56note" },
    "按 ISA 去重 · 校验目的地/预约账号 · 预约账号＝上方所选「仓库供应商」"),
    el("span", { class: "up56gate", id: "whGateNote", hidden: "" },
      "⚠ 请先选择仓库供应商，再进行上传/预演"));
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

// Gate all upload actions on a chosen 仓库供应商: the per-row 上传 buttons and
// the 预演 button stay disabled until the dropdown has a value (locked
// file-derived values count). Buttons that are done or mid-flight are skipped.
function updateUploadGate() {
  const ok = !!whValue();
  const tip = ok ? "" : "请先选择仓库供应商";
  const dry = $("#dry56Btn");
  if (dry) { dry.disabled = !ok; dry.title = tip; }
  document.querySelectorAll("button.mini.up").forEach((b) => {
    if (b.dataset.done === "1" || b.dataset.busy === "1") return;
    b.disabled = !ok;
    b.title = tip;
  });
  const note = $("#whGateNote");
  if (note) note.hidden = ok;
  if (!ok) clog("上传", "已禁用：请先选择仓库供应商");
}

// Upload ONE record to 5.6 (guarded). Shows created / skip-exists / block.
async function uploadRecord(d, btn, stat) {
  if (btn.dataset.done === "1") return;
  if (!whValue()) { updateUploadGate(); return; }  // gate: need a 仓库供应商
  btn.disabled = true;
  btn.dataset.busy = "1";
  const prev = btn.textContent;
  btn.textContent = "上传中…";
  stat.className = "upstat";
  stat.textContent = "";
  clog("上传", "开始", `柜号=${d.awb}`, `ISA=${d.isa || "-"}`, `仓库=${whValue() || "(未选)"}`);
  try {
    const r = await fetch("/api/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record: d, warehouse: whValue() }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "上传失败");
    const it = r.item || {};
    const trip = it.trip || {};
    const pal = it.pallet || {};
    if (pal.error) cerr("上传", `柜号=${d.awb} 实际板数写入失败:`, pal.error);
    else if (pal.committed) clog("上传", `柜号=${d.awb} 实际板数已更新为 ${pal.value}`);
    if (it.action === "create" && it.record_id) {
      clog("上传", `完成 柜号=${d.awb}`, `预约记录=${it.record_id}`,
        trip.record_id ? `出库计划记录=${trip.record_id} (${trip.table})` : "无出库计划写入");
      if (trip.error) cerr("上传", `柜号=${d.awb} 出库计划创建失败:`, trip.error);
      btn.dataset.done = "1"; btn.textContent = "已写入"; btn.classList.add("ok");
      stat.className = "upstat ok";
      stat.textContent = "✓ " + it.record_id + tripStat(it) + palletStat(it);
    } else if (it.action === "create" && it.error) {
      // 5.6 write itself failed — keep the button usable for a retry
      cerr("上传", `失败 柜号=${d.awb} 写入 5.6 报错:`, it.error);
      btn.disabled = false; btn.textContent = prev;
      stat.className = "upstat err"; stat.textContent = "✗ 写入失败：" + it.error;
      stat.title = it.error;
    } else if (it.action === "skip") {
      console.log("当前派送记录已存在");
      const tripErr = trip.error;
      if (trip.record_id)
        clog("上传", `柜号=${d.awb} 预约已存在，补建出库计划=${trip.record_id} (${trip.table})`);
      else if (tripErr)
        cerr("上传", `柜号=${d.awb} 预约已存在，出库计划补建失败:`, tripErr);
      else
        clog("上传", `跳过 柜号=${d.awb}`, trip.do === "linked" ? "已关联出库计划" : (trip.note || ""));
      if (tripErr) { btn.disabled = false; btn.textContent = prev; } // retryable backfill
      else { btn.dataset.done = "1"; btn.textContent = "已存在"; btn.classList.add("skip"); }
      stat.className = tripErr ? "upstat err" : "upstat";
      stat.textContent = "⏭ 当前派送记录已存在" + tripStat(it) + palletStat(it);
    } else {
      cerr("上传", `拦截 柜号=${d.awb}:`, it.reason || "已拦截");
      btn.disabled = false; btn.textContent = prev;
      stat.className = "upstat err"; stat.textContent = "⛔ " + (it.reason || "已拦截");
      stat.title = it.reason || "";
    }
  } catch (e) {
    cerr("上传", `失败 柜号=${d.awb}:`, e.message);
    btn.disabled = false; btn.textContent = prev;
    stat.className = "upstat err"; stat.textContent = "✗ " + e.message;
  }
  delete btn.dataset.busy;
  updateUploadGate();   // re-sync with the current 仓库供应商 selection
}

// ---- 5.6 dry-run manifest (read-only) + commit ----
async function dryRun56(btn) {
  if (!state.parse || !(state.parse.details || []).length) return;
  const wh = whValue();
  if (!wh) { updateUploadGate(); return; }  // gate: need a 仓库供应商
  btn.disabled = true; const prev = btn.textContent; btn.textContent = "预演中…";
  const mount = $("#manifest56"); mount.innerHTML = "";
  mount.append(el("div", { class: "maninfo" }, el("span", { class: "spinner" }), " 预演中…"));
  clog("预演", "开始", `仓库=${wh || "(未选)"}`, `记录=${state.parse.details.length} 条`);
  try {
    const r = await fetch("/api/dryrun_56", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: state.parse.details, warehouse: wh }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "预演失败");
    state.commitWarehouse = wh;   // pin what was previewed for the commit step
    state.lastPlan = r;
    (r.items || []).filter((it) => it.exists).forEach(() => console.log("当前派送记录已存在"));
    const s = r.summary || {};
    clog("预演", "完成（未写入）",
      `新建=${s.create} 跳过=${s.skip} 拦截=${s.block}`,
      `出库计划: 新建=${s.trip_create} 补建=${s.trip_backfill}`,
      `实际板数: 更新=${s.pallet_write} 差异拦截=${s.pallet_block}`,
      `账号=${r.account || "?"}`, `出库计划表=${r.plan_table || "无"}`);
    (r.items || []).forEach((it) => {
      const p = it.pallet || {};
      if (p.status === "blocked")
        cerr("预演", `柜号=${it.awb} ISA=${it.isa} 已拦截:`, p.note);
    });
    ctable("预演", r.items);
    renderManifest(mount, r, false);
  } catch (e) {
    cerr("预演", "失败:", e.message);
    mount.innerHTML = ""; mount.append(el("div", { class: "maninfo err" }, "⚠ " + e.message));
  } finally {
    btn.disabled = false; btn.textContent = prev;
    updateUploadGate();   // re-sync with the current 仓库供应商 selection
  }
}

// Short trip status suffix for the single-row upload stat line.
function tripStat(it) {
  const t = it.trip || {};
  if (t.record_id) return ` · ${t.table || "出库计划"} ✓`;
  if (t.error) return ` · 出库计划创建失败`;
  if (t.do === "linked") return ` · 已关联出库计划`;
  return "";
}

// Short 实际板数 status suffix for the single-row upload stat line.
function palletStat(it) {
  const p = it.pallet || {};
  if (p.committed) return ` · 实际板数=${p.value} ✓`;
  if (p.error) return ` · 实际板数写入失败`;
  return "";
}

async function commit56(btn) {
  const s = state.parse;
  if (!s) return;
  // Commit writes the warehouse pinned at dry-run time, never the live
  // dropdown — otherwise a change after the dry-run could silently target a
  // different account / delivery-plan table than the manifest the user saw.
  if (state.commitWarehouse == null) { alert("仓库供应商已更改，请先重新预演"); return; }
  const p = state.lastPlan || {};
  if (!confirm(`将真实写入：预约账号=${p.account || "?"} · 出库计划表=${p.plan_table || "无"}\n`
    + "新建预约写入 5.6（已存在自动跳过），并在出库计划表新建/补建关联记录。确认写入？")) return;
  btn.disabled = true; btn.textContent = "写入中…";
  const mount = $("#manifest56");
  clog("写入", "开始", `仓库=${state.commitWarehouse}`, `账号=${p.account || "?"}`,
    `出库计划表=${p.plan_table || "无"}`);
  try {
    const r = await fetch("/api/commit_56", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: s.details, warehouse: state.commitWarehouse }),
    }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error || "写入失败");
    const items = r.items || [];
    const ok56 = items.filter((it) => it.record_id).length;
    const fail56 = items.filter((it) => it.action === "create" && !it.record_id).length;
    const tripOk = items.filter((it) => (it.trip || {}).record_id).length;
    const tripFail = items.filter((it) => (it.trip || {}).error).length;
    const palOk = items.filter((it) => (it.pallet || {}).committed).length;
    const palFail = items.filter((it) => (it.pallet || {}).error).length;
    (fail56 || tripFail || palFail ? cerr : clog)("写入", "完成",
      `预约: 已写入=${ok56} 失败=${fail56}`,
      `出库计划(${r.plan_table || "?"}): 已建=${tripOk} 失败=${tripFail}`,
      `实际板数: 已更新=${palOk} 失败=${palFail}`);
    items.forEach((it) => {
      const t = it.trip || {};
      const p = it.pallet || {};
      if (it.error) cerr("写入", `柜号=${it.awb} ISA=${it.isa} 写入 5.6 失败:`, it.error);
      if (t.error) cerr("写入", `柜号=${it.awb} ISA=${it.isa} 出库计划创建失败:`, t.error);
      if (p.error) cerr("写入", `柜号=${it.awb} 实际板数写入失败:`, p.error);
    });
    ctable("写入", items);
    renderManifest(mount, r, true);
  } catch (e) {
    cerr("写入", "失败:", e.message);
    btn.disabled = false; btn.textContent = "确认写入";
    mount.append(el("div", { class: "maninfo err" }, "⚠ 写入失败：" + e.message));
  }
}

// Chip for the 出库计划 column of the manifest.
function tripCell(it, committed) {
  const t = it.trip || {};
  const tbl = t.table ? t.table.split(" ")[0] : ""; // "5.2 BESTAR-CAL" -> "5.2"
  if (t.record_id) return el("span", { class: "chip ok" }, `✓ 已建 ${tbl}`);
  if (t.error) return el("span", { class: "chip bad", title: t.error }, "创建失败");
  if (t.do === "create" || t.do === "backfill") {
    if (committed) return el("span", { class: "chip neu" }, "未执行"); // 5.6 create failed upstream
    return el("span", { class: "chip " + (t.do === "create" ? "brand" : "warn") },
      `${t.do === "create" ? "新建" : "补建"} → ${tbl}`);
  }
  if (t.do === "linked") return el("span", { class: "chip neu" }, "已关联");
  return el("span", { class: "nullcell", title: t.note || "" }, t.note || "—");
}

// Chip for the 实际板数 column of the manifest.
function palletCell(it, committed) {
  const p = it.pallet || {};
  if (p.committed) return el("span", { class: "chip ok" }, `✓ ${p.value}`);
  if (p.error) return el("span", { class: "chip bad", title: p.error }, "写入失败");
  if (p.status === "blocked")
    return el("span", { class: "chip bad", title: p.note || "" }, "差异超限");
  if (p.status === "conflict")
    return el("span", { class: "chip bad", title: p.note || "" }, "板数冲突");
  if (p.status === "keep")
    return el("span", { class: "chip warn", title: p.note || "" }, "保留现值");
  if (p.status === "dup")
    return el("span", { class: "chip neu", title: p.note || "" }, "同批合并");
  if (p.status === "fill" || p.status === "update") {
    if (committed || it.action === "block")
      return el("span", { class: "chip neu" }, "未执行");
    return el("span", { class: "chip " + (p.status === "fill" ? "brand" : "warn"),
      title: p.note || "" }, `${p.status === "fill" ? "填入" : "更新"} ${p.value}`);
  }
  if (p.status === "same") return el("span", { class: "chip neu" }, "已一致");
  return el("span", { class: "nullcell", title: p.note || "" }, p.note || "—");
}

function renderManifest(mount, r, committed) {
  mount.innerHTML = "";
  const s = r.summary || { create: 0, skip: 0, block: 0 };
  const acc = r.account ? `预约账号=${r.account}` : `⚠ ${r.account_reason || "预约账号未定"}`;
  const items = r.items || [];
  let head, tripBits = "", palBits = "", prefix;
  if (committed) {
    // report what was actually written, not what was planned
    const ok56 = items.filter((it) => it.record_id).length;
    const fail56 = items.filter((it) => it.action === "create" && !it.record_id).length;
    const tripOk = items.filter((it) => (it.trip || {}).record_id).length;
    const tripFail = items.filter((it) => (it.trip || {}).error).length;
    const palOk = items.filter((it) => (it.pallet || {}).committed).length;
    const palFail = items.filter((it) => (it.pallet || {}).error).length;
    head = `已写入 ${ok56}${fail56 ? ` / 失败 ${fail56}` : ""} · 已存在跳过 ${s.skip} · 拦截 ${s.block} · ${acc}`;
    if (tripOk || tripFail) tripBits = ` · 出库计划(${r.plan_table || "?"})：已建 ${tripOk}${tripFail ? ` / 失败 ${tripFail}` : ""}`;
    if (palOk || palFail) palBits = ` · 实际板数：已更新 ${palOk}${palFail ? ` / 失败 ${palFail}` : ""}`;
    prefix = (fail56 || tripFail || palFail) ? "⚠ 写入结果：" : "✅ 写入结果：";
  } else {
    head = `新建 ${s.create} · 已存在跳过 ${s.skip} · 拦截 ${s.block} · ${acc}`;
    if (s.trip_create || s.trip_backfill)
      tripBits = ` · 出库计划(${r.plan_table || "?"})：新建 ${s.trip_create || 0} / 补建 ${s.trip_backfill || 0}`;
    if (s.pallet_write || s.pallet_block)
      palBits = ` · 实际板数：更新 ${s.pallet_write || 0}${s.pallet_block ? ` / 差异拦截 ${s.pallet_block}` : ""}`;
    prefix = "🔎 预演（未写入）：";
  }
  mount.append(el("div", { class: "maninfo" }, prefix + head + tripBits + palBits));

  const cols = ["柜号", "路线", "ISA", "时间", "动作", "出库计划", "实际板数", "说明"];
  const thead = el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c))));
  const tbody = el("tbody");
  for (const it of items) {
    let cls = "neu", label = it.action;
    if (it.action === "create") {
      if (committed && it.record_id) { cls = "ok"; label = "已写入"; }
      else if (committed) { cls = "bad"; label = "失败"; }
      else { cls = "brand"; label = "待新建"; }
    }
    else if (it.action === "skip") { cls = "warn"; label = "跳过"; }
    else if (it.action === "block") { cls = "bad"; label = "拦截"; }
    let note = committed && it.record_id ? ("✓ " + it.record_id)
      : (it.reason || "") + (it.existing_dest ? `（现${it.existing_dest}）` : "");
    if (it.error) note += (note ? " · " : "") + "写入失败：" + it.error;
    if ((it.trip || {}).error) note += (note ? " · " : "") + "出库计划：" + it.trip.error;
    const pal = it.pallet || {};
    if (pal.error) note += (note ? " · " : "") + "实际板数：" + pal.error;
    else if (pal.status === "conflict" || pal.status === "keep")
      note += (note ? " · " : "") + pal.note;
    tbody.append(el("tr", {},
      el("td", {}, it.awb || "-"), el("td", {}, it.route || "-"),
      el("td", {}, it.isa || "-"), el("td", {}, it.time || "-"),
      el("td", {}, el("span", { class: "chip " + cls }, label)),
      el("td", {}, tripCell(it, committed)),
      el("td", {}, palletCell(it, committed)),
      el("td", {}, note)));
  }
  mount.append(el("div", { class: "tablewrap" }, el("table", {}, thead, tbody)));

  if (!committed && (s.create > 0 || s.trip_backfill > 0 || s.pallet_write > 0)) {
    const parts = [];
    if (s.create > 0) parts.push(`新建 ${s.create} 条预约`);
    if (s.trip_create > 0) parts.push(`新建 ${s.trip_create} 条出库计划`);
    if (s.trip_backfill > 0) parts.push(`补建 ${s.trip_backfill} 条出库计划`);
    if (s.pallet_write > 0) parts.push(`更新 ${s.pallet_write} 条实际板数`);
    const cbtn = el("button", { class: "primary commit" }, `确认写入（${parts.join("，")}）`);
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
    sel.addEventListener("change", () => {
      console.log("仓库供应商 已选择:", sel.value || "(未选)");
      // a stale manifest must not be committed against the new warehouse
      state.commitWarehouse = null;
      const m = $("#manifest56");
      if (m && m.innerHTML) {
        m.innerHTML = "";
        m.append(el("div", { class: "maninfo" }, "仓库供应商已更改，请重新预演"));
      }
      updateUploadGate();   // enable/disable upload buttons to match the choice
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

const RE_DEST = /^(YEG|YYC|YVR)[1-9]$/;
const RE_ISA = /^\d{8,15}$/;
const RE_TIME = /^(\d{1,4})[/-](\d{1,2})[/-](\d{1,4})\s+\d{1,2}:\d{2}$/;

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
    return { ok: false, msg: `路线「${t[1]}」不合法（YEG/YYC/YVR + 1-9）` };
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
    sync.busy = false; btn.textContent = "查询并预检（只读）"; gateSyncPlan();
  }
}

function setSyncStatus(nodes, isErr) {
  const s = $("#syncStatus");
  s.hidden = false; s.className = "status" + (isErr ? " err" : "");
  s.innerHTML = ""; nodes.forEach((n) => s.append(n));
}

const ACTION_LABEL = {
  fill_pallets: (a) => `填实际板数=${a.value}`,
  update_isa_time: (a) => `更新预约 → ISA ${a.isa} @ ${a.time}`,
  link_trip: () => "挂靠行程",
  create_trip: () => "新建行程",
  create_isa: (a) => `新建预约 ISA ${a.group_isa}`,
  set_trip_isa: () => "行程补挂预约",
};
const PLAN_LABEL = {
  has_plan_match: ["ok", "✓ 计划一致"],
  has_plan_mismatch: ["warn", "计划不一致"],
  has_plan: ["neu", "已有计划"],
  has_plan_no_isa: ["warn", "行程缺预约"],
  no_plan: ["bad", "无派送计划"],
  no_plan_table: ["neu", "不适用"],
  no_dev_plan_table: ["neu", "DEV 停用"],
  link_existing: ["brand", "挂靠现有行程"],
  create_trip: ["brand", "新建行程"],
  create_isa_and_trip: ["brand", "新建预约+行程"],
  already_on_trip: ["ok", "✓ 已在行程中"],
};

function planCell(row) {
  const p = row.plan || {};
  const [cls, label] = PLAN_LABEL[p.status] || ["neu", p.status || "—"];
  const bits = [el("span", { class: "chip " + cls }, label)];
  if (p.current_isa) bits.push(el("div", { class: "sub" },
    `现挂 ISA ${p.current_isa} @ ${p.current_time || "无时间"}`));
  if (p.trip_total !== undefined && p.trip_total !== null)
    bits.push(el("div", { class: "sub" }, `行程合计约 ${p.trip_total} 板`));
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
  const NAMES = { fill_pallets: "填实际板数", update_isa_time: "更新预约ISA/时间",
    link_trip: "挂靠行程", create_trip: "新建行程(5.x)", create_isa: "新建预约(5.6)",
    set_trip_isa: "行程补挂预约" };
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
  if (j && JSON.parse(j).kind === "commit" && sync.busy) {
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
    clog("恢复", `重新连接到进行中的${saved.kind === "commit" ? "写入" : "预检"}任务`, job.id);
    sync.busy = true;
    showProgress(saved.kind);
    updateProgress(job, false);
    try {
      const finished = await pollJob(job.id, saved.kind);
      hideProgress();
      if (saved.kind === "commit") renderCommitOutcome(finished.result);
      else renderPlanOutcome(finished.result);
      if (finished.result && finished.result.warehouse) restoreWarehouse(finished.result.warehouse);
    } catch (e) {
      hideProgress();
      setSyncStatus([el("span", {}, "⚠ " + e.message)], true);
    }
    forgetJob();
    sync.busy = false;
    gateSyncPlan();
  } else {
    forgetJob();             // finished while we were away — results were seen
  }
}

function restoreWarehouse(key) {
  const seg = $("#whSeg");
  const b = [...seg.children].find((x) => x.dataset.w === key);
  if (b && !b.classList.contains("on")) b.click();
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
