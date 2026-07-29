---
title: Field Glossary
tags: [reference]
---

# Field Glossary

Every 字段名 (column name) the workflow reads or writes, by table. Names marked
**VERIFY** are best-guesses from the spec — confirm with `npm run verify:config`
and fix in [[Configuration]].

## Table 3.1 — Inventory / shipment (`config.fields.inventory`)

| 字段名 | Key | Type | Used for |
|---|---|---|---|
| `仓库供应商` | `warehouse` | text/select | Warehouse supplier. One of [[Warehouse & Account Map]]. Part of the unique-row key. |
| `柜号/AWB` | `awb` | text | Container / air-waybill code. Part of the unique-row key. |
| `目的地路线` | `destination` | text/select | Destination route (YEG/YYC/YVR 1-9). Part of the unique-row key. |
| `实际板数` | `actualPallets` | **text** (live) | Actual pallet count. Filled if empty (step 3). |
| `预计板数` | `estimatedPallets` | **formula** (live) | Estimated pallet count. Read-only; compared for the ±2 warning. |
| `箱数` | `boxes` | number | Carton/piece count (Excel 件数 sums). |
| `重量` | `weight` | number | Weight, kg. |
| `体积` | `volume` | number | Volume, m³. |
| `客户批次号` | `customerBatch` | text | Customer batch number. |
| `5.2 BESTAR-CAL` | link | link→5.2 | Delivery-plan link when warehouse = BESTAR **or CAL-5505**. |
| `5.3 WBLL-EDM` | link | link→5.3 | Delivery-plan link when warehouse = WBLL. |
| `5.4 VAST-VAN-01` | link | link→5.4 | Delivery-plan link when warehouse = VAST. |
| `5.5 GFL-VAN-02` | link | link→5.5 | Delivery-plan link when warehouse = GFL. |

> [!info] Live-schema notes (verified 2026-07-22)
> On 3.1, `ISA` and `派送计划` are **formula** fields — the workflow reads plan
> state through the link fields, and writes ISA/time on the linked 5.6 record.
> `仓库供应商` and `目的地路线` are single-selects; all YEG/YYC/YVR routes used so
> far already exist as options.

> The **unique row** in 3.1 is identified by `柜号/AWB` + `目的地路线` +
> `仓库供应商` together.

## Table 5.6 — ISA appointments (`config.fields.appointment`)

| 字段名 | Key | Type | Used for |
|---|---|---|---|
| `ISA` **(VERIFY)** | `isa` | text | The ISA appointment number. Search key in 5.6. |
| `复制时间列` | `timestamp` | datetime | The copy-time value tied to this ISA. |
| `目的地` **(VERIFY)** | `destination` | text/select | Destination for the appointment. |
| `预约账号` | `account` | select | Appointment account (derived from warehouse). |
| `配送计划` **(VERIFY)** | `deliveryPlanLink` | link→5.x | The correlated delivery trip. Empty ⇒ ISA not yet tied to a trip. |

## Tables 5.x — Delivery plans / trips (`config.fields.deliveryPlan`)

| 字段名 | Key | Type | Used for |
|---|---|---|---|
| `总板数` **(VERIFY)** | `totalPallets` | number/rollup | Trip total pallets. Warn if > 28 ([[Production Guardrails]]). |
| `ISA` **(VERIFY)** | `isaLink` | link→5.6 | Correlates the trip to its ISA appointment. |
| `库存信息-XXX` **(VERIFY)** | back-link | link→3.1 | Two-way back-link to inventory rows. Label differs per warehouse — see [[Warehouse & Account Map]]. |

Related: [[Table Registry]] · [[Appointment Sync Runbook]]
