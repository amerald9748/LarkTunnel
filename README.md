# 🚇 LarkTunnel

Batch-command tooling for a **production Lark (Feishu) Base**. Provides a clean
wrapper library over `lark-cli` and a documented, agent-runnable workflow for the
**warehouse appointment & delivery-plan sync**.

> ⚠️ **Production.** Writes are **simulated by default** (safe mode). Never run a
> live write without a human in the loop. See `docs/60 Safety/Production Guardrails.md`.

## Layout

| Path | What |
|---|---|
| `config/config.js` | **Single source of truth** — tokens, table ids, field names, warehouse map. Edit this. |
| `config/secrets.txt` | App ID / Secret (gitignored). |
| `src/lark/` | Wrapper library — `client.js`, `LarkBase.js`, `LarkTable.js`, `index.js`. |
| `src/workflow/inputs.example.json` | Template for one appointment session. |
| `scripts/verify-config.js` | **Read-only** check of config against the live Base. |
| `docs/` | **Obsidian vault** — open this folder in Obsidian. Start at `00 Home/Home.md`. |
| `archive/lark_core_legacy/` | Previous forecast/sync project, kept for reference. |

## Quick start

```bash
npm install
lark-cli auth login              # authenticate (see docs/10 Setup/Authentication.md)
npm run verify:config            # READ-ONLY: confirm tables + field names resolve
```

Then read **`docs/00 Home/Home.md`** and follow
**`docs/40 Workflows/Appointment Sync Runbook.md`**.

## Using the library

```js
const lark = require('./src/lark');
const base = lark.base();
const inv  = base.tableByLabel('3.1');

const row = inv.findUnique([
  [lark.config.fields.inventory.awb, 'is', '093-9992123'],
  [lark.config.fields.inventory.destination, 'is', 'YYC3'],
  [lark.config.fields.inventory.warehouse, 'is', 'BESTAR'],
]);

// writes are simulated unless LARK_SAFE_MODE=false
inv.updateRecord(row.record_id, { [lark.config.fields.inventory.actualPallets]: 12 });
```

## Documentation

The `docs/` folder is an **Obsidian vault** (open the folder in Obsidian for
wikilinks + graph view; the Markdown is readable anywhere). Key notes:

- `40 Workflows/Appointment Sync Runbook.md` — the step-by-step procedure.
- `40 Workflows/Decision Tree.md` — the branching logic as flowcharts.
- `30 Reference/` — table registry, field glossary, warehouse map.
- `50 Prompts/Agent System Prompt.md` — paste-ready framing for an agent.
- `60 Safety/Production Guardrails.md` — read before any live run.
