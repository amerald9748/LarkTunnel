---
title: Wrapper Library
tags: [architecture, reference]
---

# Wrapper Library

`require('./src/lark')` exposes a clean API over `lark-cli`. Reads are free;
writes are **gated by safe mode** ([[Production Guardrails]]).

## Quick start

```js
const lark = require('./src/lark');

const base = lark.base();               // LarkBase on config.baseToken
const inv  = base.tableByLabel('3.1');  // LarkTable for the inventory table

// READ — find the unique shipment row
const row = inv.findUnique([
  [lark.config.fields.inventory.awb, 'is', '093-9992123'],
  [lark.config.fields.inventory.destination, 'is', 'YYC3'],
  [lark.config.fields.inventory.warehouse, 'is', 'BESTAR'],
]);

// WRITE — simulated unless LARK_SAFE_MODE=false
inv.updateRecord(row.record_id, { [lark.config.fields.inventory.actualPallets]: 12 });
```

## `LarkBase`

| Method | Kind | Notes |
|---|---|---|
| `LarkBase.resolveBaseTokenFromWiki(wiki)` | read | static; wiki node → obj_token |
| `listTables()` | read | all tables in the base |
| `table(id, meta?)` | — | LarkTable by raw id |
| `tableByLabel(label)` | — | LarkTable by config registry label |

## `LarkTable`

### Read — schema
| Method | Returns |
|---|---|
| `listFields()` | field definitions |
| `getFieldTypes()` | `{ fieldName: numericType }` |
| `getFieldNames()` | `string[]` of column names |

### Read — records
| Method | Returns |
|---|---|
| `listRecords(fieldNames?)` | all records (auto-paginates) |
| `getRecord(recordId)` | one record via OpenAPI |
| `search(conditions, logic?)` | server-side filtered records |
| `findBy(field, value, op?)` | single-field search (default op `is`) |
| `findUnique(conditions)` | exactly one match or `null`; **throws if >1** |

`conditions` is an array of `[fieldName, operator, value]`. Operators:
`is`, `isNot`, `contains`, `doesNotContain`, `isEmpty`, `isNotEmpty`,
`isGreater`, `isLess`, …

### Write — ⚠ gated
| Method | Op |
|---|---|
| `createRecord(fields)` | create |
| `updateRecord(recordId, fields)` | update |
| `deleteRecord(recordId)` | delete |
| `batchCreate(list)` / `batchUpdate(updates)` | batch |

In safe mode each logs the **intended** write as a JSON `intent` and returns
`{ simulated: true, intent }` without calling Lark.

### Two-way links
| Method | Purpose |
|---|---|
| `LarkTable.readLinkIds(fieldValue)` | normalize a link field → `string[]` of record ids |
| `setLink(recordId, field, ids)` | set a link field (updates both sides) |
| `addLink(recordId, field, ids, current)` | append without dropping existing links |

> [!tip] Two-way links
> Writing to **either** side of a two-way link updates the other automatically.
> The runbook links from the **3.1** side (`planLinkField`) which populates the
> plan table's `库存信息-XXX` back-link, and vice-versa.

Related: [[Appointment Sync Runbook]] · [[Field Glossary]]
