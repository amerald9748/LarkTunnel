---
title: Warehouse & Account Map
tags: [reference]
---

# Warehouse & Account Map

Maps the `仓库供应商` value on a 3.1 row to everything the workflow needs:
the appointment account, the delivery-plan table, and the two link fields.

## Mapped warehouses

| Warehouse (`仓库供应商`) | 预约账号 (account) | Plan table | 3.1 link field (`planLinkField`) | 5.x back-link (`inventoryBackLinkField`) | Region |
|---|---|---|---|---|---|
| `BESTAR` | `BESTAR` | [[Table Registry\|5.2 BESTAR-CAL]] | `5.2 BESTAR-CAL` | `库存信息-卡尔加里` *(VERIFY)* | CAL |
| `WBLL` | `WBLL` | 5.3 WBLL-EDM | `5.3 WBLL-EDM` | `库存信息-埃德蒙顿` *(VERIFY)* | EDM |
| `VAST` | `VAST` *(UI label 元浩)* | 5.4 VAST-VAN-01 | `5.4 VAST-VAN-01` | `库存信息-温哥华` *(VERIFY)* | VAN |
| `GFL` | `GFL` | 5.5 GFL-VAN-02 | `5.5 GFL-VAN-02` | `库存信息-温哥华GFL` *(VERIFY)* | VAN |
| `CAL-5505` | `BESTAR` | 5.2 BESTAR-CAL | `5.2 BESTAR-CAL` | `库存信息-卡尔加里` *(VERIFY)* | CAL |

> [!info] Confirmed 2026-07-22
> - `CAL-5505` routes through **BESTAR** (user confirmed).
> - 3.1 link-field names verified against live schema: `5.4 VAST-VAN-01`,
>   `5.5 GFL-VAN-02` (note the `-01`/`-02` suffixes).

> [!note] Appointment accounts (from spec)
> `元浩 = VAST`, `BESTAR = BESTAR`, `WBLL = WBLL`, `GFL = GFL`.
> The UI shows **元浩** for the VAST account; `config.warehouses.VAST.accountAlias`
> records that.

## ⚠ Unmapped warehouses — do NOT guess

| Warehouse | Problem |
|---|---|
| `TOR-1140` | No appointment account and no delivery-plan table in the spec. |

`config.warehouses` flags these with `unmapped: true`. If a session's warehouse
is one of these, the workflow must **stop and ask the user** how it routes
rather than inventing a mapping.

## Destination options

`YEG1..9` (Edmonton), `YYC1..9` (Calgary), `YVR1..9` (Vancouver) — see
`config.enums.destinationOptions`.

Related: [[Field Glossary]] · [[Appointment Sync Runbook]]
