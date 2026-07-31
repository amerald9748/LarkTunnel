const fs = require('fs');
const path = require('path');
const xlsx = require('xlsx');

class LocalDB {
    constructor(dbPath) {
        this.dbPath = dbPath;
        this.records = [];
        this.load();
    }

    load() {
        if (fs.existsSync(this.dbPath)) {
            const raw = fs.readFileSync(this.dbPath, 'utf-8');
            this.records = JSON.parse(raw);
        } else {
            this.records = [];
        }
    }

    save() {
        fs.writeFileSync(this.dbPath, JSON.stringify(this.records, null, 2), 'utf-8');
    }

    // Parse Excel and initialize local DB
    importExcel(excelPath) {
        const wb = xlsx.readFile(excelPath);
        const wsName = wb.SheetNames[0];
        const ws = wb.Sheets[wsName];
        const data = xlsx.utils.sheet_to_json(ws);
        this.records = data;
        this.save();
        console.log(`Imported ${this.records.length} records from ${excelPath}.`);
    }

    // Export DB to CSV for human reading
    exportCSV(csvPath) {
        if (this.records.length === 0) {
            console.log("No records to export.");
            return;
        }
        const ws = xlsx.utils.json_to_sheet(this.records);
        const csvString = xlsx.utils.sheet_to_csv(ws);
        // Add UTF-8 BOM so Excel opens it with correct encoding for Chinese characters
        fs.writeFileSync(csvPath, '\uFEFF' + csvString, 'utf-8');
        console.log(`Exported local DB to ${csvPath}`);
    }

    getAllRecords() {
        return this.records;
    }

    getRecordByUniqueKey(keyFn) {
        return this.records.find(keyFn);
    }

    updateRecord(keyFn, updates) {
        const idx = this.records.findIndex(keyFn);
        if (idx !== -1) {
            this.records[idx] = { ...this.records[idx], ...updates };
            this.save();
            return true;
        }
        return false;
    }

    createRecord(record) {
        this.records.push(record);
        this.save();
    }
}

module.exports = { LocalDB };

// CLI execution for testing manually
if (require.main === module) {
    const db = new LocalDB(path.join(__dirname, 'local_db.json'));
    db.importExcel(path.join(__dirname, 'ImportData.xlsx'));
    db.exportCSV(path.join(__dirname, 'local_db.csv'));
    console.log("Local DB setup complete. First record:");
    console.log(db.getAllRecords()[0]);
}

// Execute with: node local_db.js to build the database and export to CSV.