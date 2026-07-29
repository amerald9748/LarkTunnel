const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const { LarkBase, trackTime } = require('./playground.js');
const { LocalDB } = require('./local_db.js');

const ALIAS_MAP_PATH = path.join(__dirname, 'alias-map.json');

function resolveLocalFieldName(larkField, localKeys, aliasMap, allowFuzzy) {
    if (!allowFuzzy) {
        if (localKeys.includes(larkField)) return larkField;

        let aliasEntry = aliasMap[larkField];
        if (!aliasEntry) {
            const possibleKey = Object.keys(aliasMap).find(k => k === larkField);
            if (possibleKey) aliasEntry = aliasMap[possibleKey];
        }

        if (aliasEntry && aliasEntry.alias) {
            for (const alias of aliasEntry.alias) {
                if (localKeys.includes(alias)) return alias;
            }
        }
        return null;
    }

    // Fuzzy matching phase
    let aliasEntry = aliasMap[larkField];
    if (!aliasEntry) {
        const possibleKey = Object.keys(aliasMap).find(k => larkField.includes(k) || k.includes(larkField));
        if (possibleKey) aliasEntry = aliasMap[possibleKey];
    }
    
    if (aliasEntry && aliasEntry.alias) {
        for (const alias of aliasEntry.alias) {
            const fuzzyAlias = localKeys.find(h => h.includes(alias) || alias.includes(h));
            if (fuzzyAlias) return fuzzyAlias;
        }
    }

    const fuzzyMatch = localKeys.find(h => h.includes(larkField) || larkField.includes(h));
    if (fuzzyMatch) return fuzzyMatch;

    return null;
}

function batchUpdate(baseToken, tableId, updates) {
    if (updates.length === 0) return;
    console.log(`[BATCH UPDATE] Pushing ${updates.length} records...`);
    fs.writeFileSync('temp_patch.json', JSON.stringify({ records: updates }), 'utf-8');
    const cmd = `lark-cli api post /open-apis/bitable/v1/apps/${baseToken}/tables/${tableId}/records/batch_update --data @temp_patch.json --as user`;
    execSync(cmd, { stdio: 'pipe' });
    fs.unlinkSync('temp_patch.json');
}

function batchCreate(baseToken, tableId, creations) {
    if (creations.length === 0) return;
    console.log(`[BATCH CREATE] Pushing ${creations.length} records...`);
    fs.writeFileSync('temp_create.json', JSON.stringify({ records: creations }), 'utf-8');
    const cmd = `lark-cli api post /open-apis/bitable/v1/apps/${baseToken}/tables/${tableId}/records/batch_create --data @temp_create.json --as user`;
    execSync(cmd, { stdio: 'pipe' });
    fs.unlinkSync('temp_create.json');
}

async function sync() {
    console.log("=== Starting Batch Sync ===");

    const db = new LocalDB(path.join(__dirname, 'local_db.json'));
    const localRecords = db.getAllRecords();
    if (localRecords.length === 0) {
        console.log("Local DB is empty. Run local_db.js first.");
        return;
    }

    const WIKI_TOKEN = "FFcNw6f15in36NklOy3laH9jgxb"; 
    const BASE_TOKEN = LarkBase.getBaseTokenFromWiki(WIKI_TOKEN) || "C13Zb8l6WassnesyRJhufdvLsFe";
    const TABLE_ID = "tblVCef9hxcG39VF"; 
    const base = new LarkBase(BASE_TOKEN);
    const table = base.table(TABLE_ID);

    const aliasMap = JSON.parse(fs.readFileSync(ALIAS_MAP_PATH, 'utf-8'));
    const allLarkFields = table.getFields();
    console.log(`Retrieved ${allLarkFields.length} fields from Lark table.`);

    const fieldTypesMap = table.getFieldTypes();
    
    // Filter out read-only / complex fields (Lookup:19, Formula:20, Auto/Times:1001-1005)
    const IGNORED_TYPES = new Set([19, 20, 1001, 1002, 1003, 1004, 1005]);
    const larkFields = allLarkFields.filter(f => !IGNORED_TYPES.has(fieldTypesMap[f]));
    console.log(`Filtered down to ${larkFields.length} writable fields for mapping.`);

    const fieldMappingReport = { mapped: new Set(), unmapped: new Set() };
    const larkToLocalMap = {};
    
    if (localRecords.length > 0) {
        const localKeys = Object.keys(localRecords[0]);
        const unmappedLarkFields = [];

        // Pass 1: Exact and Alias Exact Matches
        for (const larkField of larkFields) {
            const aliasEntry = aliasMap[larkField];
            if (aliasEntry && aliasEntry.expression) {
                larkToLocalMap[larkField] = { type: 'expression', expr: aliasEntry.expression };
                fieldMappingReport.mapped.add(`[EXPR] -> ${larkField}`);
                continue;
            }

            const resolvedLocal = resolveLocalFieldName(larkField, localKeys, aliasMap, false);
            if (resolvedLocal) {
                fieldMappingReport.mapped.add(`${resolvedLocal} -> ${larkField}`);
                larkToLocalMap[larkField] = { type: 'key', key: resolvedLocal };
            } else {
                unmappedLarkFields.push(larkField);
            }
        }

        // Pass 2: Fuzzy Matches for remaining Lark fields
        for (const larkField of unmappedLarkFields) {
            const resolvedLocal = resolveLocalFieldName(larkField, localKeys, aliasMap, true);
            if (resolvedLocal) {
                fieldMappingReport.mapped.add(`${resolvedLocal} -> ${larkField} (Fuzzy)`);
                larkToLocalMap[larkField] = { type: 'key', key: resolvedLocal };
            }
        }

        // Identify unmapped local keys for reporting
        const mappedLocalKeys = Object.values(larkToLocalMap).filter(m => m.type === 'key').map(m => m.key);
        for (const localKey of localKeys) {
            if (!mappedLocalKeys.includes(localKey)) {
                fieldMappingReport.unmapped.add(localKey);
            }
        }
    }

    let uniqueKeyLarkField = larkFields.find(f => f.includes("唯一编号"));
    if (!uniqueKeyLarkField) uniqueKeyLarkField = "唯一编号";

    // 1. Pre-fetch all existing unique keys from Lark
    console.log(`\nFetching existing unique keys from Lark Base...`);
    const existingRecordsMap = new Map();
    trackTime("Fetch Unique Keys", () => {
        const records = table.listRecords([uniqueKeyLarkField]);
        for (const rec of records) {
            const val = rec.fields[uniqueKeyLarkField];
            if (val) {
                existingRecordsMap.set(String(val), rec.record_id);
            }
        }
    });

    // 2. Bucket local records
    const updates = [];
    const creations = [];

    for (const row of localRecords) {
        let waybill = row.Waybill || "";
        let custId = row.Cust_ID || "";

        for (const [larkField, mapping] of Object.entries(larkToLocalMap)) {
            if (mapping.type === 'key') {
                if (larkField.includes("柜号") || larkField === "AWB" || larkField === "Waybill") waybill = row[mapping.key] || waybill;
                if (larkField.includes("批次号") || larkField === "Cust_ID") custId = row[mapping.key] || custId;
            }
        }

        const uniqueKey = `${waybill}${custId}`;
        if (!uniqueKey) continue; 

        const payload = {};
        for (const [larkField, mapping] of Object.entries(larkToLocalMap)) {
            let value;
            if (mapping.type === 'expression') {
                try {
                    const fn = new Function('row', `return ${mapping.expr}`);
                    value = fn(row);
                } catch (e) {
                    console.error(`Error evaluating expression for ${larkField}:`, e);
                    continue;
                }
            } else {
                value = row[mapping.key];
            }

            if (value === "" || value === null || value === undefined) continue;
            
            if (fieldTypesMap[larkField] === 5) { // 5 is DateTime
                const parsed = Date.parse(value);
                if (!isNaN(parsed)) {
                    payload[larkField] = parsed;
                }
            } else if (fieldTypesMap[larkField] === 21) { // 21 is Duplex Link
                payload[larkField] = [String(value)];
            } else {
                payload[larkField] = value;
            }
        }

        let originalUniqueKeyValue = null;
        if (larkToLocalMap[uniqueKeyLarkField]) {
            if (larkToLocalMap[uniqueKeyLarkField].type === 'key') {
                originalUniqueKeyValue = row[larkToLocalMap[uniqueKeyLarkField].key];
            } else {
                originalUniqueKeyValue = payload[uniqueKeyLarkField];
            }
        }

        let existingId = existingRecordsMap.get(uniqueKey);
        if (!existingId && originalUniqueKeyValue) {
            existingId = existingRecordsMap.get(originalUniqueKeyValue);
        }

        // Force overwrite the unique key in the payload to the concatenated standard
        payload[uniqueKeyLarkField] = uniqueKey;

        if (existingId) {
            updates.push({ record_id: existingId, fields: payload });
        } else {
            creations.push({ fields: payload });
        }
    }

    // 3. Perform Batch Execution
    trackTime("Batch Update", () => batchUpdate(BASE_TOKEN, TABLE_ID, updates));
    trackTime("Batch Create", () => batchCreate(BASE_TOKEN, TABLE_ID, creations));

    console.log("\n=== Sync Complete ===");
    console.log(`Created: ${creations.length} | Updated: ${updates.length}`);
    
    console.log("\n=== Field Mapping Report ===");
    console.log("SUCCESSFULLY MAPPED:");
    fieldMappingReport.mapped.forEach(m => console.log(`  [OK] ${m}`));
    console.log("\nUNMAPPED (Ignored):");
    fieldMappingReport.unmapped.forEach(m => console.log(`  [--] ${m}`));
    console.log("============================\n");
}

if (require.main === module) {
    sync().catch(console.error);
}

// Execute with: node sync.js to update the Lark table with the local data in batch.