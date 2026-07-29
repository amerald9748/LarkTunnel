---
title: Appointment Sync Runbook
tags: [workflow, runbook, prompt]
---

# Appointment Sync Runbook

> [!abstract] What this does
> For **one shipment session**, reconcile the pallet count on its **3.1**
> inventory row and ensure it is correctly wired to an **ISA appointment (5.6)**
> and a **delivery trip (5.x)** for its warehouse — creating the ISA and/or trip
> if they don't exist yet, and collecting **warnings** for a human to review.

This note is written so an **agent can execute it deterministically**. Follow the
steps in order. Do not improvise around the [[Production Guardrails|guardrails]].

---

## 🧭 Operating rules (read first)

> [!danger] Production Base — non-negotiable
> 1. **Safe mode is ON by default.** Every write is *simulated* (logged, not
>    sent) until a human explicitly enables live writes. Do a **full dry run
>    first** and show the operator the intended writes.
> 2. **One row only.** This workflow acts on the single unique 3.1 row for the
>    session's AWB + destination + warehouse. If the lookup is not exactly one
>    row, **STOP** and ask.
> 3. **Never guess a mapping.** Unmapped warehouses (`TOR-1140`,
>    `卡尔加里（CAL-5505）`) → **STOP** and ask the user how they route.
> 4. **Confirm field names.** If `npm run verify:config` shows any `[MISSING]`
>    field, **STOP** — the schema drifted from [[Configuration|config.js]].
> 5. **Collect, don't crash.** Accumulate warnings and report them at the end;
>    only hard-stop on ambiguity/safety issues above.

---

## 📥 Inputs (Step 1 — obtain)

Collect these six from the operator (template: [`inputs.example.json`](../../src/workflow/inputs.example.json)):

| Input | 字段名 | Where it maps |
|---|---|---|
| `warehouse` | `仓库供应商` | 3.1 · [[Warehouse & Account Map]] |
| `awb` | `柜号/AWB` | 3.1 |
| `destination` | `目的地路线` | 3.1 |
| `isa` | `ISA` | 5.6 search key |
| `timestamp` | `复制时间列` | 5.6 (stored/compared per ISA) |
| `actualPallets` | `实际板数` | 3.1 |

> [!note] On `timestamp` / `复制时间列`
> `复制时间列` lives in **5.6**, keyed by ISA. For an **existing** ISA it is the
> stored value to compare against; for a **new** ISA the provided `timestamp`
> seeds the record. Treat the session `timestamp` as the intended current value.

**Validate before proceeding:**
- `warehouse` ∈ `config.enums.warehouseOptions`, and **is mapped** (not
  `unmapped:true`). Else → STOP.
- `destination` ∈ `config.enums.destinationOptions`.
- All six present and non-empty.

```js
const lark = require('./src/lark');
const { config } = lark;
const wh = config.warehouses[input.warehouse];
if (!wh || wh.unmapped) throw new Error(`Warehouse ${input.warehouse} is unmapped — ask the user how it routes.`);
```

Initialize a warnings list: `const warnings = [];`

---

## 🔎 Step 2 — Find the unique 3.1 row

Search **3.1** for the one row matching AWB **and** destination **and** warehouse.

```js
const base = lark.base();
const inv = base.tableByLabel('3.1');
const F = config.fields.inventory;

const row = inv.findUnique([
  [F.awb,         'is', input.awb],
  [F.destination, 'is', input.destination],
  [F.warehouse,   'is', input.warehouse],
]);
if (!row) throw new Error('No matching 3.1 row — STOP, ask the user.');
// findUnique throws automatically if >1 match (ambiguous) — surface to user.
```

---

## 📦 Step 3 — Reconcile pallet count

Read `实际板数` (actual) and `预计板数` (estimated) from the row.

```js
const actual    = row.fields[F.actualPallets];      // may be empty/undefined
const estimated = Number(row.fields[F.estimatedPallets]);
const provided  = Number(input.actualPallets);
const diff      = Math.abs(provided - estimated);
const CAP_DIFF  = config.thresholds.palletDiffWarning; // 2
```

**If `实际板数` is empty:**
1. Write `实际板数 = provided` on the 3.1 row (gated write).
2. If `diff > CAP_DIFF` → push warning **W1**.

```js
if (actual === undefined || actual === null || actual === '') {
  inv.updateRecord(row.record_id, { [F.actualPallets]: provided });
  if (diff > CAP_DIFF) warnings.push(`W1: 实际板数 filled (${provided}) but differs from 预计板数 (${estimated}) by ${diff} (>2) — manually check.`);
}
```

**If `实际板数` is NOT empty:**
1. **Do not** update it.
2. If `diff > CAP_DIFF` → push warning **W2** (note it was already populated).

```js
else {
  if (diff > CAP_DIFF) warnings.push(`W2: 实际板数 already = ${actual} (not updated); provided ${provided} differs from 预计板数 (${estimated}) by ${diff} (>2).`);
}
```

---

## 🚚 Step 4 — Ensure delivery plan link

Determine the warehouse's **plan link field** on the 3.1 row and whether it's set.

```js
const planLinkField = F.planLinkFields[input.warehouse]; // e.g. "5.2 BESTAR-CAL"
const planLinkVal   = row.fields[planLinkField];
const linkedPlanIds = lark.LarkTable.readLinkIds(planLinkVal);
const hasPlan       = linkedPlanIds.length > 0;

const planTable = base.tableByLabel(wh.planTable);       // the 5.x table
const appt      = base.tableByLabel('5.6');
const A = config.fields.appointment;
const D = config.fields.deliveryPlan;
```

### 4A — Row **HAS** a plan  → verify ISA/time, update if mismatched

1. From the linked 5.x trip, get its correlated **5.6** ISA record.
2. Compare that record's `ISA` + `复制时间列` to the session `isa` + `timestamp`.
3. **Match** → do nothing.
4. **Mismatch** → update the **linked 5.6 record's** `ISA` and `复制时间列` to the
   session values (gated write).

```js
if (hasPlan) {
  const trip   = planTable.getRecord(linkedPlanIds[0]);
  const isaIds = lark.LarkTable.readLinkIds(trip.fields[D.isaLink]);
  const isaRec = isaIds.length ? appt.getRecord(isaIds[0]) : null;

  const sameIsa  = isaRec && String(isaRec.fields[A.isa]) === String(input.isa);
  const sameTime = isaRec && String(isaRec.fields[A.timestamp]) === String(input.timestamp);

  if (isaRec && sameIsa && sameTime) {
    /* matches — do nothing */
  } else if (isaRec) {
    appt.updateRecord(isaRec.record_id, { [A.isa]: input.isa, [A.timestamp]: input.timestamp });
  }
  // (also re-run the >28 pallet check on the trip — see W3 below)
}
```

### 4B — Row has **NO** plan → resolve the ISA, then link

Search **5.6** for the session `isa`.

```js
else {
  const isaMatches = appt.findBy(A.isa, input.isa);   // exact match
  const isaRec = isaMatches[0] || null;
```

Three sub-cases:

#### (i) ISA exists **and** already correlated to a trip
Link this 3.1 row to that **existing** trip via the two-way link, unless the trip
already contains this shipment.

```js
  if (isaRec && lark.LarkTable.readLinkIds(isaRec.fields[A.deliveryPlanLink]).length) {
    const tripId = lark.LarkTable.readLinkIds(isaRec.fields[A.deliveryPlanLink])[0];
    // Link from the 3.1 side (populates the 5.x 库存信息-XXX back-link automatically)
    inv.setLink(row.record_id, planLinkField, [tripId]);
    // W3: overflow check
    const trip = planTable.getRecord(tripId);
    if (Number(trip.fields[D.totalPallets]) > config.thresholds.tripPalletCap)
      warnings.push(`W3: trip ${tripId} total pallets ${trip.fields[D.totalPallets]} exceeds 28.`);
  }
```

#### (ii) ISA exists but has **no** trip
Create a new trip in 5.x, correlate it to the existing ISA, then link the 3.1 row.

```js
  else if (isaRec) {
    const newTrip = planTable.createRecord({
      [D.isaLink]: [isaRec.record_id],
      [cfgBackLink(wh)]: [row.record_id],   // two-way link to 3.1 (库存信息-XXX)
    });
    // Also set the ISA's plan link + the 3.1 plan link (two-way keeps sides in sync)
    appt.setLink(isaRec.record_id, A.deliveryPlanLink, [newTrip.record_id]);
    inv.setLink(row.record_id, planLinkField, [newTrip.record_id]);
  }
```

#### (iii) ISA does **not** exist yet
Create the ISA in 5.6 (from `isa` + `destination` + `timestamp` + the warehouse's
`account`), create a new trip correlated to it, then link the 3.1 row.

```js
  else {
    const newIsa = appt.createRecord({
      [A.isa]:         input.isa,
      [A.destination]: input.destination,
      [A.timestamp]:   input.timestamp,
      [A.account]:     wh.account,          // BESTAR / WBLL / VAST / GFL
    });
    const newTrip = planTable.createRecord({
      [D.isaLink]:     [newIsa.record_id],
      [cfgBackLink(wh)]: [row.record_id],
    });
    appt.setLink(newIsa.record_id, A.deliveryPlanLink, [newTrip.record_id]);
    inv.setLink(row.record_id, planLinkField, [newTrip.record_id]);
  }
}

function cfgBackLink(wh) { return wh.inventoryBackLinkField; } // 库存信息-XXX
```

---

## 🧾 Step 5 — Report

Print a concise summary:
- The 3.1 row acted on (record id + AWB/dest/warehouse).
- What changed (or, in safe mode, what **would** change — the logged `intent`s).
- **All collected warnings** (W1/W2/W3), or "No warnings."

```js
console.log(warnings.length ? warnings.join('\n') : 'No warnings.');
```

---

## ✅ Execution checklist for the agent

- [ ] `npm run verify:config` clean (no `[MISSING]`)?
- [ ] Warehouse mapped (not `TOR-1140` / `CAL-5505`)?
- [ ] Exactly one 3.1 row found?
- [ ] Ran a **dry run** (safe mode ON) and showed intended writes to the operator?
- [ ] Operator approved live writes for THIS session?
- [ ] Warnings reported?

> [!warning] Known field-name uncertainty
> Steps 4A/4B depend on `VERIFY`-flagged fields in 5.6 and 5.x
> (`ISA`, `复制时间列`, `配送计划`, `总板数`, `库存信息-XXX`). Confirm them before
> trusting the branch logic. See [[Field Glossary]].

Related: [[Decision Tree]] · [[Wrapper Library]] · [[Production Guardrails]] · [[Agent System Prompt]]
