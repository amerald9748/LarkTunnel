---
title: Lark CLI Cheatsheet
tags: [reference]
---

# Lark CLI Cheatsheet

The raw commands the [[Wrapper Library]] runs under the hood. Useful for manual
inspection. All are `--as user` and `--format json`.

> [!tip]
> Prefer the wrappers for anything scripted — they add pagination, JSON parsing,
> and the safe-mode write gate. Use raw commands mainly for ad-hoc reads.

## Resolve a wiki node → base token
```bash
lark-cli wiki +node-get --node-token "https://feishu.cn/wiki/<WIKI>" --format json --as user
```

## List tables in a base
```bash
lark-cli base +table-list --base-token <BASE> --format json --as user
```

## List fields of a table (names)
```bash
lark-cli base +field-list --base-token <BASE> --table-id <TBL> --format json --as user
```

## Field types (numeric) via OpenAPI
```bash
lark-cli api GET /open-apis/bitable/v1/apps/<BASE>/tables/<TBL>/fields --page-all --as user
```

## Read records (paginated)
```bash
lark-cli base +record-list --base-token <BASE> --table-id <TBL> --limit 200 --format json --as user
```

## Server-side filter (search)
Filter JSON is passed via a temp file to avoid shell-escaping Chinese text:
```bash
# filter.json: {"logic":"and","conditions":[{"field_name":"柜号/AWB","operator":"is","value":["093-9992123"]}]}
lark-cli base +record-list --base-token <BASE> --table-id <TBL> --filter-json "@filter.json" --format json --as user
```

## ⚠ Writes (create / update / delete)
Only run with safe mode understood. The wrappers pass payloads via `--data @file`.
```bash
lark-cli api POST   /open-apis/bitable/v1/apps/<BASE>/tables/<TBL>/records            --data @create.json --as user
lark-cli api PUT    /open-apis/bitable/v1/apps/<BASE>/tables/<TBL>/records/<RECID>    --data @update.json --as user
lark-cli api DELETE /open-apis/bitable/v1/apps/<BASE>/tables/<TBL>/records/<RECID>    --as user
```

## Field type codes (common)
| Code | Type | Code | Type |
|---|---|---|---|
| 1 | Text | 17 | Attachment |
| 2 | Number | 18 | Single link |
| 3 | Single select | 19 | Lookup |
| 4 | Multi select | 20 | Formula |
| 5 | DateTime | 21 | Two-way link |
| 7 | Checkbox | 1001-1005 | Created/modified time & user |

Related: [[Wrapper Library]] · [[Production Guardrails]]
