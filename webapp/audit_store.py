# -*- coding: utf-8 -*-
"""
audit_store.py — SQLite store for Bitable record-change audit events.
================================================================================

Written by tools/deletion-watcher/watcher.py (ingest) and read by the webapp's
🗑️ 删除日志 tab (search). Stdlib only (sqlite3, WAL mode) so the webapp keeps
its zero-dependency guarantee; the watcher imports this module by path.

DESIGN
------
* Each row = one action on one record (a single drive.file.bitable_record_changed_v1
  event can carry several actions — they are stored individually).
* `raw_json` is the VERBATIM action + envelope — the source of truth. Columns
  are convenience projections; anything the projection misses is still
  recoverable from raw_json.
* `search_text` = every leaf string/number found anywhere in the action,
  flattened and lowercased. Field-value clues (柜号, 路线, 批次号…) findable by
  substring even when the exact payload shape shifts — deleted records searchable
  FOREVER regardless of table state.
* Dedup: (event_id, record_id, action) unique — Feishu may redeliver events;
  redelivery must not double-log.
"""

import os
import json
import time
import sqlite3
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # LarkTunnel/
DB_PATH = os.environ.get("LARK_AUDIT_DB",
                         os.path.join(ROOT, "logs", "audit.db"))

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT,               -- Feishu event_id (envelope header)
    ts            INTEGER,            -- update_time, epoch seconds
    received_at   INTEGER,            -- when the watcher ingested it
    file_token    TEXT,
    table_id      TEXT,
    action        TEXT,               -- record_deleted / record_edited / record_added
    record_id     TEXT,
    operator_open_id  TEXT,
    operator_user_id  TEXT,
    revision      INTEGER,
    search_text   TEXT,               -- flattened lowercase leaves for LIKE search
    raw_json      TEXT,               -- verbatim action + envelope (source of truth)
    UNIQUE(event_id, record_id, action)
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action, table_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_record ON events(record_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init():
    with _lock, _connect() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Flattening — collect every leaf scalar under a payload node
# ---------------------------------------------------------------------------

def flatten_leaves(node, out=None, depth=0):
    """All leaf strings/numbers anywhere in `node` (dict/list/scalar).
    JSON-encoded string values are decoded one level (Bitable often serializes
    field values as JSON strings inside the event)."""
    if out is None:
        out = []
    if depth > 12 or node is None:
        return out
    if isinstance(node, dict):
        for v in node.values():
            flatten_leaves(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            flatten_leaves(v, out, depth + 1)
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        n = node
        out.append(str(int(n)) if isinstance(n, float) and n.is_integer() else str(n))
    elif isinstance(node, str):
        s = node.strip()
        if not s:
            return out
        # decode nested JSON payloads: arrays/objects AND bare JSON strings
        # (single-select values arrive as '"optXXX"' — quotes must not leak
        # into the search index or the opt-id -> name resolution)
        if len(s) < 20000 and ((s[:1] in "[{" and s[-1:] in "]}")
                               or (s[:1] == '"' and s[-1:] == '"')):
            try:
                return flatten_leaves(json.loads(s), out, depth + 1)
            except (ValueError, TypeError):
                pass
        out.append(s)
    return out


def build_search_text(*nodes):
    parts = []
    for n in nodes:
        parts.extend(flatten_leaves(n))
    # dedupe, keep order, cap runaway sizes
    seen, kept = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            kept.append(p)
    return " ␟ ".join(kept)[:200000].lower()


# ---------------------------------------------------------------------------
# Ingest (watcher)
# ---------------------------------------------------------------------------

def insert_action(event_id, ts, file_token, table_id, action, record_id,
                  operator, revision, action_payload, envelope=None,
                  extra_search=None):
    """Insert one action row. Returns True if new, False if duplicate.
    extra_search: additional terms (e.g. option NAMES resolved from opt-ids)
    appended to the search index."""
    operator = operator or {}
    row = {
        "event_id": event_id or "",
        "ts": int(ts or time.time()),
        "received_at": int(time.time()),
        "file_token": file_token or "",
        "table_id": table_id or "",
        "action": action or "",
        "record_id": record_id or "",
        "operator_open_id": operator.get("open_id") or "",
        "operator_user_id": operator.get("user_id") or "",
        "revision": int(revision or 0),
        "search_text": build_search_text(action_payload, operator, record_id,
                                         extra_search or []),
        "raw_json": json.dumps({"action": action_payload, "envelope": envelope},
                               ensure_ascii=False),
    }
    with _lock, _connect() as con:
        try:
            con.execute(
                "INSERT INTO events (event_id, ts, received_at, file_token, table_id,"
                " action, record_id, operator_open_id, operator_user_id, revision,"
                " search_text, raw_json) VALUES (:event_id, :ts, :received_at,"
                " :file_token, :table_id, :action, :record_id, :operator_open_id,"
                " :operator_user_id, :revision, :search_text, :raw_json)", row)
            return True
        except sqlite3.IntegrityError:
            return False          # redelivered event — already logged


def set_meta(key, value):
    with _lock, _connect() as con:
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))


def heartbeat():
    set_meta("watcher_heartbeat", int(time.time()))


# ---------------------------------------------------------------------------
# Query (webapp) — read-only
# ---------------------------------------------------------------------------

def _where(q=None, action=None, table_id=None, ts_from=None, ts_to=None,
           record_id=None):
    """Shared WHERE builder for search / count / conditional delete."""
    where, args = [], []
    if q:
        for term in str(q).lower().split():
            where.append("search_text LIKE ?")
            args.append(f"%{term}%")
    if action and action != "all":
        where.append("action = ?")
        args.append(action)
    if table_id:
        where.append("table_id = ?")
        args.append(table_id)
    if record_id:
        where.append("record_id = ?")
        args.append(record_id)
    if ts_from:
        where.append("ts >= ?")
        args.append(int(ts_from))
    if ts_to:
        where.append("ts <= ?")
        args.append(int(ts_to))
    return where, args


def search(q=None, action=None, table_id=None, ts_from=None, ts_to=None,
           record_id=None, limit=200):
    """LIKE search over flattened field values + filters. Newest first."""
    where, args = _where(q, action, table_id, ts_from, ts_to, record_id)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 1000)))
    with _connect() as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql, args)]
    for r in rows:
        try:
            r["raw"] = json.loads(r.pop("raw_json") or "{}")
        except ValueError:
            r["raw"] = {}
        r.pop("search_text", None)
    return rows


def count_where(**filters):
    """Row count for a condition set (dry-run preview of delete_where)."""
    where, args = _where(**filters)
    if not where:
        raise ValueError("空条件 — 拒绝统计/删除全部日志（至少给一个条件）")
    sql = "SELECT COUNT(*) FROM events WHERE " + " AND ".join(where)
    with _connect() as con:
        return con.execute(sql, args).fetchone()[0]


def delete_where(vacuum=True, **filters):
    """Conditional bulk delete (LOCAL store only — never touches Lark).
    Same filter keys as search(); refuses an empty condition set so a typo
    can't wipe the whole log. Returns the number of rows removed."""
    where, args = _where(**filters)
    if not where:
        raise ValueError("空条件 — 拒绝删除全部日志（至少给一个条件）")
    with _lock, _connect() as con:
        cur = con.execute("DELETE FROM events WHERE " + " AND ".join(where), args)
        n = cur.rowcount
    if vacuum and n:
        with _lock:
            con = sqlite3.connect(DB_PATH, timeout=30)
            try:
                con.isolation_level = None
                con.execute("VACUUM")
            finally:
                con.close()
    return n


# Size guard — warn (never auto-delete) when the audit DB outgrows this.
MAX_DB_BYTES = int(float(os.environ.get("LARK_AUDIT_MAX_GB", "5")) * 1024 ** 3)


def db_bytes():
    """Current on-disk size of the store (main db + WAL journal)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    return total


def delete_events(ids, vacuum=True):
    """Delete audit rows by id (LOCAL store only — never touches Lark).
    VACUUM afterwards so the freed space is actually returned to disk
    (a plain DELETE keeps the file size; the whole point of pruning is
    the 5 GB budget). Returns the number of rows removed."""
    ids = [int(i) for i in (ids or [])][:10000]
    if not ids:
        return 0
    with _lock, _connect() as con:
        marks = ",".join("?" * len(ids))
        cur = con.execute(f"DELETE FROM events WHERE id IN ({marks})", ids)
        n = cur.rowcount
    if vacuum and n:
        # VACUUM must run outside a transaction — dedicated autocommit conn.
        with _lock:
            con = sqlite3.connect(DB_PATH, timeout=30)
            try:
                con.isolation_level = None
                con.execute("VACUUM")
            finally:
                con.close()
    return n


def status():
    """Watcher liveness + store stats for the UI status bar."""
    with _connect() as con:
        con.row_factory = sqlite3.Row
        stats = con.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(action='record_deleted') AS deleted,"
            " MIN(ts) AS earliest, MAX(ts) AS latest FROM events").fetchone()
        meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}
        per_table = [dict(r) for r in con.execute(
            "SELECT table_id, action, COUNT(*) AS n FROM events"
            " GROUP BY table_id, action ORDER BY n DESC LIMIT 40")]
    hb = int(meta.get("watcher_heartbeat") or 0)
    size = db_bytes()
    return {
        "db_path": DB_PATH,
        "db_bytes": size,
        "db_max_bytes": MAX_DB_BYTES,
        "db_over_limit": size > MAX_DB_BYTES,
        "total": stats["total"] or 0,
        "deleted": stats["deleted"] or 0,
        "earliest": stats["earliest"],
        "latest": stats["latest"],
        "per_table": per_table,
        "watcher_heartbeat": hb,
        "watcher_alive": bool(hb) and (time.time() - hb) < 120,
        "watcher_started": meta.get("watcher_started"),
        "subscribed": meta.get("subscribed_file_token"),
    }


init()
