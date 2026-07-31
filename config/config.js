/**
 * config.js — SINGLE SOURCE OF TRUTH for this tool.
 * ============================================================================
 * Edit THIS file to point the tool at your tables, warehouses, and fields.
 * Everything the wrappers and the workflow runbook use is declared here so an
 * operator (or an agent) never has to hunt for a table id or field name.
 *
 * ⚠️  PRODUCTION BASE. Field names / table ids marked `VERIFY` below are
 *     best-guesses transcribed from the workflow spec and have NOT yet been
 *     confirmed against the live schema. Before any live run, confirm them
 *     read-only:  lark-cli base +field-list --base-token <t> --table-id <t> --as user
 * ============================================================================
 */

module.exports = {
  // ---------------------------------------------------------------------------
  // Connection
  // ---------------------------------------------------------------------------
  // The Bitable "app token" (obj_token) that contains ALL the workflow tables.
  // Reused from the previous project per user confirmation.
  baseToken: 'C13Zb8l6WassnesyRJhufdvLsFe',

  // Optional: if the base moves, a wiki node token/URL can be resolved instead.
  // LarkBase.resolveBaseTokenFromWiki(wikiToken)
  wikiToken: 'FFcNw6f15in36NklOy3laH9jgxb', // VERIFY: from old project, may be unrelated

  // lark-cli identity to act as.
  actAs: 'user',

  // ---------------------------------------------------------------------------
  // Table registry — friendly label -> table id
  // Reference tables by label in code: base.tableByLabel('3.1')
  // ---------------------------------------------------------------------------
  tables: {
    '3.1': 'tblbJiIHMwND2OGX', // 库存信息 / inventory-shipment master table
    '5.6': 'tblxiClBxvCku7rT', // ISA appointments (holds 复制时间列 timestamp)
    '5.2 BESTAR-CAL': 'tblILN4i0RIdpPhz', // delivery plans for BESTAR (Calgary)
    '5.3 WBLL-EDM': 'tblacP4Rr6L5AXco', // delivery plans for WBLL (Edmonton)
    '5.4 VAST-VAN-01': 'tblHIdhwKuqvdM7I', // delivery plans for VAST (Vancouver)
    '5.5 GFL-VAN-02': 'tbl2QdSLd53g987f', // delivery plans for GFL (Vancouver)
  },

  // ---------------------------------------------------------------------------
  // DEV environment (user-created copies for testing).
  // LARK_ENV=dev makes the webapp read/write THESE copies. A warehouse whose
  // plan table has NO devTables entry is trip-DISABLED in dev (pallet checks
  // still work) — dev mode NEVER writes into the shared/prod 5.x tables.
  //
  // To enable another warehouse in dev: duplicate its 5.x table in Lark, then
  // add the SAME plan-table key to devTables AND the three dev field maps
  // below. (Lark copies preserve the 5.x-side field names; the auto-created
  // back-columns on DEV 3.1 / DEV 5.6 get long generated names — run
  // scratchpad inspect or check the field list and copy them exactly.)
  // ---------------------------------------------------------------------------
  devTables: {
    '3.1': 'tblQcXC82tDveeSp', // DEV copy of 3.1 (155 fields, verified 2026-07-31)
    '5.6': 'tblIsKv8k3vvDs0B', // DEV copy of 5.6 (16 fields, verified 2026-07-31)
    // "5.2 BESTAR-CAL-P-01 副本" — full-dev trip table (user-created 2026-07-31)
    '5.2 BESTAR-CAL': 'tblJaXrkXhZr3R6N',
  },

  // prod: 5.x -> 3.1 inventory back-link (the "库存信息-XXX" columns).
  // Verified live 2026-07-31.
  prodTripLinkFields: {
    '5.2 BESTAR-CAL': '库存信息-卡尔加里',
    '5.3 WBLL-EDM': '库存信息-埃德蒙顿',
    '5.4 VAST-VAN-01': '库存信息-元浩',
    '5.5 GFL-VAN-02': '库存信息-GFL',
  },

  // ---- dev-pipeline field names (verified live 2026-07-31) -----------------
  // ON THE DEV 5.x COPY: link -> DEV 3.1 (same field name as on the prod
  // table, because Lark copies preserve field names).
  devTripLinkFields: {
    '5.2 BESTAR-CAL': '3.1 库存总表 副本-5.2 BESTAR-CAL',
  },
  // ON THE DEV 5.x COPY: link -> DEV 5.6 (prod equivalent is '预约信息').
  devTripIsaFields: {
    '5.2 BESTAR-CAL': '5.6 预约表 副本-5.2 出库计划 卡尔加里',
  },
  // ON DEV 3.1: the plan-link column pointing at the dev 5.x copy (prod
  // equivalent is the column named like the table label itself).
  devPlanLinkFields31: {
    '5.2 BESTAR-CAL': '5.2 BESTAR-CAL-P-01 副本-3.1 库存总表 副本-5.2 BESTAR-CAL',
  },
  // ON DEV 5.6: the back-link column pointing at the dev 5.x copy (prod
  // equivalent: '5.2 出库计划 卡尔加里' etc — see appointment_sync.LINK_ON_56).
  devLinkOn56: {
    '5.2 BESTAR-CAL': '5.2 BESTAR-CAL-P-01 副本-5.6 预约表 副本-5.2 出库计划 卡尔加里',
  },

  // ---------------------------------------------------------------------------
  // Field names — the exact column names used by the workflow.
  // Grouped by table. Change here if a column is renamed in Lark.
  // ---------------------------------------------------------------------------
  fields: {
    // 3.1 inventory/shipment master table
    inventory: {
      warehouse: '仓库供应商', // warehouse supplier
      awb: '柜号/AWB', // container / air waybill code
      destination: '目的地路线', // destination route
      actualPallets: '实际板数', // actual pallet count
      estimatedPallets: '预计板数', // estimated pallet count
      // Quantity fields on 3.1 (confirmed against live schema 2026-07-22).
      boxes: '箱数', // number — carton/piece count (Excel 件数 sums land here)
      weight: '重量', // number — kg
      volume: '体积', // number — m³
      customerBatch: '客户批次号', // text
      // Per-warehouse two-way link fields on 3.1 -> the delivery-plan tables.
      // (Same keys as `warehouses` below.) Confirmed against live schema.
      planLinkFields: {
        BESTAR: '5.2 BESTAR-CAL',
        WBLL: '5.3 WBLL-EDM',
        VAST: '5.4 VAST-VAN-01',
        GFL: '5.5 GFL-VAN-02',
        'CAL-5505': '5.2 BESTAR-CAL', // routes to BESTAR per user 2026-07-22
      },
      // Live-schema notes: 实际板数 is TEXT (type 1); 预计板数 / ISA / 派送计划
      // are FORMULA fields (type 20) — read-only, resolved via the link fields.
    },

    // 5.6 ISA appointment table (all names confirmed against live schema 2026-07-29)
    appointment: {
      isa: 'ISA', // Number field
      timestamp: '复制时间列', // Text; stored as 'YYYY/MM/DD HH:MM'
      destination: '目的地', // SingleSelect
      account: '预约账号', // SingleSelect
      // Two-way link fields on 5.6, ONE PER 5.x delivery-plan table (there is
      // NO single '配送计划' field on live 5.6). Keyed by plan-table label.
      planLinks: {
        '5.2 BESTAR-CAL': '5.2 出库计划 卡尔加里',
        '5.3 WBLL-EDM': '5.3 出库计划 埃德蒙顿',
        '5.4 VAST-VAN-01': '5.4 出库计划 温哥华',
        '5.5 GFL-VAN-02': '5.5 出库计划-GFL-预约信息',
      },
    },

    // 5.x delivery-plan tables (shared shape; back-link name differs per table)
    deliveryPlan: {
      // Two-way link back to the 5.6 appointment (single-value). Confirmed on
      // all four live 5.x tables 2026-07-29. Writing this one field is enough:
      // the 5.6 side (fields.appointment.planLinks) auto-fills, and the 5.x
      // ISA / 预约时间 formula columns resolve from the link.
      isaLink: '预约信息',
      // '总板数' does not exist on live 5.x tables. The rollup columns are
      // '出库板数' (5.2/5.3) / '出库板数-元浩' (5.4) / '出库板数-GFL' (5.5) —
      // read-only Lookup/Formula, and they roll up ONLY the prod link column,
      // so they are blind in dev mode. The webapp therefore COMPUTES trip
      // totals from the trip's linked 3.1 rows (实际板数, falling back to
      // 预计板数) — one code path that is correct in both environments.
      rollupPallets: {
        '5.2 BESTAR-CAL': '出库板数',
        '5.3 WBLL-EDM': '出库板数',
        '5.4 VAST-VAN-01': '出库板数-元浩',
        '5.5 GFL-VAN-02': '出库板数-GFL',
      },
      // Two-way back-link field pointing to 3.1 inventory rows. The label
      // differs per warehouse table (e.g. "库存信息-卡尔加里"). Declared per
      // warehouse in `warehouses[].inventoryBackLinkField`.
    },
  },

  // ---------------------------------------------------------------------------
  // Warehouse master map
  //   key           = value stored in 3.1 仓库供应商
  //   account       = 预约账号 used when creating an ISA appointment
  //   planTable     = table label (see `tables`) holding this warehouse's trips
  //   planLinkField = field on 3.1 that links to that plan table
  //   inventoryBackLinkField = field on the plan table linking back to 3.1
  //                            (the "库存信息-XXX" two-way link)
  // ---------------------------------------------------------------------------
  warehouses: {
    BESTAR: {
      account: 'BESTAR',
      planTable: '5.2 BESTAR-CAL',
      planLinkField: '5.2 BESTAR-CAL',
      inventoryBackLinkField: '库存信息-卡尔加里', // confirmed live 2026-07-31
      region: 'CAL',
    },
    WBLL: {
      account: 'WBLL',
      planTable: '5.3 WBLL-EDM',
      planLinkField: '5.3 WBLL-EDM',
      inventoryBackLinkField: '库存信息-埃德蒙顿', // confirmed live 2026-07-31
      region: 'EDM',
    },
    VAST: {
      account: 'VAST', // appointment account labelled 元浩 in the UI maps to VAST
      accountAlias: '元浩',
      planTable: '5.4 VAST-VAN-01',
      planLinkField: '5.4 VAST-VAN-01',
      inventoryBackLinkField: '库存信息-元浩', // confirmed live 2026-07-29
      region: 'VAN',
    },
    GFL: {
      account: 'GFL',
      planTable: '5.5 GFL-VAN-02',
      planLinkField: '5.5 GFL-VAN-02',
      inventoryBackLinkField: '库存信息-GFL', // confirmed live 2026-07-29
      region: 'VAN',
    },
    // CAL-5505 routes through BESTAR (confirmed by user 2026-07-22).
    'CAL-5505': {
      account: 'BESTAR',
      planTable: '5.2 BESTAR-CAL',
      planLinkField: '5.2 BESTAR-CAL',
      inventoryBackLinkField: '库存信息-卡尔加里', // confirmed live 2026-07-31
      region: 'CAL',
    },

    // ---- Warehouses listed as options but NOT yet mapped to an appointment
    //      account or a delivery-plan table. Flagged so the workflow refuses
    //      to guess. Ask the user how these route before enabling. ----
    'TOR-1140': { unmapped: true, note: 'No appointment account / plan table provided. CONFIRM with user.' },
  },

  // ---------------------------------------------------------------------------
  // Enumerations (for input validation)
  // ---------------------------------------------------------------------------
  enums: {
    // Live 仓库供应商 select option names (subset relevant to this workflow).
    warehouseOptions: ['TOR-1140', 'CAL-5505', 'BESTAR', 'WBLL', 'VAST', 'GFL'],
    destinationOptions: [
      // YEG (Edmonton), YYC (Calgary), YVR (Vancouver) — bays 1..9
      ...Array.from({ length: 9 }, (_, i) => `YEG${i + 1}`),
      ...Array.from({ length: 9 }, (_, i) => `YYC${i + 1}`),
      ...Array.from({ length: 9 }, (_, i) => `YVR${i + 1}`),
    ],
  },

  // ---------------------------------------------------------------------------
  // Workflow tunables
  // ---------------------------------------------------------------------------
  thresholds: {
    palletDiffWarning: 2, // |provided - estimated| > this => warn (node runbook)
    // Webapp upload guard: |file pallets - 预计板数| > this => BLOCK the row
    // (no 5.6 create / no trip / no 实际板数 write). Mirrored as
    // PALLET_DIFF_BLOCK in webapp/upload_56.py.
    palletDiffBlock: 3,
    tripPalletCap: 28, // trip total pallets > this => warn (overflow)
  },
};
