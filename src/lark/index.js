/**
 * index.js — Public entry point for the Lark wrapper library.
 *
 *   const lark = require('./src/lark');
 *   const base = lark.base();                 // LarkBase bound to config.baseToken
 *   const inv  = base.tableByLabel('3.1');    // LarkTable for the inventory table
 *   const rows = inv.findUnique([...]);       // read
 *   inv.updateRecord(id, {...});              // gated write (safe by default)
 *
 * Safe mode is ON by default — writes are simulated. Enable real writes with
 * LARK_SAFE_MODE=false or lark.setSafeMode(false), and only with human sign-off.
 */

const client = require('./client');
const { LarkBase } = require('./LarkBase');
const { LarkTable } = require('./LarkTable');
const config = require('../../config/config');

/** Build a LarkBase bound to the configured base token + table registry. */
function base() {
  return new LarkBase(config.baseToken, config.tables);
}

module.exports = {
  config,
  base,
  LarkBase,
  LarkTable,
  setSafeMode: client.setSafeMode,
  isSafeMode: client.isSafeMode,
};
