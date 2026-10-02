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

- [[Start Here]] — **read this first**: what the tool is for, who uses which
  entry point, the daily workflow, the rules, and how to run/release.
- [[Project History]] — why things are the way they are (decision log).
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
- [[Deletion Tracking]] — 3.1 records going missing: forensics (操作历史),
  event-based deletion logger design, snapshot fallback, prevention.

## For agents

- [[Agent System Prompt]] — paste-ready framing for a future agent picking this up.

---

## Status (LTS — 2026-10-02)

> [!success] Stable. All table ids and 3.1 field names verified live; every
> warehouse mapped (CAL-5505 → BESTAR; TOR-1140 = pallets-only by design).
> Two Task-Scheduler services run on the owner machine (webapp :8787 +
> deletion watcher). Members use the distributed `LarkTunnel.exe`.
> Decision log: [[Project History]] · Onboarding: [[Start Here]].
