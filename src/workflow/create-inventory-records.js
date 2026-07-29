/**
 * create-inventory-records.js — Import an unloading plan (收货派送计划) into
 * the 3.1 inventory table as one summary record per destination route.
 *
 * This encodes the exact process validated on 2026-07-22 (BEAU6279991):
 *   parse Excel -> aggregate 箱数/重量/体积 by destination -> guard checks
 *   -> preview -> (on --yes) create records -> read back and verify sums.
 *
 * USAGE
 *   node src/workflow/create-inventory-records.js \
 *     --file "<path to 收货派送计划 .xlsx>" \
 *     --awb <柜号/AWB> --batch <客户批次号> --warehouse <仓库供应商>
 *     [--yes] [--allow-new-options] [--allow-duplicates] [--sheet <name>]
 *
 * SAFETY (production Base — see docs/60 Safety/Production Guardrails.md)
 *   - Default is a DRY RUN: parses, runs all guards, prints the proposed
 *     records, writes nothing.
 *   - --yes performs the live creation, then verifies by reading back.
 *   - Guards (each aborts unless explicitly overridden):
 *       * warehouse must be a known 仓库供应商 option (config.enums)
 *       * existing 3.1 records with the same AWB  -> abort (--allow-duplicates)
 *       * destinations not yet a 目的地路线 option -> abort (--allow-new-options,
 *         because creating a record with a new value ADDS that select option)
 *       * UPS-flagged rows are included in sums but reported loudly
 */

const path = require('path');
const lark = require('../lark');
const { loadPlan, aggregateByDestination } = require('./parse-plan');

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith('--')) { args[key] = next; i++; }
      else args[key] = true;
    } else args._.push(a);
  }
  return args;
}

function fail(msg) {
  console.error(`\n[ABORT] ${msg}`);
  process.exit(1);
}

async function main() {
  const args = parseArgs(process.argv);
  const { file, awb, batch, warehouse } = args;
  const live = args.yes === true;

  if (!file || !awb || !batch || !warehouse) {
    fail('Required: --file <xlsx> --awb <柜号/AWB> --batch <客户批次号> --warehouse <仓库供应商>');
  }

  const config = lark.config;
  const F = config.fields.inventory;

  // ---- Guard: warehouse is a known 仓库供应商 option ----------------------
  if (!config.enums.warehouseOptions.includes(warehouse)) {
    fail(`Warehouse "${warehouse}" is not in config.enums.warehouseOptions ` +
      `(${config.enums.warehouseOptions.join(', ')}). Add it to config.js only after ` +
      `confirming it exists as a 仓库供应商 select option in 3.1.`);
  }

  // ---- Parse + aggregate ---------------------------------------------------
  console.log(`Parsing: ${path.resolve(file)}`);
  const plan = loadPlan(file, { sheetName: args.sheet || null });
  console.log(`Sheet: "${plan.sheetName}" (header row ${plan.headerRow + 1}), data rows: ${plan.records.length}`);

  const { aggregates, totals, upsRows } = aggregateByDestination(plan.records);
  const dests = Object.keys(aggregates).sort();
  if (!dests.length) fail('No destination rows found in the plan.');

  if (upsRows.length) {
    console.log(`\n[NOTICE] ${upsRows.length} row(s) look like UPS/courier shipments ` +
      `(keyword or 1Z tracking code). They ARE included in the sums below — ` +
      `review whether they belong in 3.1 route summaries.`);
  }

  // ---- Read-only checks against live 3.1 ----------------------------------
  const inv = lark.base().tableByLabel('3.1');

  const existing = inv.findBy(F.awb, awb, 'contains');
  if (existing.length && !args['allow-duplicates']) {
    console.log(`\nExisting 3.1 records for AWB ${awb}:`);
    for (const r of existing) {
      const txt = (v) => (Array.isArray(v) ? v.map((x) => x.text ?? x).join('') : v);
      console.log(`  ${r.record_id} | ${txt(r.fields[F.destination])} | ${txt(r.fields[F.customerBatch])}`);
    }
    fail(`${existing.length} record(s) already exist for this AWB. Review them first ` +
      `(re-run with --allow-duplicates only if adding more routes is intended).`);
  }

  const fieldTypes = inv.getFieldTypes();
  if (!fieldTypes[F.destination]) fail(`Field "${F.destination}" not found in 3.1 — schema drift? Run npm run verify:config.`);
  const res = require('../lark/client').apiCmd(
    'GET',
    `/open-apis/bitable/v1/apps/${config.baseToken}/tables/${config.tables['3.1']}/fields`,
    { pageAll: true }
  );
  const destField = (res?.data?.items || []).find((f) => f.field_name === F.destination);
  const destOptions = new Set((destField?.property?.options || []).map((o) => o.name));
  const newOptions = dests.filter((d) => !destOptions.has(d));
  if (newOptions.length && !args['allow-new-options']) {
    fail(`These destinations are NOT existing 目的地路线 options and would be ` +
      `CREATED as new select options: ${newOptions.join(', ')}. ` +
      `Verify the spelling; re-run with --allow-new-options if intended.`);
  }

  // ---- Preview -------------------------------------------------------------
  console.log(`\nProposed records for 3.1 (${config.tables['3.1']}):`);
  console.log(`  ${F.awb} = ${awb} | ${F.customerBatch} = ${batch} | ${F.warehouse} = ${warehouse}`);
  for (const d of dests) {
    const a = aggregates[d];
    console.log(`  ${d.padEnd(8)} ${F.boxes}=${a.boxes}  ${F.weight}=${a.weight}  ${F.volume}=${a.volume}  (${a.rows} plan rows)`);
  }
  console.log(`  TOTAL    ${F.boxes}=${totals.boxes}  ${F.weight}=${totals.weight}  ${F.volume}=${totals.volume}  (${totals.rows} rows)`);

  if (!live) {
    console.log('\n[DRY RUN] No records created. Re-run with --yes after operator approval.');
    return;
  }

  // ---- Live creation ---------------------------------------------------------
  lark.setSafeMode(false);
  console.log('\n[LIVE] Creating records...');
  const created = [];
  for (const d of dests) {
    const a = aggregates[d];
    const fields = {
      [F.awb]: String(awb),
      [F.customerBatch]: String(batch),
      [F.warehouse]: warehouse,
      [F.destination]: d,
      [F.boxes]: a.boxes,
      [F.weight]: a.weight,
      [F.volume]: a.volume,
    };
    const r = inv.createRecord(fields);
    if (r && r.record_id) {
      console.log(`[CREATED] ${d} -> ${r.record_id}`);
      created.push({ dest: d, record_id: r.record_id });
    } else {
      console.error(`[FAILED] ${d} — stopping here; ${created.length} record(s) already created: ` +
        created.map((c) => c.record_id).join(', '));
      process.exit(1);
    }
  }

  // ---- Verify by reading back -------------------------------------------------
  const back = inv.findBy(F.awb, awb, 'contains');
  const check = { boxes: 0, weight: 0, volume: 0 };
  for (const r of back) {
    check.boxes += Number(r.fields[F.boxes]) || 0;
    check.weight += Number(r.fields[F.weight]) || 0;
    check.volume += Number(r.fields[F.volume]) || 0;
  }
  check.weight = Math.round(check.weight * 100) / 100;
  check.volume = Math.round(check.volume * 100) / 100;
  const ok = back.length >= created.length &&
    check.boxes === totals.boxes && check.weight === totals.weight && check.volume === totals.volume;
  console.log(`\nVerification: ${back.length} record(s) for AWB ${awb}; ` +
    `sums ${check.boxes}/${check.weight}/${check.volume} vs expected ${totals.boxes}/${totals.weight}/${totals.volume}`);
  console.log(ok ? '[VERIFIED] Live records match the plan totals.' :
    '[WARNING] Read-back does not match expected totals — inspect manually.');
}

main().catch((e) => fail(e.message));
