---
title: Deletion Tracking
tags: [safety, design, ops]
---

# Deletion Tracking（3.1 记录丢失追踪）

> [!abstract] Problem
> Records in **3.1** are going missing. Base 自动化 workflow **cannot trigger
> on deletion**, so nothing logs who deleted what, when. Goal: track deletion
> events with **time + operator + the deleted content**, and ideally prevent
> accidental deletions (typo / misclick) in the first place.

Researched 2026-09-03 against Feishu OpenAPI docs + help center. Four layers,
independent — adopt any subset. **L0 works today with zero code.**

---

## L0 · Built-in forensics（今天就能查，零代码）

Feishu Base already records every add/edit/**delete** operation with
**operator + timestamp**, and keeps record-level change history **180 days**:

- **操作历史 / 历史记录**: Base 右上角 ⋯ 菜单 → 查看历史记录。Shows 数据表
  configuration and record 新增/删除/编辑 operations, per operator, per time.
  The Base can be **restored to any historical version** (restore keeps current
  permissions; the restore itself is also logged).
- **单条记录变更历史**: for surviving records — content diffs, 操作时间, 操作人.
- 回收站 (30 天) applies to whole 云文档 files, not individual Bitable records —
  record recovery goes through **version restore** or manual re-entry from the
  history view.

> [!tip] For the deletions you've ALREADY suffered
> Open 3.1 → 历史记录, scan for 删除记录 entries — operator and time are right
> there (within 180 days). This answers "who misclicked" for current incidents
> before any tooling is built.

Refs: [查看多维表格的历史记录](https://www.feishu.cn/hc/zh-CN/articles/263286036719) ·
[版本管理](https://www.feishu.cn/hc/zh-CN/articles/895547707871)

## L1 · Real-time deletion logger（核心方案：事件订阅）

The automation tool can't trigger on delete, but the **Open Platform event
`drive.file.bitable_record_changed_v1` can**. Verified from the official doc
([bitable_record_changed](https://open.feishu.cn/document/docs/bitable-v1/events/bitable_record_changed.md)):

| Payload field | Contents |
|---|---|
| `action_list[].action` | `record_added` / `record_edited` / **`record_deleted`** |
| `action_list[].before_value` | **the deleted record's field values** (formula fields excluded — fine, they're derived) |
| `operator_id` | union_id / user_id / open_id of **who did it** |
| `update_time` | **when** (epoch seconds) |
| `table_id` / `file_token` / `revision` | which table (filter to 3.1; same subscription covers 5.x + dev copies — the whole Base) |

**One-time setup:**
1. App console（应用 `cli_aa89c...`）: 事件订阅 → add
   `drive.file.bitable_record_changed_v1`; delivery mode **长连接**（WebSocket —
   no public URL needed）. Scopes: any of `base:record:read` / `bitable:app` /
   `drive:drive`, plus `contact:user.employee_id:readonly` to resolve operator
   names.
2. Subscribe the Base file once（[subscribe API](https://open.feishu.cn/document/server-docs/docs/drive-v1/event/subscribe.md)）:
   `POST /open-apis/drive/v1/files/C13Zb8l6WassnesyRJhufdvLsFe/subscribe?file_type=bitable`
   （bitable 只支持订阅全部事件，事件里再按 `table_id` 过滤）.
3. Run a small watcher daemon that consumes the event stream and logs
   `record_deleted` actions.

> [!success] IMPLEMENTED 2026-09-03 — pending activation
> Built after L0 proved impractical at scale (history UI unusable beyond ~30 min
> under heavy multi-table traffic) and L3 impossible (roles lack a
> delete-specific toggle). Components:
>
> | Piece | Where |
> |---|---|
> | Watcher daemon（长连接 → SQLite）| `tools/deletion-watcher/watcher.py`（lark-oapi 1.7.3 typed handler; `run-watcher.bat`）|
> | Audit store（stdlib SQLite, WAL, dedup by event_id）| `webapp/audit_store.py` → `logs/audit.db`（gitignored）|
> | Read-time enrichment（field_id→字段名, operator→姓名, JSON 值美化）| `webapp/audit_view.py` |
> | 🗑️ 删除日志 search tab（只读）| webapp `/api/audit/status` + `/api/audit/search` |
>
> Logging policy (operator-specified 2026-09-03): **`record_added` +
> `record_deleted`, all tables; edits not logged** (re-enable via
> `TRACKED_ACTIONS` in watcher.py). Deletions carry `before_value` — the
> deleted content, searchable forever. Search = AND of substring terms over
> every flattened field value (select opt-ids resolved to names at ingest)
> + filters (action / table / record_id / time). Events stored verbatim
> (`raw_json`). The 🗑️ tab also supports **selective log pruning**: tick rows
> → 删除选中日志 → local delete + auto-VACUUM (reclaims disk; never touches
> Lark). Operator-name display requires `contact:contact.base:readonly`
> (falls back to open_id without it). Tests: `webapp/tests/test_audit.py`.
>
> **Activation checklist（需要操作者执行/批准）:**
> 1. 开发者后台 → 事件与回调: 订阅方式=**长连接**; 添加事件
>    `drive.file.bitable_record_changed_v1`（多维表格记录变更）; 确认应用有
>    `base:record:read`（或 bitable:app）; 可选 `contact:user.employee_id:readonly`
>    （事件带 user_id）与通讯录读权限（操作人显示姓名而非 open_id）.
> 2. `pip install -r tools/deletion-watcher/requirements.txt`（本机已装）.
> 3. 一次性订阅（写入类配置，先获批准）: `python tools/deletion-watcher/watcher.py --subscribe`
> 4. 常驻运行: **已注册 Windows 任务计划**（2026-09-03）—
>    「LarkTunnel Deletion Watcher」与「LarkTunnel Webapp」登录时自动启动
>    （隐藏窗口 + 崩溃自动重启; 日志 `logs/watcher-task.log` / `logs/webapp-task.log`;
>    手动运行仍可用 `run-watcher.bat`）.
> 5. Dev 验证: 在 DEV 3.1 副本删一条种子记录 → 🗑️ 页签按字段值搜到，含操作人+时间+被删内容.
> 6. 重启 webapp 服务以加载新 `/api/audit/*` 路由.
>
> ⚠ Reminders: 只有订阅并且监听器在线之后发生的操作才会入库（历史删除仍走 L0）;
> 不要与 `lark-cli event consume` 长期并行运行（同应用长连接负载均衡可能分走事件 —
> 上线时实测验证）.
>
> **ACTIVATED 2026-09-03 (live-verified on prod):** console event added + published;
> `--subscribe` OK (`is_subscribe: true`); watcher connected — note the tenant is
> **international Lark**: ws must use `open.larksuite.com`（feishu.cn ws 报
> `1000040351 Incorrect domain name`，尽管 REST 两个域都通）; watcher auto-tries
> lark→feishu, override `LARK_WS_DOMAIN`. End-to-end test: operator created+deleted
> a 3.1 row (柜号 205-34129966) — add/9 edits/delete all captured with operator
> and 157-field before_value; search by the vanished 柜号 hits. Select option ids
> (optXXX) resolve to names at ingest (search) and read (display); formula-carried
> opt ids from OTHER tables stay raw (minor known gap). Operator shows open_id
> until a contact-read scope is granted (optional). 5 GB size guard active
> (`LARK_AUDIT_MAX_GB`): watcher console warning + 🗑️ tab banner, warn-only.

> [!example] 🗑️ tab quick guide (full version lives IN the tab — 📖 使用指南与示例)
> **Investigate:** keyword box, space-separated terms = AND (`BEAU6279991 YVR4`)
> → click a row for the deleted record's full fields → paste stray `opt…` ids
> into the ID 解析 box.
> **Bulk prune (条件删除):** JSON condition → 预览命中 → 按条件删除. Keys
> combine (AND): `q` · `action` · `table_id` · `record_id` · `from`/`to`
> (`YYYY-MM-DD[ HH:MM]`, inclusive). Examples:
> `{"action":"record_edited"}` (purge legacy edit events) ·
> `{"to":"2026-08-31"}` (older than Sept) · `{"q":"BEAU6279991"}` (one
> container) · empty `{}` is refused — full wipe needs an explicit
> `{"from":"2000-01-01"}`. Local `logs/audit.db` only; auto-VACUUM after.
> The in-tab guide's 填入 buttons drop each example straight into the input.

**Watcher design** (original proposal — as built, see above):
- Python + `lark-oapi` SDK WebSocket client（唯一新依赖，与零依赖 webapp 隔离）.
  `lark-cli event consume` does NOT wrap this event key (verified: its registry
  has IM/VC/Minutes/Whiteboard only), so the SDK client is the path.
- On `record_deleted`: append one NDJSON line to `logs/deletions-YYYYMM.ndjson`
  — `{time, operator(open_id + resolved name), table label, record_id,
  before_value}` — and optionally **write a row into a new Lark
  「删除日志」table** in the same Base so the team sees deletions in-product
  and can one-click re-create the lost row from `before_value`.
- Also log `record_edited` on 3.1 key fields? Optional — cheap once the pipe
  exists, gives an edit audit trail the UI already has (skip initially).

**Caveats:**
- Events during watcher downtime are lost（长连接断开期间的补推有限）→ L2 是兜底。
- Long-connection coexistence with lark-cli's event bus (same app credentials)
  needs a live check — VERIFY at implementation before trusting both at once.
- Console event config is an app-level change → per
  [[Production Guardrails]], inform the operator before touching it.

## L2 · Shadow snapshot + diff（兜底：不依赖平台推送）

The webapp server already runs continuously — add a background snapshot thread
(stdlib only, keeps webapp zero-dep):

- Every 10–15 min: page through 3.1 (record_id + key identity fields:
  柜号/AWB, 目的地路线, 仓库供应商, 客户批次号, 箱数, 实际板数, 最后修改时间),
  persist to `webapp/.snapshots/31-latest.json` (+ daily archive).
- Diff vs previous: **vanished record_id ⇒ deletion detected**, with the full
  last-known row and a detection window (`between T1 and T2`).
- No operator attribution on its own — pair with L0 (look up the window in
  操作历史) or L1 (event has the operator). Catches anything L1 misses while
  down; costs a few read calls per cycle.

## L3 · Prevention（治本）

- **高级权限 (advanced permissions)** on the Base: give regular operators a
  role **without record-delete rights** on 3.1; admins keep it. Misclicks
  become impossible rather than merely logged. (Plan-tier gated — check
  whether the org's edition includes 高级权限.)
- Soft-delete convention: a `作废` checkbox field + view filter, so "removing"
  a row = ticking a box (reversible, attributable via 修改人 field types
  1003/1004) instead of hard delete.

---

## Recommendation

| Layer | Effort | Gets you | Verdict |
|---|---|---|---|
| L0 操作历史 | none | who + when for PAST deletions (≤180d) | **Do now, manually** |
| L1 event watcher | ~1 day | permanent who + when + **what** log, in-product 删除日志表 | **Build — the actual goal** |
| L2 snapshot diff | ~half day | detection safety net when watcher is down | Nice insurance, webapp-native |
| L3 高级权限/soft-delete | config only | deletions stop happening | **Strongly consider** |

Suggested order: L0 today → L3 policy decision → L1 build (dev-Base first,
per [[Production Guardrails]]) → L2 if the watcher's downtime window matters.

Related: [[Production Guardrails]] · [[Table Registry]] · [[Home]]
