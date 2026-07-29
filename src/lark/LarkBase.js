/**
 * LarkBase.js — Wrapper around a Bitable app (a "Base").
 *
 * Resolves wiki tokens to base tokens, lists tables, and hands out LarkTable
 * instances. Combine with the config table registry so callers refer to tables
 * by friendly label ("3.1", "5.6") instead of raw ids.
 */

const { execCli } = require('./client');
const { LarkTable } = require('./LarkTable');

class LarkBase {
  /**
   * @param {string} baseToken - Bitable app token (obj_token).
   * @param {object} [registry] - optional { label: tableId } map from config.
   */
  constructor(baseToken, registry = {}) {
    this.baseToken = baseToken;
    this.registry = registry;
  }

  /**
   * Resolve a Wiki node token/URL to the underlying Bitable obj_token.
   * @returns {string|null}
   */
  static resolveBaseTokenFromWiki(wikiToken) {
    const url = wikiToken.startsWith('http') ? wikiToken : `https://feishu.cn/wiki/${wikiToken}`;
    const res = execCli(
      `lark-cli wiki +node-get --node-token "${url}" --format json --as user`,
      { label: 'wiki node-get' }
    );
    return res?.data?.obj_token || null;
  }

  listTables() {
    const res = execCli(
      `lark-cli base +table-list --base-token ${this.baseToken} --format json --as user`,
      { label: 'table-list' }
    );
    return res?.data || null;
  }

  /** Get a LarkTable by raw table id. */
  table(tableId, meta = {}) {
    return new LarkTable(this.baseToken, tableId, meta);
  }

  /**
   * Get a LarkTable by its friendly label from the config registry
   * (e.g. base.tableByLabel('3.1')). Throws if the label is unknown.
   */
  tableByLabel(label) {
    const tableId = this.registry[label];
    if (!tableId) {
      throw new Error(
        `Unknown table label "${label}". Known labels: ${Object.keys(this.registry).join(', ') || '(none)'}`
      );
    }
    return new LarkTable(this.baseToken, tableId, { label });
  }
}

module.exports = { LarkBase };
