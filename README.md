# 🚇 LarkTunnel · 到仓核对台

**LTS edition — 2026-10-02.** A guarded operations console for the company's
production Lark (Feishu) Base: inventory import, appointment creation, delivery-plan
sync, chain verification, and a permanent deletion audit log. Every write is
plan (read-only) → operator approval → commit → read-back verification.

> 📖 **New here? Read [`docs/00 Home/Start Here.md`](docs/00%20Home/Start%20Here.md) first** —
> what it's for, who uses which entry point, the daily workflow, the rules, and how to
> run/release. Why things are the way they are: [`docs/00 Home/Project History.md`](docs/00%20Home/Project%20History.md).

## Two ways in

| You are… | Use |
|---|---|
| A **member** (warehouse / ops colleague) | The `LarkTunnel.exe` folder your admin sent + your `LT-…` 授权码. See `webapp/dist-extras/使用说明.txt`. |
| The **admin / maintainer** | This repo. `python webapp/server.py` → http://127.0.0.1:8787 (or the registered Task Scheduler services). Test env: `webapp\run-dev.bat` (:8788, dev copy tables). |

## Layout

| Path | What |
|---|---|
| `webapp/` | **The tool.** Pure-stdlib Python server + vanilla-JS frontend; exe build scripts. Details: `webapp/README.md`. |
| `config/config.js` | **Single source of truth** — base token, table ids, field names, warehouse map, env maps. |
| `tools/deletion-watcher/` | Long-connection listener feeding the 🗑️ 删除日志 (only part with a pip dependency: `lark-oapi`). |
| `docs/` | Obsidian vault (readable as plain Markdown). Start at `00 Home/Start Here.md`. |
| `src/`, `scripts/` | Node wrapper library + CLI fallback; `npm run verify:config` (read-only schema check). |
| `logs/` | `audit.db`, `ops.jsonl`, service logs — local, gitignored. |

## Everyday commands

```bash
npm test                      # 200+ offline unit tests (python -m unittest discover webapp/tests)
npm run verify:config         # READ-ONLY: confirm table ids + field names still match the Base
python webapp/server.py       # run the admin server (prod tables)
webapp\run-dev.bat            # run against dev copy tables
webapp\build.bat              # build the member exe -> webapp/dist/LarkTunnel/
```

## Non-negotiables

1. Writes only through a reviewed plan; never overwrite 实际板数; never auto-create select options.
2. One writer per table (📦→3.1 create · ①→5.6 create · ②→3.1 update/5.x/linked 5.6). Keep it that way.
3. Test on dev copies first (`LARK_ENV=dev`); dev never writes the shared prod 5.x tables.
4. Schema changes → `config.js` + `appointment_sync.F31/F56` + restart. Option-value changes → in Feishu only.

Full list: `docs/60 Safety/Production Guardrails.md`.

---

## Author's notes

> _交接要点、正确操作习惯。更多预留位在
> `docs/00 Home/Start Here.md` 第 8 节与 `Project History.md` 末尾。_

- Intended to be used with the below two WxWork forms:

VAST Delivery Plans:
https://doc.weixin.qq.com/smartsheet/s3_AVUAiQYcAP4SGGSdZ51quT022t2Vo?scode=AMcAvgd2AAw8ty0J8uAVUAiQYcAP4&version=5.0.9.6065&platform=win&tab=q979lj&viewId=vukaF8

BESTAR Delivery Plans:
https://doc.weixin.qq.com/smartsheet/s3_ARoASgZHADISGwM2X0tDRTzCTlO2i?scode=APMAYAepAFonGMNj22AaIADwY_AL8&journal_source=chat&roomid=Person%3A1688857363746722%3A1688857542906621&open_source=wecomprivate&version=5.0.9.6060&platform=win&tab=q979lj&viewId=vukaF8

The above forms need to follow the shapes in the tunnel input. Rearrange and maintain these forms to ensure all future operations are consistent.

The GFL plan form needs some slight extra work. To-do later.