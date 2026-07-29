---
title: LarkTunnel — Home
tags: [moc, index]
---

# 🚇 LarkTunnel

A small, careful tool for running **batch commands against a production Lark
(Feishu) Base**. Its first job is the **warehouse appointment & delivery-plan
sync** workflow: given one shipment's details, reconcile pallet counts and wire
up ISA appointments ↔ delivery trips ↔ inventory rows.

> [!danger] This talks to PRODUCTION
> The configured Base is live. Writes are **simulated by default** (safe mode).
> Never run a live write without a human in the loop. See
> [[Production Guardrails]].

## Start here

- [[Authentication]] — get `lark-cli` logged in.
- [[Configuration]] — the one file you edit: `config/config.js`.
- [[Project Structure]] — where everything lives.

## The workflow

- [[Appointment Sync Runbook]] — **the main event.** Step-by-step procedure an
  agent (or human) follows for one appointment session.
- [[Decision Tree]] — the same logic as a flowchart + branch table.
- [[Session Input Template]] — the 6 inputs required to start.
- [[Inventory Import Runbook]] — 收货派送计划 Excel → per-route records in 3.1
  (also available as the `lark-inventory-import` skill).

## Reference

- [[Table Registry]] — table labels ↔ ids.
- [[Field Glossary]] — every 字段名 the workflow touches.
- [[Warehouse & Account Map]] — warehouse → account → plan table → link fields.
- [[Wrapper Library]] — the JS API you call.
- [[Lark CLI Cheatsheet]] — raw commands behind the wrappers.

## For agents

- [[Agent System Prompt]] — paste-ready framing for a future agent picking this up.

---

## Status / open questions

> [!warning] Needs confirmation before first live run
> - Field names marked **VERIFY** in `config/config.js` (especially on tables
>   **5.6** and the **5.x** delivery-plan tables) are best-guesses from the spec.
>   Confirm them read-only with `npm run verify:config`.
> - Warehouses **`TOR-1140`** and **`卡尔加里（CAL-5505）`** have **no appointment
>   account or delivery-plan table** in the spec. The workflow refuses to guess
>   for them — see [[Warehouse & Account Map]].
