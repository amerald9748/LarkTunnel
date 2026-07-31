const { execSync } = require('child_process');
const util = require('util');
const fs = require('fs');
const path = require('path');
const { performance } = require('perf_hooks');

let cumulativeComputeTimeMs = 0;

function trackTime(name, fn) {
    const start = performance.now();
    const result = fn();
    const end = performance.now();
    const duration = end - start;
    cumulativeComputeTimeMs += duration;
    console.log(`\n[Timer] ${name} took ${(duration / 1000).toFixed(3)} seconds (${duration.toFixed(0)} ms)`);
    return result;
}

class LarkBase {
    constructor(baseToken) {
        this.baseToken = baseToken;
    }

    static getBaseTokenFromWiki(wikiToken) {
        try {
            const url = wikiToken.startsWith("http") ? wikiToken : `https://feishu.cn/wiki/${wikiToken}`;
            const fullCmd = `lark-cli wiki +node-get --node-token "${url}" --format json --as user`;
            const output = execSync(fullCmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
            const response = JSON.parse(output);
            if (response && response.data && response.data.obj_token) {
                return response.data.obj_token;
            }
            return null;
        } catch (error) {
            console.error(`\n[ERROR] Failed to resolve base token from wiki token: ${wikiToken}`);
            console.error(error.stdout || error.message || error);
            return null;
        }
    }

    #runCmd(cmdStr) {
        try {
            const fullCmd = `lark-cli base ${cmdStr} --base-token ${this.baseToken} --format json --as user`;
            const output = execSync(fullCmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
            return JSON.parse(output);
        } catch (error) {
            console.error(`\n[ERROR] Command failed: lark-cli base ${cmdStr}`);
            console.error(error.stdout || error.message || error);
            return null;
        }
    }

    listTables() {
        const response = this.#runCmd(`+table-list`);
        return response?.data || null;
    }

    table(tableId) {
        return new LarkTable(this.baseToken, tableId);
    }
}

class LarkTable {
    constructor(baseToken, tableId) {
        this.baseToken = baseToken;
        this.tableId = tableId;
    }

    #runCmd(cmdStr) {
        try {
            const fullCmd = `lark-cli base ${cmdStr} --base-token ${this.baseToken} --table-id ${this.tableId} --format json --as user`;
            const output = execSync(fullCmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
            return JSON.parse(output);
        } catch (error) {
            console.error(`\n[ERROR] Command failed: lark-cli base ${cmdStr}`);
            console.error(error.stdout || error.message || error);
            return null;
        }
    }

    /** Helper to parse matrix into friendly objects */
    #formatRecord(recordId, fieldsArray, dataArray) {
        const parsedFields = {};
        for (let i = 0; i < fieldsArray.length; i++) {
            if (dataArray[i] !== null && dataArray[i] !== undefined) {
                parsedFields[fieldsArray[i]] = dataArray[i];
            }
        }
        return {
            record_id: recordId,
            fields: parsedFields
        };
    }

    listFields() {
        const response = this.#runCmd(`+field-list`);
        return response?.data || null;
    }

    getFieldTypes() {
        try {
            const fullCmd = `lark-cli api get /open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/fields --page-all --as user`;
            const output = execSync(fullCmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
            const response = JSON.parse(output);
            
            const typeMap = {};
            if (response && response.data && response.data.items) {
                for (const item of response.data.items) {
                    typeMap[item.field_name] = item.type;
                }
            }
            return typeMap;
        } catch (error) {
            console.error(`\n[ERROR] Command failed: lark-cli api get fields`);
            console.error(error.stdout || error.message || error);
            return {};
        }
    }

    getFields() {
        const response = this.#runCmd(`+record-list --limit 1`);
        return response?.data?.fields || [];
    }

    listRecords(fieldIds = []) {
        let allRecords = [];
        let pageToken = "";
        let hasMore = true;
        let fieldFlag = fieldIds.map(f => `--field-id "${f}"`).join(" ");

        while (hasMore) {
            const tokenFlag = pageToken ? `--page-token "${pageToken}"` : "";
            const response = this.#runCmd(`+record-list ${fieldFlag} ${tokenFlag} --limit 200`);
            if (!response || !response.data) break;

            const raw = response.data;
            if (raw.data && raw.fields && raw.record_id_list) {
                for (let i = 0; i < raw.record_id_list.length; i++) {
                    allRecords.push(this.#formatRecord(raw.record_id_list[i], raw.fields, raw.data[i]));
                }
            }
            hasMore = raw.has_more;
            pageToken = raw.page_token;
        }
        return allRecords;
    }

    getRecordByIndex(index) {
        const records = this.listRecords();
        return records[index] || null;
    }

    getRecordById(recordId) {
        const records = this.listRecords();
        return records.find(r => r.record_id === recordId) || null;
    }

    findRecordsByField(fieldName, value) {
        // Query the server directly to bypass the 100-record pagination limit
        const filterObj = {
            logic: "and",
            conditions: [[fieldName, "contains", value]]
        };
        const tempPath = path.join(__dirname, 'temp_filter.json');
        fs.writeFileSync(tempPath, JSON.stringify(filterObj), 'utf-8');

        // Pass the file using a relative path as required by lark-cli
        const filterArg = `@./temp_filter.json`;
        const response = this.#runCmd(`+record-list --filter-json "${filterArg}"`);

        // Clean up the temp file
        try { fs.unlinkSync(tempPath); } catch (e) { }

        if (!response || !response.data) return [];

        const raw = response.data;
        const results = [];
        if (raw.items) {
            return raw.items;
        } else if (raw.data && raw.fields && raw.record_id_list) {
            for (let i = 0; i < raw.record_id_list.length; i++) {
                results.push(this.#formatRecord(raw.record_id_list[i], raw.fields, raw.data[i]));
            }
        }
        return results;
    }

    createRecord(fieldsObj) {
        const jsonStr = JSON.stringify(fieldsObj).replace(/"/g, '\\"');
        const response = this.#runCmd(`+record-upsert --json "${jsonStr}"`);
        return response?.data || null;
    }

    updateRecord(recordId, fieldsObj) {
        const jsonStr = JSON.stringify(fieldsObj).replace(/"/g, '\\"');
        const response = this.#runCmd(`+record-upsert --record-id ${recordId} --json "${jsonStr}"`);
        return response?.data || null;
    }

    deleteRecord(recordId) {
        const response = this.#runCmd(`+record-delete --record-ids ${recordId}`);
        return response?.data || null;
    }
}

// Helper to print nested objects cleanly
const prettyPrint = (obj) => console.log(util.inspect(obj, { depth: null, colors: true }));

// ==========================================
// AUTOMATION EXAMPLES USING THE NEW WRAPPER
// ==========================================

function deduplicateRows(table, columnName) {
    console.log(`\n--- Deduplicating based on column: '${columnName}' ---`);
    const records = table.listRecords();

    if (records.length === 0) {
        console.log("No records found.");
        return;
    }

    const seenValues = new Set();
    const duplicateRecordIds = [];

    for (const record of records) {
        const cellValue = record.fields[columnName];
        if (cellValue !== undefined && cellValue !== null) {
            const valStr = typeof cellValue === 'object' ? JSON.stringify(cellValue) : String(cellValue);
            if (seenValues.has(valStr)) {
                duplicateRecordIds.push(record.record_id);
            } else {
                seenValues.add(valStr);
            }
        }
    }

    if (duplicateRecordIds.length > 0) {
        console.log(`Found ${duplicateRecordIds.length} duplicate(s). Deleting...`);
        const idsString = duplicateRecordIds.join(',');
        table.deleteRecord(idsString);
        console.log("Deduplication complete!");
    } else {
        console.log("No duplicates found.");
    }
}

function pollAndTriggerEvent(table, intervalMs = 10000) {
    console.log(`\n--- Starting event polling for '运输方式' == 'Air' ---`);
    const processedRecordIds = new Set();

    setInterval(() => {
        process.stdout.write(".");

        const matchedRecords = table.findRecordsByField("运输方式", "Air");

        for (const record of matchedRecords) {
            if (!processedRecordIds.has(record.record_id)) {
                console.log(`\n\n[TRIGGER] Condition met on record ${record.record_id}!`);
                processedRecordIds.add(record.record_id);

                // Update AGENT_HELLO_WORLD
                const targetRecords = table.findRecordsByField("客户自单号", "AGENT_HELLO_WORLD");
                if (targetRecords.length > 0) {
                    const target = targetRecords[0];
                    const currentString = target.fields["TEST_COLUMN"] || "";
                    const appendedString = currentString + " [Air Transport Triggered!]";

                    console.log(`Updating record ${target.record_id} -> TEST_COLUMN: '${appendedString}'`);
                    table.updateRecord(target.record_id, { "TEST_COLUMN": appendedString });
                }
            }
        }
    }, intervalMs);
}

// ==========================================
// MAIN WORKSPACE
// ==========================================
async function main() {
    console.log("Lark Base Playground\n");

    const BASE_TOKEN = "C13Zb8l6WassnesyRJhufdvLsFe";
    const TABLE_ID = "tblVCef9hxcG39VF";

    // Instantiate our wrapper
    const base = new LarkBase(BASE_TOKEN);
    const table = base.table(TABLE_ID);

    // ==========================================
    // EXERCISE 1: Explore the structure
    // ==========================================

    // console.log("=== Get Row by Index (Row 0) ===");
    // prettyPrint(table.getRecordByIndex(1));

    // console.log("\n=== Search Records by Field ===");
    // prettyPrint(table.findRecordsByField("运输方式", "海运")[1]);

    // ==========================================
    // EXERCISE 2: Automations
    // ==========================================

    // deduplicateRows(table, "客户自单号");
    // pollAndTriggerEvent(table, 5000);

    // ==========================================
    // EXERCISE 3: Lambda to fetch and print first 10 KQ records
    // ==========================================
    console.log("\n=== First 1000 'KQ' Records ===");
    const printTop10KQ = () => {
        const kqRecords = trackTime("Retrieval Process (findRecordsByField)", () => {
            return table.findRecordsByField("客户简称", "KQ");
        });
        // prettyPrint(kqRecords.slice(0, 1000));
    };
    printTop10KQ();

    console.log(`\n=== Cumulative Total Compute Time: ${(cumulativeComputeTimeMs / 1000).toFixed(3)} seconds ===\n`);
}

if (require.main === module) {
    main();
}

module.exports = { LarkBase, LarkTable, trackTime };
