---
title: Configuration
tags: [setup]
---

# Configuration

There is **one file to edit**: [`config/config.js`](../../config/config.js).
It is the single source of truth for tokens, table ids, warehouse mappings,
field names, and thresholds. Nothing else is hardcoded.

## What lives in config.js

| Section | Purpose |
|---|---|
| `baseToken` | The Bitable app token containing all workflow tables. |
| `wikiToken` | Optional — resolve the base token from a Wiki node instead. |
| `tables` | Friendly **label → table id** registry. Call `base.tableByLabel('3.1')`. |
| `fields` | Exact column names (字段名) grouped by table. |
| `warehouses` | Per-warehouse account, plan table, and link fields. |
| `enums` | Valid warehouse + destination options, for input validation. |
| `thresholds` | `palletDiffWarning` (2) and `tripPalletCap` (28). |

## Editing tips

- To **add / point at a new table**: add a line to `tables` — that's the
  "insert tableId" spot the tool was designed around.
- To **fix a renamed column**: change it in `fields`; every wrapper and the
  runbook pick it up automatically.
- Labels in `tables` are reused across the codebase (e.g. `'5.2 BESTAR-CAL'`),
  so keep them stable.

## The `VERIFY` markers

Some field names were transcribed from the workflow spec and not yet confirmed
against the live schema (mostly on **5.6** and the **5.x** tables). They carry a
`VERIFY` comment. Confirm them read-only:

```bash
npm run verify:config
```

This prints `[OK]` / `[MISSING]` for each expected field so you can correct
`config.js`. It performs **only read calls**.

Related: [[Table Registry]] · [[Field Glossary]] · [[Warehouse & Account Map]]
