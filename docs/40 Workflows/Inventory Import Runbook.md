---
title: Inventory Import Runbook
tags: [workflow, runbook]
---

# Inventory Import Runbook（收货派送计划 → 3.1）

> [!abstract] What this does
> Parses a container's unloading plan (收货派送计划 Excel), aggregates
> 箱数/重量/体积 **per destination route**, and creates one summary record per
> route in table **3.1**. Validated live on 2026-07-22 with container
> BEAU6279991 (5 records, sums reconciled exactly).

Registered as the **`lark-inventory-import`** skill
(`C:\Users\AAAA\.claude\skills\lark-inventory-import\SKILL.md`) so future
agents trigger it by intent ("import this unloading plan to Lark").

## Inputs (4)

| Input | Maps to 字段名 | Example |
|---|---|---|
| `--file` | — (the Excel plan) | `BEAU6279991  收货派送计划.xlsx` |
| `--awb` | `柜号/AWB` | `BEAU6279991` |
| `--batch` | `客户批次号` | `2026-LY-618` |
| `--warehouse` | `仓库供应商` | `VAST` |

Destinations are **derived from the file** (仓库代码/派送目的地 column),
normalized `CA-YVR4` → `YVR4`, one 3.1 record per route.

## Procedure

```bash
# 1. DRY RUN — parse, guard checks, preview. Writes nothing.
npm run import:inventory -- --file "<xlsx>" --awb <AWB> --batch <批次> --warehouse <仓库>

# 2. Show the preview to the operator; wait for explicit approval.

# 3. LIVE — same command + --yes. Creates records, then reads back and
#    verifies the sums match the plan totals.
npm run import:inventory -- --file "<xlsx>" --awb <AWB> --batch <批次> --warehouse <仓库> --yes
```

## Parsing engine

[`src/workflow/parse-plan.js`](../../src/workflow/parse-plan.js) — logic
extracted from the West Pipeline module
`awb-batch-processor/src/pod_generator.py` (battle-tested on real plans):

- **Sheet auto-selection** — UPS-named sheets skipped; 加西/WEST/卡派/LTL…
  names preferred; needs ≥2 known header aliases.
- **Header auto-detection** — the row among the first 20 with the most alias
  hits (`COLUMN_ALIASES`: 派送目的地/仓库代码, 箱数/件数, 重量, 体积/材积/CBM…).
- **Vertical-merge expansion** — quantity columns keep the group total in the
  top row, sub-rows get 0; identity columns are copied down.
- **汇总/footer rows** drop out automatically (no destination value).
- **UPS detection** — rows with UPS keywords or `1Z…` tracking codes are
  counted but loudly reported for human review.

## Guards (abort → ask the operator)

| Guard | Why | Override |
|---|---|---|
| Same-AWB records already in 3.1 | duplicate import protection | `--allow-duplicates` |
| Destination not an existing 目的地路线 option | creating a record with a new value **adds a select option** | `--allow-new-options` |
| Warehouse not in `config.enums.warehouseOptions` | typo → unwanted select option | edit config after verifying |

> [!danger] Production
> Dry run first, always. Overrides are operator decisions — an agent must
> never add them on its own. See [[Production Guardrails]].

Related: [[Appointment Sync Runbook]] · [[Field Glossary]] · [[Wrapper Library]]
