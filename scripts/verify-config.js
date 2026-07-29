/**
 * verify-config.js — READ-ONLY sanity check for config.js against the live Base.
 * ============================================================================
 * Run this (with human awareness) BEFORE any workflow run to confirm:
 *   - the base token resolves
 *   - every table id in the registry is reachable
 *   - the field names the workflow depends on actually exist
 *
 * It performs ONLY read calls (+table-list, +field-list). It never writes.
 * Fields flagged `VERIFY` in config.js are exactly what this script exists to
 * confirm — mismatches are printed so you can correct config.js.
 *
 *   node scripts/verify-config.js
 * ============================================================================
 */

const lark = require('../src/lark');
const config = lark.config;

function line() { console.log('-'.repeat(70)); }

function checkFields(table, expected) {
  const actual = new Set(table.getFieldNames());
  for (const name of expected) {
    const ok = actual.has(name);
    console.log(`   ${ok ? '[OK]' : '[MISSING]'} ${name}`);
  }
}

function main() {
  console.log('LarkTunnel config verification (READ-ONLY)');
  console.log(`Safe mode: ${lark.isSafeMode() ? 'ON (writes simulated)' : 'OFF (writes LIVE!)'}`);
  line();

  const base = lark.base();

  console.log(`Base token: ${config.baseToken}`);
  const tables = base.listTables();
  console.log(tables ? '[OK] base token resolved, table-list returned' : '[FAIL] could not list tables — check auth / token');
  line();

  // Inventory table 3.1
  console.log('Table 3.1 (inventory):', config.tables['3.1']);
  const inv = base.tableByLabel('3.1');
  const invF = config.fields.inventory;
  checkFields(inv, [
    invF.warehouse, invF.awb, invF.destination, invF.actualPallets, invF.estimatedPallets,
    ...Object.values(invF.planLinkFields),
  ]);
  line();

  // Appointment table 5.6
  console.log('Table 5.6 (ISA appointments):', config.tables['5.6']);
  const appt = base.tableByLabel('5.6');
  const apF = config.fields.appointment;
  checkFields(appt, [apF.isa, apF.timestamp, apF.destination, apF.account, apF.deliveryPlanLink]);
  line();

  // Delivery-plan tables 5.x
  for (const [wh, cfg] of Object.entries(config.warehouses)) {
    if (cfg.unmapped) { console.log(`Warehouse ${wh}: UNMAPPED — ${cfg.note}`); continue; }
    console.log(`Delivery plan ${cfg.planTable} (warehouse ${wh}):`, config.tables[cfg.planTable]);
    const plan = base.tableByLabel(cfg.planTable);
    checkFields(plan, [
      config.fields.deliveryPlan.totalPallets,
      config.fields.deliveryPlan.isaLink,
      cfg.inventoryBackLinkField,
    ]);
  }
  line();
  console.log('Done. Correct any [MISSING] field names in config.js before running the workflow.');
}

main();
