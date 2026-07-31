---
title: Decision Tree
tags: [workflow]
---

# Decision Tree

Visual companion to the [[Appointment Sync Runbook]]. Same logic, faster to scan.

## Overall flow

```mermaid
flowchart TD
    START([Session inputs:\nwarehouse, AWB, destination,\nISA, timestamp, actualPallets]) --> VAL{Warehouse mapped?}
    VAL -->|No: TOR-1140 / CAL-5505| STOP1[/STOP — ask user how it routes/]
    VAL -->|Yes| FIND[Find UNIQUE 3.1 row\nAWB + destination + warehouse]
    FIND -->|0 rows| STOP2[/STOP — no matching shipment/]
    FIND -->|>1 rows| STOP3[/STOP — ambiguous, ask user/]
    FIND -->|1 row| PAL

    subgraph P3 [Step 3 — Pallet reconciliation]
      PAL{实际板数 empty?}
      PAL -->|Empty| FILL[Fill 实际板数 = provided]
      FILL --> D1{ diff vs 预计板数 > 2 ?}
      D1 -->|Yes| W1[[⚠ WARN: manually check\npallet count]]
      D1 -->|No| OK1[ok]
      PAL -->|Not empty| NOFILL[Do NOT update 实际板数]
      NOFILL --> D2{ diff vs 预计板数 > 2 ?}
      D2 -->|Yes| W2[[⚠ WARN: was not empty +\nlarge diff]]
      D2 -->|No| OK2[ok]
    end

    P3 --> PLAN{3.1 plan link\nfor this warehouse empty?}
    PLAN -->|Has plan| HASPLAN
    PLAN -->|No plan| NOPLAN
```

## Branch: row **has** a delivery plan

```mermaid
flowchart TD
    HASPLAN{ISA & timestamp match\nthe linked plan's ISA?} -->|Match| DONE1([Do nothing])
    HASPLAN -->|Mismatch| UPD[Update ISA + timestamp on the\nlinked 5.6 record to this session's values]
    UPD --> DONE2([Done])
```

## Branch: row has **no** delivery plan

```mermaid
flowchart TD
    NOPLAN{ISA exists in 5.6?} -->|Yes, ISA has a trip| A
    NOPLAN -->|Yes, ISA has NO trip| B
    NOPLAN -->|No, ISA not created| C

    A[Link 3.1 row to that existing trip\nvia two-way link] --> A2{trip total pallets > 28?}
    A2 -->|Yes| AW[[⚠ WARN: trip over 28 pallets]]
    A2 -->|No| ADONE([Done])

    B[Create a NEW trip in 5.x\ncorrelate trip ↔ existing ISA] --> B2[Link 3.1 row ↔ new trip]
    B2 --> BDONE([Done])

    C[Create ISA in 5.6\n= ISA + dest + timestamp + account] --> C2[Create NEW trip in 5.x\ncorrelate trip ↔ new ISA]
    C2 --> C3[Link 3.1 row ↔ new trip]
    C3 --> CDONE([Done])
```

## Warning summary (collect, report at end)

| # | Condition | Message |
|---|---|---|
| W1 | 实际板数 was empty **and** \|provided − 预计板数\| > 2 | "Pallet count filled but differs from estimate by >2 — manually check." |
| W2 | 实际板数 was **not** empty **and** \|provided − 预计板数\| > 2 | "实际板数 already populated; provided differs from estimate by >2." |
| W3 | Linked/【newly linked】 trip total pallets > 28 | "Trip exceeds 28-pallet cap." |

Related: [[Appointment Sync Runbook]] · [[Warehouse & Account Map]]
