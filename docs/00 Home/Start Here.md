---
title: Start Here
tags: [onboarding, lts]
---

# 🚇 LarkTunnel — Start Here（新接手者必读）

> [!abstract] 一句话
> LarkTunnel 是 **到仓核对台**：把每天到仓的集装箱/板数/预约信息，安全地写进
> 公司生产环境的飞书多维表格（3.1 库存 · 5.6 预约 · 5.x 出库计划），并留下
> 可追溯的审计记录。它不是"自动化黑盒"——**每一次写入都先只读预检、再人工勾选、
> 再二次确认，写完回读核实。**

状态：**LTS（2026-10-02 起）**。核心流程自 2026-08 起在生产稳定运行；本文
是给任何发现/接手这个工具的人的操作与维护说明。决策来龙去脉见 [[Project History]]。

---

## 1. 它解决什么问题

| 痛点（2026-07 当时） | LarkTunnel 的做法 |
|---|---|
| 到仓后要在 3.1 逐行找柜号+路线、填实际板数、再去 5.6/5.x 建预约和出库计划、再互相关联 —— 多表多步、容易漏/错 | ② 计划同步：粘贴一批明细 → 一次预检 → 勾选 → 批量执行，自动完成接线 |
| 预约（5.6）重复创建；同一 ISA 多柜 | ① 新建预约 三层去重（同批 / 查表 / 跨波次缓存） |
| 收货派送计划 Excel 要手工按路线汇总建 3.1 记录 | 📦 库存导入：解析 Excel → 按路线汇总 → 新建 3.1 |
| 3.1 记录莫名消失，飞书自带历史在高流量下不可用 | 🗑️ 删除日志：长连接事件监听，谁/何时/删了什么，永久可搜 |
| 多人共用一套 App 凭据，无法按人停用 | 授权表 + 角色（管理员/成员），成员版 exe 不含可见凭据 |

## 2. 两种使用身份

| 身份 | 入口 | 能做什么 |
|---|---|---|
| **成员**（仓库/操作同事） | 管理员分发的 `LarkTunnel.exe` 文件夹 + 授权码（`LT-` 开头） | 📦 导入 · ① 新建预约 · ② 计划同步 · ③ 核对。不接触任何飞书凭据。随附 `webapp/dist-extras/使用说明.txt` |
| **管理员 / 维护者**（你） | 本仓库；`python webapp/server.py`（或 Task Scheduler 服务） | 以上全部 + 🔍 查询 · 📄 解析 · 🗑️ 删除日志 · ⚙ 设置（凭据、授权表、环境） |

成员的写入权限由 **LT授权表** 控制：停用 = 把该行 状态 改为 停用；彻底撤销 =
再轮换 App Secret 并重新打包。详见 `webapp/README.md` 「桌面程序 · 设置 · 团队授权」。

## 3. 日常工作流（按页签顺序）

```
收货派送计划.xlsx ──📦 库存导入──▶ 3.1 新建记录（按路线汇总）
                                     │
预约单（目的地/ISA/时间）──① 新建预约─▶ 5.6 预约记录（只建缺失的 ISA）
                                     │
到仓明细（柜号 路线 板数 箱数 [ISA 时间]）──② 计划同步──▶ 3.1 实际板数 + 出库计划(5.x) + 互联 + 预约时间更新
                                     │
                              ③ 核对（只读）──▶ 3.1 → 出库计划 → 预约 链路逐层确认
                                     │
                     🗑️ 删除日志（只读 + 本地清理）──▶ 事后追查谁删了什么
```

**每张表只有一个写入入口**（📦 只新建 3.1；① 只新建 5.6；② 更新 3.1/建 5.x/改已关联 5.6）。
这是刻意的设计，防止两处逻辑对同一张表各写各的 —— 改动前请先读 [[Project History]] 里的"单一写入者"决定。

## 4. 不可妥协的规则（生产环境）

1. **预检只读；写入只写勾选行；写前复检**（情况变了的行自动跳过）；**写后回读核实**。
2. **绝不自动新建选项值**：目的地/预约账号/目的地路线 必须命中飞书里的**实时**选项，否则拦截。
3. **不猜**：3.1 匹配不到唯一行、仓库未映射、同 ISA 时间冲突 → 拦截并说明，不是猜一个。
4. **实际板数 只填空，绝不覆盖**。
5. **测试先在 dev 副本表**（`LARK_ENV=dev` / `run-dev.bat`，端口 8788）；dev 永不写共享的生产 5.x。
6. 改表结构（字段名/表 id）→ 改 `config/config.js` + Python 常量 + 重启；改选项值 → 只在飞书里改，网页实时读取。

完整版见 [[Production Guardrails]]。

## 5. 运行与发布

| 场景 | 怎么做 |
|---|---|
| 本机开发/生产服务 | `python webapp/server.py`（:8787）· 已注册 Task Scheduler「LarkTunnel Webapp」「LarkTunnel Deletion Watcher」开机自启，日志在 `logs/*-task.log` |
| 测试环境 | `webapp\run-dev.bat`（:8788，dev 副本表） |
| 跑离线测试 | `npm test`（`python -m unittest discover webapp/tests`，200+ 用例，不联网） |
| 真实数据集成测试（只写 dev 表） | `npm run test:integration` / `test:create56` / `test:workflow` / `test:split` |
| 打包成员版 exe | `webapp\build.bat`（PyInstaller，产物 `webapp/dist/LarkTunnel/`，把整文件夹 + `dist-extras/使用说明.txt` 发给成员） |
| 新成员 | 在 LT授权表 加一行（授权码/姓名/角色=成员/状态=启用），发 exe + 授权码 |
| 离职 | 该行 状态=停用；若可能拿到了凭据 → 飞书后台轮换 App Secret → ⚙ 重新保存 → 重新打包 |
| 字段/表变了 | `npm run verify:config`（只读）看 `[MISSING]`，按提示改 `config.js` 与 `appointment_sync.F31/F56` |
| 删除日志体积 | 🗑️ 页签状态栏显示体积，>5 GB 警告（`LARK_AUDIT_MAX_GB`）；用条件删除清理，自动 VACUUM |

依赖：webapp **零第三方依赖**（纯标准库 Python）；只有 `tools/deletion-watcher`
需要 `lark-oapi`，只有 `src/`（Node 命令行备用路径）需要 `npm install` + `lark-cli`。

## 6. 代码地图（先看哪里）

| 想了解 | 看 |
|---|---|
| 核心决策树（② 的每一步） | `webapp/appointment_sync.py` `_plan_row()` / `commit()`，对应 [[Appointment Sync Runbook]] + [[Decision Tree]] |
| 所有表/字段名 | `config/config.js`（唯一事实来源）+ [[Table Registry]] [[Field Glossary]] [[Warehouse & Account Map]] |
| HTTP 路由与权限矩阵 | `webapp/server.py`（`_FEATURE_OF` / `_gate`） |
| 飞书调用、环境解析、重试 | `webapp/lark_client.py` |
| 删除追踪 | `tools/deletion-watcher/watcher.py` + `webapp/audit_store.py` + [[Deletion Tracking]] |
| 桌面壳/凭据/授权 | `webapp/desktop.py` `app_settings.py` `access_control.py` |
| 前端 | `webapp/static/app.js`（原生 JS，一个文件，按页签分节） |

## 7. 排障速查

- 按钮像是"卡住" → 看页面进度条/`logs/webapp-task.log`；预检/执行是后台任务，刷新页面会自动重连。
- `环境不匹配` → 页面与服务端 LARK_ENV 不同，刷新页面。
- 🗑️ 显示监听器离线 → `logs/watcher-task.log`；离线期间的删除不会被记录（没有补推）。
- 操作人显示为 `ou_…` → 点 🗑️ 页签「🔄 采集操作人名单」；仍不行 = 该用户不在应用通讯录范围且没在任何表留下过痕迹。
- 控制台乱码/崩溃 → GBK 终端；启动输出保持 ASCII（`server._safe_console()` 已兜底）。

---

## 8. 作者/开发者特别说明（请作者填写）

> [!quote] 正确实践 — 作者补充
> _（留给作者：哪些操作习惯是对的、哪些"看起来能用但别那么做"。例如：
> 什么时候必须先跑 dev；拆柜 A/B 后缀怎么填；预约改期时该用 ② 还是手改 5.6……）_
>
> -
> -

> [!quote] 已知边界 / 不要试图"修"的地方 — 作者补充
> _（留给作者：哪些行为是刻意的，后来者可能误以为是 bug。）_
>
> -

> [!quote] 联系与交接 — 作者补充
> _（留给作者：飞书后台的管理员账号归属、App 凭据保管人、授权表所有者、
> 遇到问题先找谁。）_
>
> -

Related: [[Home]] · [[Project History]] · [[Production Guardrails]]
