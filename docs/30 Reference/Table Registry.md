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
| `5.4 VAST-VAN` | `tblHIdhwKuqvdM7I` | Delivery plans (trips) for **VAST** — Vancouver. |
| `5.5 GFL-VAN` | `tbl2QdSLd53g987f` | Delivery plans (trips) for **GFL** — Vancouver. |

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
        number totalPallets "总板数 (VERIFY)"
        link isa "→ 5.6"
        link inventory "库存信息-XXX (→ 3.1)"
    }
    APPT_5_6 {
        text isa "ISA"
        datetime timestamp "复制时间列"
        link plan "配送计划 (VERIFY)"
    }
```

- A **3.1** shipment links to exactly one **5.x** delivery trip (the warehouse
  decides which table + which link field).
- A **5.x** trip correlates to one **5.6** ISA appointment.
- The link between 3.1 and 5.x is **two-way**: the 5.x side back-link is
  `库存信息-XXX` (see [[Warehouse & Account Map]]).

Related: [[Field Glossary]] · [[Warehouse & Account Map]]
