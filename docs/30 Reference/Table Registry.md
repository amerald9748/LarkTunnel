---
title: Table Registry
tags: [reference]
---

# Table Registry

All tables live in one Base: `C13Zb8l6WassnesyRJhufdvLsFe` (see [[Configuration]]).
Reference them by **label** in code (`base.tableByLabel('3.1')`).

| Label | Table id | Role |
|---|---|---|
| `3.1` | `tblbJiIHMwND2OGX` | **Inventory / shipment master.** One row per shipment. Holds pallet counts and per-warehouse links to delivery plans. |
| `5.6` | `tblxiClBxvCku7rT` | **ISA appointments.** One row per ISA. Holds the `复制时间列` timestamp and links to a delivery trip. |
| `5.2 BESTAR-CAL` | `tblILN4i0RIdpPhz` | Delivery plans (trips) for **BESTAR** — Calgary. |
| `5.3 WBLL-EDM` | `tblacP4Rr6L5AXco` | Delivery plans (trips) for **WBLL** — Edmonton. |
| `5.4 VAST-VAN-01` | `tblHIdhwKuqvdM7I` | Delivery plans (trips) for **VAST** — Vancouver. |
| `5.5 GFL-VAN-02` | `tbl2QdSLd53g987f` | Delivery plans (trips) for **GFL** — Vancouver. |

### DEV copies (testing — created 2026-07-31)

| Label | Table id | Notes |
|---|---|---|
| DEV `3.1` | `tblQcXC82tDveeSp` | Full copy (155 fields). `LARK_ENV=dev` resolves `3.1` here. |
| DEV `5.6` | `tblIsKv8k3vvDs0B` | Full copy (16 fields). `LARK_ENV=dev` resolves `5.6` here. |
| DEV `5.2 BESTAR-CAL` | `tblJaXrkXhZr3R6N` | "5.2 BESTAR-CAL-P-01 副本". Full-dev trip pipeline for BESTAR / CAL-5505. |

**Dev never writes the shared/prod 5.x tables.** Only plan tables with a dev
copy are trip-enabled in dev; the rest have appointment/trip actions disabled
(pallet reconciliation still works). The duplex columns wired for dev live in
`config.js` (`devTables` / `devTripLinkFields` / `devTripIsaFields` /
`devPlanLinkFields31` / `devLinkOn56`) — to enable another warehouse,
duplicate its 5.x table and add the same key to all five maps. Note the dev
5.x copy's `ISA`/`预约时间`/`出库板数` formula-and-rollup columns resolve from
the PROD-pointing links, so they stay empty in dev — verify the dev link
columns and the dev 5.6 values instead (trip totals are computed by the tool,
not read from the rollup).

## Relationships

```mermaid
erDiagram
    INVENTORY_3_1 ||--o{ PLAN_5x : "linked per warehouse"
    PLAN_5x ||--o| APPT_5_6 : "trip ↔ ISA"
    INVENTORY_3_1 {
        text AWB "柜号/AWB"
        text warehouse "仓库供应商"
        text destination "目的地路线"
        number actualPallets "实际板数"
        number estimatedPallets "预计板数"
        link plan "5.2 / 5.3 / 5.4 / 5.5"
    }
    PLAN_5x {
        number rollupPallets "出库板数 (per-table name; prod links only)"
        link isa "预约信息 (→ 5.6)"
        link inventory "库存信息-XXX (→ 3.1)"
    }
    APPT_5_6 {
        number isa "ISA"
        text timestamp "复制时间列 'YYYY/MM/DD HH:MM'"
        link plan "5.2 出库计划 卡尔加里 … (one per 5.x)"
    }
```

- A **3.1** shipment links to exactly one **5.x** delivery trip (the warehouse
  decides which table + which link field).
- A **5.x** trip correlates to one **5.6** ISA appointment.
- The link between 3.1 and 5.x is **two-way**: the 5.x side back-link is
  `库存信息-XXX` (see [[Warehouse & Account Map]]).

Related: [[Field Glossary]] · [[Warehouse & Account Map]]
