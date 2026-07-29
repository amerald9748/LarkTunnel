/**
 * LarkTable.js — Clean, well-documented wrapper around a single Bitable table.
 *
 * Read methods run freely. Write methods (create/update/delete) route through
 * the SAFE_MODE gate in client.js, so they are simulated no-ops unless writes
 * have been explicitly enabled. See ../lark/client.js and docs/60 Safety.
 */

const { baseCmd, apiCmd, withTempJson, execCli, gatedWrite } = require('./client');

class LarkTable {
  /**
   * @param {string} baseToken - the Bitable app token (obj_token).
   * @param {string} tableId   - the table id (tblXXXX).
   * @param {object} [meta]     - optional metadata, e.g. { label:'3.1' } for logs.
   */
  constructor(baseToken, tableId, meta = {}) {
    this.baseToken = baseToken;
    this.tableId = tableId;
    this.meta = meta;
  }

  get label() {
    return this.meta.label || this.tableId;
  }

  // -------------------------------------------------------------------------
  // Internal: turn the base +record-list matrix response into friendly records
  // -------------------------------------------------------------------------
  #formatRecord(recordId, fields, dataRow) {
    const parsed = {};
    for (let i = 0; i < fields.length; i++) {
      if (dataRow[i] !== null && dataRow[i] !== undefined) parsed[fields[i]] = dataRow[i];
    }
    return { record_id: recordId, fields: parsed };
  }

  #base(subcmd) {
    return baseCmd(subcmd, { baseToken: this.baseToken, tableId: this.tableId });
  }

  // =========================================================================
  // READ — schema / metadata
  // =========================================================================

  /** List field definitions (name/type/etc) via the base subcommand. */
  listFields() {
    return this.#base('+field-list')?.data || null;
  }

  /** Map of { fieldName: numericType } from the OpenAPI fields endpoint. */
  getFieldTypes() {
    const res = apiCmd(
      'GET',
      `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/fields`,
      { pageAll: true }
    );
    const typeMap = {};
    for (const item of res?.data?.items || []) typeMap[item.field_name] = item.type;
    return typeMap;
  }

  /** The list of field names present in the table (sampled from one record). */
  getFieldNames() {
    return this.#base('+record-list --limit 1')?.data?.fields || [];
  }

  // =========================================================================
  // READ — records
  // =========================================================================

  /**
   * List all records, auto-paginating. Optionally restrict to specific fields
   * (recommended for large tables to reduce payload).
   * @param {string[]} fieldNames
   * @returns {Array<{record_id:string, fields:object}>}
   */
  listRecords(fieldNames = []) {
    const out = [];
    let pageToken = '';
    let hasMore = true;
    const fieldFlags = fieldNames.map((f) => `--field-id "${f}"`).join(' ');

    while (hasMore) {
      const tokenFlag = pageToken ? `--page-token "${pageToken}"` : '';
      const res = this.#base(`+record-list ${fieldFlags} ${tokenFlag} --limit 200`);
      const raw = res?.data;
      if (!raw) break;
      if (raw.data && raw.fields && raw.record_id_list) {
        for (let i = 0; i < raw.record_id_list.length; i++) {
          out.push(this.#formatRecord(raw.record_id_list[i], raw.fields, raw.data[i]));
        }
      }
      hasMore = raw.has_more;
      pageToken = raw.page_token;
    }
    return out;
  }

  /** Fetch a single record by its record_id via the OpenAPI endpoint. */
  getRecord(recordId) {
    const res = apiCmd(
      'GET',
      `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records/${recordId}`
    );
    return res?.data?.record || null;
  }

  /**
   * Server-side filtered search. Accepts an array of conditions so callers can
   * build compound (AND/OR) queries — essential for the 3.1 unique-row lookup
   * (AWB + destination + warehouse).
   *
   * @param {Array<[fieldName, operator, value]>} conditions
   * @param {'and'|'or'} logic
   * @returns {Array<{record_id, fields}>}
   *
   * Operators (lark-cli / OpenAPI): is, isNot, contains, doesNotContain,
   * isEmpty, isNotEmpty, isGreater, isLess, ...
   * NOTE: for `isEmpty`/`isNotEmpty` pass value as [] or omit.
   */
  search(conditions, logic = 'and') {
    // lark-cli's +record-list --filter-json expects ARRAY-style conditions:
    //   { logic, conditions: [["字段名", "operator", "value"], ...] }
    const filterObj = {
      logic,
      conditions: conditions.map(([field, operator, value]) => {
        return value === undefined ? [field, operator] : [field, operator, String(value)];
      }),
    };
    return withTempJson(filterObj, (file) => {
      const res = this.#base(`+record-list --filter-json "@${file}" --limit 200`);
      const raw = res?.data;
      if (!raw) return [];
      if (raw.items) return raw.items; // some cli versions return items[]
      const out = [];
      if (raw.data && raw.fields && raw.record_id_list) {
        for (let i = 0; i < raw.record_id_list.length; i++) {
          out.push(this.#formatRecord(raw.record_id_list[i], raw.fields, raw.data[i]));
        }
      }
      return out;
    });
  }

  /** Convenience: exact-match search on a single field. */
  findBy(fieldName, value, operator = 'is') {
    return this.search([[fieldName, operator, value]]);
  }

  /**
   * Find exactly one record for a set of AND conditions.
   * @returns {{record_id, fields}|null} the single match.
   * @throws if more than one record matches (ambiguous — surface to human).
   */
  findUnique(conditions) {
    const matches = this.search(conditions, 'and');
    if (matches.length === 0) return null;
    if (matches.length > 1) {
      const err = new Error(
        `Expected unique row in ${this.label} but found ${matches.length} for conditions ` +
          JSON.stringify(conditions)
      );
      err.matches = matches;
      throw err;
    }
    return matches[0];
  }

  // =========================================================================
  // WRITE — gated by SAFE_MODE (see client.js)
  // =========================================================================

  /** Create a record. `fields` is a plain { fieldName: value } object. */
  createRecord(fields) {
    return gatedWrite(
      { op: 'create', table: this.label, tableId: this.tableId, fields },
      () => {
        const res = apiCmd(
          'POST',
          `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records`,
          { dataObj: { fields } }
        );
        return res?.data?.record || res?.data || null;
      }
    );
  }

  /** Update a record's fields. Only the provided fields are changed. */
  updateRecord(recordId, fields) {
    return gatedWrite(
      { op: 'update', table: this.label, tableId: this.tableId, recordId, fields },
      () => {
        const res = apiCmd(
          'PUT',
          `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records/${recordId}`,
          { dataObj: { fields } }
        );
        return res?.data?.record || res?.data || null;
      }
    );
  }

  /** Delete a single record. */
  deleteRecord(recordId) {
    return gatedWrite(
      { op: 'delete', table: this.label, tableId: this.tableId, recordId },
      () => {
        const res = apiCmd(
          'DELETE',
          `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records/${recordId}`
        );
        return res?.data || null;
      }
    );
  }

  // ---- batch writes -------------------------------------------------------

  batchCreate(recordsFields) {
    const records = recordsFields.map((fields) => ({ fields }));
    return gatedWrite(
      { op: 'batch_create', table: this.label, count: records.length, records },
      () =>
        apiCmd(
          'POST',
          `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records/batch_create`,
          { dataObj: { records } }
        )?.data || null
    );
  }

  batchUpdate(updates) {
    // updates: [{ record_id, fields }]
    return gatedWrite(
      { op: 'batch_update', table: this.label, count: updates.length, records: updates },
      () =>
        apiCmd(
          'POST',
          `/open-apis/bitable/v1/apps/${this.baseToken}/tables/${this.tableId}/records/batch_update`,
          { dataObj: { records: updates } }
        )?.data || null
    );
  }

  // =========================================================================
  // LINKED-RECORD (two-way link) helpers
  // =========================================================================

  /**
   * Read the linked record_ids stored in a link field of a record.
   * Link fields serialize as an array of ids, or an array of {record_ids:[...]}
   * depending on cli version — this normalizes to a flat string[].
   */
  static readLinkIds(fieldValue) {
    if (!fieldValue) return [];
    if (Array.isArray(fieldValue)) {
      return fieldValue
        .map((v) => (typeof v === 'string' ? v : v?.record_id || v?.id || null))
        .filter(Boolean);
    }
    if (fieldValue.link_record_ids) return fieldValue.link_record_ids;
    if (fieldValue.record_ids) return fieldValue.record_ids;
    return [];
  }

  /**
   * Set a two-way link field to point at the given target record_ids.
   * Writing to EITHER side of a two-way link updates both sides automatically.
   * @param {string} recordId  - the record whose link field we edit.
   * @param {string} linkFieldName - e.g. "5.2 BESTAR-CAL" or "库存信息-卡尔加里".
   * @param {string[]} targetRecordIds
   */
  setLink(recordId, linkFieldName, targetRecordIds) {
    return this.updateRecord(recordId, { [linkFieldName]: targetRecordIds });
  }

  /** Add target ids to an existing link field without dropping current links. */
  addLink(recordId, linkFieldName, addIds, currentValue) {
    const existing = LarkTable.readLinkIds(currentValue);
    const merged = Array.from(new Set([...existing, ...addIds]));
    return this.setLink(recordId, linkFieldName, merged);
  }
}

module.exports = { LarkTable };
