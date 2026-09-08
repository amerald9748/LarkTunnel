# -*- coding: utf-8 -*-
"""
watcher.py — Bitable deletion watcher（长连接事件监听 → 本地审计库）
================================================================================

Listens to `drive.file.bitable_record_changed_v1` over Feishu's WebSocket
long-connection (no public URL needed) and writes every record change into the
local SQLite audit store (webapp/audit_store.py). The webapp's 🗑️ 删除日志 tab
searches that store — deleted records stay findable by field-value clues
forever, independent of the Base's own 30-min-usable history UI.

WHAT IS LOGGED（operator-specified 2026-09-03: creations + deletions only）
  * record_deleted — every table in the Base（含 before_value 被删内容）
  * record_added   — every table in the Base（低频，体积可控）
  * record_edited  — NOT logged（此前短暂记录过 3.1 编辑；操作者决定不追踪，
                     编辑洪峰也是体积的主要来源）. To re-enable: add it to
                     TRACKED_ACTIONS below.

PREREQUISITES（一次性,见 docs/60 Safety/Deletion Tracking.md 激活清单）
  1. 开发者后台 → 事件与回调:
       - 订阅方式 = 长连接
       - 添加事件 `drive.file.bitable_record_changed_v1`
       - 权限: base:record:read（或 bitable:app / drive:drive）;
         可选 contact:user.employee_id:readonly（事件里带 user_id）
  2. 订阅 Base 文件（本脚本代办）:  python watcher.py --subscribe
  3. 常驻运行:                      python watcher.py   （或 run-watcher.bat）

USAGE
  python watcher.py               # run the listener (blocking)
  python watcher.py --subscribe   # one-time: subscribe the Base file to events
  python watcher.py --status      # check file subscription + store stats

DEPENDENCY: lark-oapi (pip install -r requirements.txt) — deliberately isolated
from the zero-dependency webapp; only this watcher needs it.

⚠ COEXISTENCE CAVEAT: Feishu load-balances long-connection deliveries across a
single app's connected clients. If `lark-cli event consume` (same app
credentials) runs concurrently, it may receive — and drop — events this watcher
never sees. Don't run both long-term; verify before trusting overlap.
"""

import os
import sys
import json
import time
import argparse
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))            # LarkTunnel/
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import audit_store                       # noqa: E402  (webapp/, stdlib only)
import lark_client                       # noqa: E402  (creds + config + _api)

EVENT_TYPE = "drive.file.bitable_record_changed_v1"

# Which action types are persisted (operator-specified: creations + deletions).
TRACKED_ACTIONS = {"record_added", "record_deleted"}


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------

def _to_plain(obj, depth=0):
    """lark-oapi model objects -> plain dicts (defensive, shape-agnostic)."""
    if depth > 14:
        return None
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v, depth + 1) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k.lstrip("_"): _to_plain(v, depth + 1)
                for k, v in vars(obj).items()}
    return str(obj)


def option_resolver(table_id):
    """opt-id -> option-name map for a table（single/multi-select 值在事件里是
    optXXX id — 解析成名字进搜索索引，路线/仓库等才能按名称搜到）.
    Network read via lark_client.field_meta (5-min cached); {} on any failure."""
    try:
        merged = {}
        for m in lark_client.field_meta(table_id)["by_id"].values():
            merged.update(m.get("options") or {})
        return merged
    except Exception:                                             # noqa
        return {}


_OPT_ID = __import__("re").compile(r"^opt[0-9A-Za-z]+$")


def handle_event(payload, tracked=TRACKED_ACTIONS, resolver=None):
    """payload: plain dict of the full event (header + event).
    tracked: action types to persist (others are skipped).
    resolver: optional table_id -> {opt_id: name} (network); None in tests."""
    header = payload.get("header") or {}
    event = payload.get("event") or payload
    event_id = header.get("event_id") or payload.get("event_id") or ""
    file_token = event.get("file_token") or ""
    table_id = event.get("table_id") or ""
    revision = event.get("revision") or 0
    ts = event.get("update_time") or header.get("create_time") or time.time()
    try:
        ts = int(str(ts)[:13])
        if ts > 10 ** 12:                      # ms -> s
            ts //= 1000
    except (TypeError, ValueError):
        ts = int(time.time())
    operator = event.get("operator_id") or {}

    optmap = resolver(table_id) if resolver else {}

    stored = skipped = 0
    for action in event.get("action_list") or []:
        act = (action or {}).get("action") or ""
        rec = (action or {}).get("record_id") or ""
        if act not in tracked:
            skipped += 1
            continue
        # Resolve select option ids -> names into the search index, so field
        # values like 目的地路线 stay searchable by their visible name.
        extras = []
        if optmap:
            for leaf in audit_store.flatten_leaves(action):
                if isinstance(leaf, str) and _OPT_ID.match(leaf) and leaf in optmap:
                    extras.append(optmap[leaf])
        new = audit_store.insert_action(
            event_id=event_id, ts=ts, file_token=file_token,
            table_id=table_id, action=act, record_id=rec,
            operator=operator if isinstance(operator, dict) else {},
            revision=revision, action_payload=action,
            envelope={"header": header,
                      "event_keys": sorted(k for k in event if k != "action_list")},
            extra_search=extras)
        stored += 1 if new else 0
    if stored:
        acts = [a.get("action") for a in event.get("action_list") or []]
        print(f"[watcher] {time.strftime('%H:%M:%S')} table={table_id} "
              f"stored={stored} actions={acts} operator={operator.get('open_id', '?')}",
              flush=True)
    audit_store.set_meta("last_event_at", int(time.time()))


# ---------------------------------------------------------------------------
# Long-connection client (lark-oapi)
# ---------------------------------------------------------------------------

def run_listener():
    import lark_oapi as lark

    app_id, app_secret = lark_client._load_creds()
    print(f"[watcher] tracked actions: {sorted(TRACKED_ACTIONS)} "
          f"(all tables; edits not logged)", flush=True)

    # Typed handler (lark-oapi ships P2DriveFileBitableRecordChangedV1):
    # data.header = {event_id, create_time, …}; data.event = {file_token,
    # table_id, revision, operator_id{open_id,…}, update_time,
    # action_list[{record_id, action, before_value[{field_id, field_value}]}]}.
    def on_record_changed(data) -> None:
        try:
            payload = _to_plain(data)
            if not isinstance(payload, dict):
                payload = json.loads(str(data))
            handle_event(payload, resolver=option_resolver)
        except Exception as e:                                    # noqa
            print(f"[watcher] ERROR handling event: {e}", flush=True)
            try:  # never lose an event: keep the raw text at least
                audit_store.insert_action(
                    event_id=f"err-{time.time()}", ts=time.time(),
                    file_token="", table_id="", action="parse_error",
                    record_id="", operator={}, revision=0,
                    action_payload={"raw": str(data)[:100000], "error": str(e)})
            except Exception:                                     # noqa
                pass

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_drive_file_bitable_record_changed_v1(on_record_changed)
               .build())

    # WS domain: this tenant's REST calls are accepted on open.feishu.cn, but
    # the long-connection gateway is domain-strict — the app actually lives on
    # international Lark (verified 2026-09-03: feishu ws -> "1000040351
    # Incorrect domain name"; larksuite ws -> connected, msg-frontier-sg).
    # Try LARK first, fall back to FEISHU; override with LARK_WS_DOMAIN.
    pref = os.environ.get("LARK_WS_DOMAIN", "").strip().lower()
    if pref == "feishu":
        domains = [("feishu", lark.FEISHU_DOMAIN)]
    elif pref == "lark":
        domains = [("lark", lark.LARK_DOMAIN)]
    else:
        domains = [("lark", lark.LARK_DOMAIN), ("feishu", lark.FEISHU_DOMAIN)]

    audit_store.set_meta("watcher_started", int(time.time()))

    def beat():
        warned = 0.0
        while True:
            audit_store.heartbeat()
            # Size guard: loud console warning (hourly) when the store exceeds
            # LARK_AUDIT_MAX_GB (default 5 GB). Never deletes anything itself.
            size = audit_store.db_bytes()
            if size > audit_store.MAX_DB_BYTES and time.time() - warned > 3600:
                warned = time.time()
                print(f"[watcher] ⚠⚠ AUDIT LOG SIZE {size / 1024**3:.2f} GB exceeds "
                      f"{audit_store.MAX_DB_BYTES / 1024**3:.1f} GB limit — archive or "
                      f"prune logs/audit.db (the webapp 🗑️ tab shows the same warning)",
                      flush=True)
                audit_store.set_meta("size_warned_at", int(warned))
            time.sleep(30)
    threading.Thread(target=beat, daemon=True).start()

    last_err = None
    for name, domain in domains:
        client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                                domain=domain, log_level=lark.LogLevel.INFO)
        print(f"[watcher] connecting long-connection for {EVENT_TYPE} "
              f"via {name} ({domain}) …", flush=True)
        try:
            audit_store.set_meta("ws_domain", name)
            client.start()                           # blocking; returns only on stop
            return
        except Exception as e:                       # noqa — try the other domain
            last_err = e
            print(f"[watcher] {name} connection failed: {e}", flush=True)
    raise SystemExit(f"[watcher] could not connect on any domain: {last_err}")


# ---------------------------------------------------------------------------
# One-time subscription helpers（写入类配置 — 仅经用户明确执行）
# ---------------------------------------------------------------------------

def subscribe():
    base = lark_client.config_values()["base_token"]
    print(f"[watcher] subscribing base {base} (file_type=bitable) …")
    data = lark_client._api(
        "POST", f"/open-apis/drive/v1/files/{base}/subscribe",
        query={"file_type": "bitable"})
    audit_store.set_meta("subscribed_file_token", base)
    print("[watcher] subscribe OK:", json.dumps(data, ensure_ascii=False))


def sub_status():
    base = lark_client.config_values()["base_token"]
    try:
        data = lark_client._api(
            "GET", f"/open-apis/drive/v1/files/{base}/get_subscribe",
            query={"file_type": "bitable"})
        print("[watcher] subscription:", json.dumps(data, ensure_ascii=False))
    except lark_client.LarkError as e:
        print(f"[watcher] get_subscribe failed: {e}")
    print("[watcher] store:", json.dumps(audit_store.status(), ensure_ascii=False,
                                         indent=2))


def main():
    ap = argparse.ArgumentParser(description="Bitable deletion watcher")
    ap.add_argument("--subscribe", action="store_true",
                    help="one-time: subscribe the Base file to drive events")
    ap.add_argument("--status", action="store_true",
                    help="check subscription + audit store stats")
    args = ap.parse_args()
    if args.subscribe:
        subscribe()
    elif args.status:
        sub_status()
    else:
        run_listener()


if __name__ == "__main__":
    main()
