# Lark Core Integration Package

This package contains the core scripts and wrappers for Lark/Feishu authentication, access, and manipulation. It allows you to easily connect to Lark Base, query records, and synchronize local data.

## 📁 Package Structure

- **`playground.js`**: The core logic wrapper. Contains the `LarkBase` and `LarkTable` classes which wrap the underlying `lark-cli` to provide a clean interface for querying, updating, deleting, and subscribing to Lark records.
- **`sync.js`**: A synchronization script that uses the core wrapper to batch sync local data (like CSV/JSON files) into a specified Lark table.
- **`local_db.js`**: A utility for reading and interacting with local files (.csv, .xlsx, .json).
- **`alias-map.json`**: Provides field mapping rules between local data column names and Lark table column names.
- **`secrets.txt`**: Contains the App ID and App Secret needed for authentication.
- **`examples/`**: Contains batch scripts demonstrating how to invoke the `lark-cli` directly.
- **`local_db.csv` & `local_db.json`**: Example data sources that can be read by `local_db.js` for testing synchronization.

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have Node.js installed. Install the necessary project dependencies:
```bash
npm install
```
You will also need the `lark-cli` installed and authenticated on your system.
You can log in to `lark-cli` using the credentials in `secrets.txt`.

### 2. Authentication
The `LarkBase` wrapper relies on the `lark-cli` being authenticated. If not already authenticated, you can use the App ID and App Secret provided in `secrets.txt`.

### 3. Usage

#### Using the Wrapper Directly
You can use `playground.js` to script your own logic:
```javascript
const { LarkBase } = require('./playground.js');

const base = new LarkBase("YOUR_BASE_TOKEN");
const table = base.table("YOUR_TABLE_ID");

// Fetch records
const records = table.listRecords(["Field_Name"]);
console.log(records);
```

#### Running the Data Sync
To synchronize the local database to the Lark table, verify the tokens in `sync.js` and run:
```bash
node sync.js
```

## 📝 Notes
- Ensure `alias-map.json` is correctly mapped before running `sync.js`.
- Make sure not to expose `secrets.txt` in public repositories.
