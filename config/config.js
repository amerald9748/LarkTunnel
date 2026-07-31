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

    // 5.6 ISA appointment table
    appointment: {
      isa: 'ISA', // VERIFY exact column name (ISA / ISA# / ISA号)
      timestamp: '复制时间列', // the copy-time column
      destination: '目的地', // VERIFY
      account: '预约账号', // appointment account
      // Link on 5.6 pointing to the correlated delivery trip (in a 5.x table).
      deliveryPlanLink: '配送计划', // VERIFY exact column name
    },

    // 5.x delivery-plan tables (shared shape; back-link name differs per table)
    deliveryPlan: {
      totalPallets: '总板数', // VERIFY — used for the >28 overflow warning
      isaLink: 'ISA', // VERIFY — link back to the 5.6 appointment
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
      inventoryBackLinkField: '库存信息-卡尔加里', // VERIFY (Calgary)
      region: 'CAL',
    },
    WBLL: {
      account: 'WBLL',
      planTable: '5.3 WBLL-EDM',
      planLinkField: '5.3 WBLL-EDM',
      inventoryBackLinkField: '库存信息-埃德蒙顿', // VERIFY (Edmonton)
      region: 'EDM',
    },
    VAST: {
      account: 'VAST', // appointment account labelled 元浩 in the UI maps to VAST
      accountAlias: '元浩',
      planTable: '5.4 VAST-VAN-01',
      planLinkField: '5.4 VAST-VAN-01',
      inventoryBackLinkField: '库存信息-温哥华', // VERIFY (Vancouver)
      region: 'VAN',
    },
    GFL: {
      account: 'GFL',
      planTable: '5.5 GFL-VAN-02',
      planLinkField: '5.5 GFL-VAN-02',
      inventoryBackLinkField: '库存信息-温哥华GFL', // VERIFY (Vancouver)
      region: 'VAN',
    },
    // CAL-5505 routes through BESTAR (confirmed by user 2026-07-22).
    'CAL-5505': {
      account: 'BESTAR',
      planTable: '5.2 BESTAR-CAL',
      planLinkField: '5.2 BESTAR-CAL',
      inventoryBackLinkField: '库存信息-卡尔加里', // VERIFY (Calgary)
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
    palletDiffWarning: 2, // |provided - estimated| > this => warn
    tripPalletCap: 28, // trip total pallets > this => warn (overflow)
  },
};
