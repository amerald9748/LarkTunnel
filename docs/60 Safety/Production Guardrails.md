---
title: Production Guardrails
tags: [safety]
---

# Production Guardrails

> [!danger] The configured Base is LIVE production data.
> Assume every write is visible to real operators immediately. Prefer reading,
> simulating, and asking over writing.

## 1. Safe mode (default ON)

All writes route through `gatedWrite` in
[`src/lark/client.js`](../../src/lark/client.js). When safe mode is on:

- The write is **not sent** to Lark.
- The intended change is logged as an `intent` object.
- The call returns `{ simulated: true, intent }`.

Check / toggle:

```js
const lark = require('./src/lark');
lark.isSafeMode();        // true by default
lark.setSafeMode(false);  // ⚠ enable LIVE writes (human sign-off only)
```

Or via env before the process starts:

```bash
LARK_SAFE_MODE=false node your-script.js   # ⚠ live
```

**Standard operating procedure:** run once in safe mode, show the operator the
intended writes, get explicit approval, then re-run with writes enabled.

## 2. Hard-stop conditions

Stop and ask a human — do not proceed or guess:

| Situation | Why |
|---|---|
| 3.1 lookup returns **0** or **>1** rows | `findUnique` throws; ambiguous target. |
| Warehouse is `TOR-1140` or `卡尔加里（CAL-5505）` | No account / plan table mapped. |
| `npm run verify:config` shows `[MISSING]` | Schema drifted from config. |
| A `VERIFY` field is untested and a branch depends on it | Branch logic may be wrong. |

## 3. Scope discipline

- One session = one 3.1 row. Never batch-edit as a side effect.
- Only the fields named in the runbook are written. No "cleanups."
- Two-way links are set from the **3.1** side; let Lark mirror the back-link.

## 4. Secrets

- `config/secrets.txt` (App ID / Secret) is **gitignored**. Never print it,
  commit it, or paste it into chat.

## 5. Testing on production (per operator)

> The operator plans to create a **separate test table** on the production Base
> for future testing. Until that exists, treat all tables as live and keep safe
> mode ON. When the test table exists, add it to `config.tables` and point a
> `TEST` run at it before touching real tables.

Related: [[Appointment Sync Runbook]] · [[Configuration]] · [[Wrapper Library]]
