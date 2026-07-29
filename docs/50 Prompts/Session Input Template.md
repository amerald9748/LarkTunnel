---
title: Session Input Template
tags: [prompt]
---

# Session Input Template

The six inputs one appointment session needs. Machine copy lives at
[`src/workflow/inputs.example.json`](../../src/workflow/inputs.example.json).

## Fill-in block (hand this to the agent)

````text
warehouse:      <仓库供应商 — BESTAR | WBLL | VAST | GFL>   # TOR-1140 / CAL-5505 not yet supported
awb:            <柜号/AWB>
destination:    <目的地路线 — YEG1-9 | YYC1-9 | YVR1-9>
isa:            <ISA appointment number>
timestamp:      <复制时间列 — e.g. 2026-07-22 14:30:00>
actualPallets:  <实际板数 — integer>
````

## JSON form

```json
{
  "warehouse": "BESTAR",
  "awb": "093-9992123",
  "destination": "YYC3",
  "isa": "ISA-000123",
  "timestamp": "2026-07-22 14:30:00",
  "actualPallets": 12
}
```

## Validation the agent applies

- `warehouse` must be **mapped** (BESTAR / WBLL / VAST / GFL). Others → STOP.
- `destination` must be one of the 27 YEG/YYC/YVR options.
- All six fields present and non-empty.

Related: [[Agent System Prompt]] · [[Appointment Sync Runbook]]
