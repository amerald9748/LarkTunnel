/**
 * client.js — Low-level lark-cli execution layer.
 *
 * Everything that actually shells out to `lark-cli` funnels through here so we
 * get ONE place to enforce:
 *   - consistent JSON parsing + error handling
 *   - the SAFE_MODE guardrail (writes are simulated unless explicitly enabled)
 *   - robust payload passing via temp files (avoids shell-escaping issues with
 *     Chinese field names / quotes in JSON)
 *
 * SAFETY MODEL
 * ------------
 * This library talks to a PRODUCTION Lark Base. To avoid accidental writes:
 *   - Read commands run normally.
 *   - Write commands (create / update / delete / batch_*) are GATED. When
 *     SAFE_MODE is on (the default), a write is logged and returned as a
 *     simulated no-op result — it never reaches Lark.
 *   - To perform real writes you must opt in explicitly, either by:
 *       process.env.LARK_SAFE_MODE = 'false'      (env, before require)
 *       require('./client').setSafeMode(false)     (programmatic)
 *   - Even then, keep a human in the loop for production edits.
 */

const { execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// ---------------------------------------------------------------------------
// Safe mode
// ---------------------------------------------------------------------------

let SAFE_MODE = String(process.env.LARK_SAFE_MODE ?? 'true').toLowerCase() !== 'false';

function setSafeMode(enabled) {
  SAFE_MODE = !!enabled;
  return SAFE_MODE;
}

function isSafeMode() {
  return SAFE_MODE;
}

// ---------------------------------------------------------------------------
// Temp file helpers (used to pass JSON payloads without shell escaping)
// ---------------------------------------------------------------------------

// NOTE: lark-cli only accepts payload paths RELATIVE to the cwd, so temp
// files live in ./.tmp (gitignored) and fn receives a relative path.
function withTempJson(obj, fn) {
  const dir = path.join(process.cwd(), '.tmp');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const name = `lark-payload-${process.pid}-${tempCounter++}.json`;
  const file = path.join(dir, name);
  fs.writeFileSync(file, JSON.stringify(obj), 'utf-8');
  try {
    return fn(`./.tmp/${name}`);
  } finally {
    try { fs.unlinkSync(file); } catch (_) { /* best effort */ }
  }
}
let tempCounter = 0;

// ---------------------------------------------------------------------------
// Raw execution
// ---------------------------------------------------------------------------

/**
 * Execute a fully-formed lark-cli command string and parse JSON stdout.
 * @returns parsed object, or null on failure.
 */
function execCli(cmd, { label } = {}) {
  try {
    const output = execSync(cmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
    if (!output || !output.trim()) return null;
    return JSON.parse(output);
  } catch (error) {
    console.error(`\n[lark-cli ERROR]${label ? ` (${label})` : ''}: ${cmd}`);
    console.error(error.stdout || error.message || error);
    return null;
  }
}

/**
 * Run a `lark-cli base <subcmd>` command scoped to a base (and optionally table).
 * READ-ONLY by contract — do not route writes through here.
 */
function baseCmd(subcmd, { baseToken, tableId, as = 'user' } = {}) {
  let cmd = `lark-cli base ${subcmd} --base-token ${baseToken}`;
  if (tableId) cmd += ` --table-id ${tableId}`;
  cmd += ` --format json --as ${as}`;
  return execCli(cmd, { label: `base ${subcmd.split(' ')[0]}` });
}

/**
 * Run a `lark-cli api <METHOD> <endpoint>` command. Used for endpoints not
 * wrapped by the `base` subcommands (field metadata, batch ops, etc).
 * `dataObj` (if provided) is written to a temp file and passed via --data @file.
 * READ-ONLY unless called via the write gate.
 */
function apiCmd(method, endpoint, { dataObj, params, pageAll = false, as = 'user' } = {}) {
  const run = (dataFile) => {
    let cmd = `lark-cli api ${method.toUpperCase()} ${endpoint}`;
    if (params) cmd += ` --params "${JSON.stringify(params).replace(/"/g, '\\"')}"`;
    if (dataFile) cmd += ` --data @${dataFile}`;
    if (pageAll) cmd += ` --page-all`;
    cmd += ` --as ${as}`;
    return execCli(cmd, { label: `api ${method}` });
  };
  return dataObj ? withTempJson(dataObj, run) : run(null);
}

// ---------------------------------------------------------------------------
// Write gate
// ---------------------------------------------------------------------------

/**
 * Gate a write operation behind SAFE_MODE.
 *
 * @param {object} intent  - human-readable description of the intended write,
 *                           e.g. { op:'update', table:'3.1', recordId, fields }.
 * @param {function} perform - thunk that actually performs the write and returns
 *                           the parsed lark-cli result.
 * @returns the real result, OR a simulated result when SAFE_MODE is on.
 */
function gatedWrite(intent, perform) {
  if (SAFE_MODE) {
    console.log(`\n[SAFE_MODE] Write suppressed — would ${intent.op.toUpperCase()}:`);
    console.log(JSON.stringify(intent, null, 2));
    return { simulated: true, intent };
  }
  console.log(`\n[LIVE WRITE] ${intent.op.toUpperCase()} ->`, intent.table || '');
  return perform();
}

module.exports = {
  setSafeMode,
  isSafeMode,
  withTempJson,
  execCli,
  baseCmd,
  apiCmd,
  gatedWrite,
};
