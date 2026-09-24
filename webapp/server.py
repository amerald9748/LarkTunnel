# -*- coding: utf-8 -*-
"""
server.py — tiny stdlib HTTP server for the LarkTunnel query UI.

Run:
    python webapp/server.py            # binds http://127.0.0.1:8787
    LARK_PORT=9000 python webapp/server.py

Serves the static frontend and a small read-only JSON API that proxies to
Feishu via lark_client.py. Binds to 127.0.0.1 ONLY (it can read production
credentials server-side, so it must not be exposed on the network).
"""

import os
import sys
import json
import time
import base64
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import apppaths
import app_settings
import access_control
import ops_log
import lark_client as lark
import file_parse
import appointment_sync
import appointment_create
import inventory_import
import verify_assignments
import audit_store
import audit_view
import identity_harvest
import sync_jobs

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB of request body

# Labels for the six identifiers the uploader extracts.
PARSE_FIELD_LABELS = {
    "awb": "柜号 / AWB",
    "batch": "客户批次号",
    "warehouse": "仓库供应商",
    "route": "目的地路线",
    "appointment": "预约时间",
    "isa": "ISA",
}


def _live_options(table_id, field_name):
    """Live select-option names for a field, used to validate parsed values."""
    try:
        meta = lark.field_meta(table_id)["by_name"].get(field_name) or {}
        return list((meta.get("options") or {}).values())
    except Exception:
        return []

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("LARK_PORT", "8787"))
VERSION = "2.0.0"

# Browser heartbeat (GET /api/ping every ~20 s from app.js). The desktop shell
# uses it to notice that the last window went away when it cannot observe the
# window itself (Edge --app fallback / plain browser).
_LAST_PING = [0.0]


def last_ping():
    return _LAST_PING[0]


# POST routes that must work while the tool is LOCKED (no credentials yet,
# or this person's 授权 was switched off) — ONLY what ⚙ 设置 needs to enter
# credentials / a 授权码. Everything else (including every /api/access/*
# admin action) goes through the gate below.
_OPEN_POSTS = {"/api/settings", "/api/settings/credentials",
               "/api/settings/credentials/clear", "/api/settings/test"}

# Route -> feature; a person's 角色 grants a set of features
# (access_control.ROLE_PERMS). Unlisted routes need only a valid 授权.
_FEATURE_OF = {
    "/api/import/plan": "import", "/api/import/commit": "import",
    "/api/create56/plan": "create", "/api/create56/commit": "create",
    "/api/sync/plan": "sync", "/api/sync/commit": "sync",
    "/api/verify": "verify",
    "/api/audit/search": "audit", "/api/audit/delete": "audit",
    "/api/audit/harvest": "audit", "/api/audit/resolve": "audit",
    "/api/audit/status": "audit",
    "/api/query": "query", "/api/views": "query",
    "/api/parse": "parse",
    "/api/access/create_table": "admin", "/api/access/issue": "admin",
    "/api/access/set_status": "admin", "/api/access/set_role": "admin",
    "/api/access/upgrade": "admin", "/api/access/members": "admin",
}


_CRED_ROUTES = {"/api/settings/credentials", "/api/settings/credentials/clear",
                "/api/settings/test"}


def _cred_routes_allowed():
    """Members never touch credentials (the build carries them). Allowed when
    NO credentials exist anywhere (first setup on a source checkout), or in
    single mode, or for an admin."""
    if not app_settings.credential_status()["configured"]:
        return True
    acc = access_control.status()
    return acc.get("mode") == "single" or "admin" in (acc.get("perms") or [])


def _gate(path):
    """None when allowed, else the JSON error body to send."""
    acc = access_control.status()
    if not acc.get("ok"):
        return {"ok": False, "locked": True, "error": acc.get("reason") or "未授权"}
    feat = _FEATURE_OF.get(path)
    if feat and feat not in (acc.get("perms") or []):
        return {"ok": False, "forbidden": True,
                "error": f"当前授权角色「{acc.get('role') or '成员'}」不包含此功能 — 请联系管理员"}
    return None


def _operator():
    """Who is at the keyboard — 授权表 name in team mode, else the local
    operator_name setting. For logs only."""
    acc = access_control.status()
    return acc.get("name") or app_settings.get_settings().get("operator_name") or ""


def _settings_public():
    """settings.json minus anything worth protecting (the 授权码 is shown as
    a hint only; re-enter to change)."""
    st = app_settings.get_settings()
    key = st.get("access_key") or ""
    out = {k: v for k, v in st.items() if k != "access_key"}
    out["access_key_set"] = bool(key)
    out["access_key_hint"] = (key[:5] + "…" + key[-3:]) if len(key) > 10 else ("已设置" if key else "")
    return out

# Field names we allow the UI to query by (label -> {field, default operator})
QUERY_FIELDS = {
    "awb": {"field": "柜号/AWB", "default_op": "contains", "label": "柜号/AWB"},
    "isa": {"field": "ISA", "default_op": "is", "label": "ISA"},
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LarkTunnel/1.0"

    # ---- helpers ----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    # ---- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if route == "/" or route == "":
                return self._send_file(os.path.join(STATIC, "index.html"))
            if route == "/api/health":
                return self._send_json({"ok": True, "version": VERSION, "env": lark.env()})
            if route == "/api/ping":
                _LAST_PING[0] = time.time()
                return self._send_json({"ok": True})
            if route == "/api/settings":
                # ⚙ 设置 bootstrap — never includes the App Secret itself
                return self._send_json({
                    "ok": True, "settings": _settings_public(),
                    "credentials": app_settings.credential_status(),
                    "access": access_control.status(),
                    "env": lark.env(), "port": PORT, "version": VERSION,
                    "frozen": apppaths.is_frozen(), "data_dir": apppaths.data_dir(),
                    "ops_log": ops_log.path(),
                    "base": lark.config_values().get("base_token"),
                    "auth_table": access_control._table(),
                    "auth_table_source": access_control.table_source(),
                    "dev_available": bool(lark.config_values().get("dev_tables"))})
            if route == "/api/access/status":
                force = (qs.get("force") or ["0"])[0] == "1"
                return self._send_json({"ok": True, "access": access_control.status(force=force)})
            if route in ("/api/access/members", "/api/audit/status", "/api/views"):
                denied = _gate(route)          # role-gated reads
                if denied:
                    return self._send_json(denied, 200)
            if route == "/api/access/members":
                return self._send_json({"ok": True, "members": access_control.list_members(),
                                        "table": access_control._table(),
                                        "role_field": access_control._role_field_present(
                                            access_control._table())})
            if route == "/api/ops/log":
                n = int((qs.get("n") or ["50"])[0])
                return self._send_json({"ok": True, "entries": ops_log.tail(max(1, min(n, 500)))})
            if route == "/api/tables":
                cfg = lark.config_values()
                # In dev mode the 3.1/5.6 labels resolve to the dev copies —
                # surface the RESOLVED ids so the search tab queries the same
                # environment the sync tab writes.
                tables = [{"label": lbl, "id": lark.table_id(lbl)}
                          for lbl in cfg["tables"]]
                return self._send_json({"ok": True, "tables": tables,
                                        "query_fields": QUERY_FIELDS,
                                        "tz": lark.tz_label(), "env": lark.env()})
            if route == "/api/sync/job":
                # progress poll — in-process only, NO Feishu I/O
                jid = (qs.get("id") or [None])[0]
                if jid == "latest":
                    job = sync_jobs.latest()
                else:
                    job = sync_jobs.get(jid) if jid else None
                if job is None:
                    return self._send_json({"ok": False, "error": "unknown job"}, 200)
                return self._send_json({"ok": True, "job": job})
            if route == "/api/sync/meta":
                # UI bootstrap for the 预约同步 tab: warehouse options + env.
                # trip_enabled reflects the CURRENT env: in dev, only plan
                # tables with a dev copy can create/link trips.
                whs = []
                for key, w in appointment_sync.WAREHOUSES.items():
                    wiring = appointment_sync._env_wiring(w["plan_table"])
                    whs.append({"key": key, "account": w["account"],
                                "plan_table": w["plan_table"],
                                "mapped": bool(w["plan_table"]),
                                "trip_enabled": wiring["enabled"],
                                "label": "卡尔加里 (CAL-5505)" if key == "CAL-5505" else key})
                # LIVE 5.6 目的地 options (5-min cached in field_meta) — the UI
                # validates destinations against THIS list, never a hardcoded
                # pattern, so options added in Lark work immediately.
                try:
                    fm = lark.field_meta(lark.table_id("5.6"))["by_name"]
                    dests = sorted(((fm.get("目的地") or {}).get("options") or {})
                                   .values())
                except lark.LarkError:
                    dests = []          # meta still loads; client falls back
                return self._send_json({
                    "ok": True, "env": lark.env(), "warehouses": whs,
                    "dest_options": dests,
                    "thresholds": {"pallet_diff": appointment_sync.PALLET_DIFF_WARN,
                                   "trip_cap": appointment_sync.TRIP_PALLET_CAP}})
            if route == "/api/audit/status":
                # 🗑️ 删除日志 status bar — local SQLite only, no Feishu I/O
                st = audit_store.status()
                labels = audit_view._table_labels()
                for row in st.get("per_table", []):
                    row["label"] = labels.get(row.get("table_id"), row.get("table_id"))
                return self._send_json({"ok": True, "status": st,
                                        "env": lark.env(), "tz": lark.tz_label()})
            if route == "/api/views":
                table_id = (qs.get("table") or [None])[0]
                if not table_id:
                    return self._send_json({"ok": False, "error": "missing ?table=<table_id>"}, 400)
                return self._send_json({"ok": True, "views": lark.list_views(table_id)})
            if route.startswith("/static/"):
                rel = route[len("/static/"):]
                safe = os.path.normpath(os.path.join(STATIC, rel))
                if not safe.startswith(STATIC):
                    return self.send_error(403, "Forbidden")
                return self._send_file(safe)
            return self.send_error(404, "Not found")
        except lark.LarkError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)

    # ---- upload + parse ---------------------------------------------------
    def _handle_parse(self, payload):
        filename = (payload.get("filename") or "uploaded").strip()
        b64 = payload.get("content_b64") or ""
        if not b64:
            return self._send_json({"ok": False, "error": "没有收到文件内容"}, 200)
        try:
            data = base64.b64decode(b64)
        except Exception as e:
            return self._send_json({"ok": False, "error": f"文件解码失败: {e}"}, 200)

        try:
            sheets = file_parse.read_file(filename, data)
        except ValueError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:
            return self._send_json({"ok": False, "error": f"无法解析该文件: {e}"}, 200)

        # Validate warehouse / route against the LIVE options on table 3.1.
        cfg = lark.config_values()
        table_id = payload.get("table") or cfg["tables"].get("3.1")
        wh_opts = _live_options(table_id, "仓库供应商")
        rt_opts = _live_options(table_id, "目的地路线")

        try:
            result = file_parse.extract(sheets, wh_opts, rt_opts)
        except Exception as e:
            return self._send_json({"ok": False, "error": f"字段识别失败: {e}"}, 200)

        return self._send_json({
            "ok": True,
            "filename": filename,
            "size": len(data),
            "sheets": [{"name": s["name"], "rows": len(s.get("rows") or [])} for s in sheets],
            "headers": result["headers"],
            "labels": PARSE_FIELD_LABELS,
            "fields": result["fields"],
            "details": result.get("details", []),
            "warehouse_options": wh_opts,
        })

    # ---- 预约同步 — job-based (both endpoints return a job id at once) -----
    # plan/commit are chains of dozens of Feishu calls (10-90 s on real
    # batches); running them inside the request made the button look hung.
    # The browser polls /api/sync/job for stage / N-of-M / current-row.
    def _handle_sync_plan(self, payload):
        warehouse = payload.get("warehouse") or ""
        text = payload.get("text") or ""

        def run(progress):
            return appointment_sync.plan(warehouse, text, progress=progress)
        try:
            job = sync_jobs.start("plan", run)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    def _handle_sync_commit(self, payload):
        warehouse = payload.get("warehouse") or ""
        text = payload.get("text") or ""
        approvals = payload.get("approvals") or []
        client_env = payload.get("env") or ""

        # env mismatch must fail the SUBMISSION, not the background job
        if client_env != lark.env():
            return self._send_json(
                {"ok": False, "error": f"环境不匹配：页面为 {client_env}，"
                                       f"服务端为 {lark.env()} — 请刷新页面"}, 200)

        # FAST PATH: the operator just reviewed a finished 预检 job — hand its
        # result to commit(), which re-reads only the records that plan
        # observed instead of re-running the whole search-heavy plan.
        # (appointment_sync decides whether it is still reusable.)
        cached = None
        pj = sync_jobs.get(payload.get("plan_job_id") or "")
        if pj and pj.get("kind") == "plan" and pj.get("state") == "done":
            cached = pj.get("result")

        def run(progress):
            return appointment_sync.commit(warehouse, text, approvals,
                                           client_env, progress=progress,
                                           cached_plan=cached)
        operator = _operator()

        def log(job_id, res, elapsed):
            ops_log.record("sync", operator, lark.env(), warehouse, job_id, elapsed, res)
        try:
            job = sync_jobs.start("commit", run, on_done=log)
        except sync_jobs.Busy as e:
            # single-flight: refuse loudly instead of queueing invisibly
            return self._send_json({"ok": False, "busy": True, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    # ---- 新建预约（仅 5.6）— same job pattern as the sync flow -------------
    def _handle_create56_plan(self, payload):
        warehouse = payload.get("warehouse") or ""
        text = payload.get("text") or ""

        def run(progress):
            return appointment_create.plan(warehouse, text, progress=progress)
        try:
            job = sync_jobs.start("plan", run)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    def _handle_create56_commit(self, payload):
        warehouse = payload.get("warehouse") or ""
        text = payload.get("text") or ""
        approvals = payload.get("approvals") or []
        client_env = payload.get("env") or ""
        if client_env != lark.env():
            return self._send_json(
                {"ok": False, "error": f"环境不匹配：页面为 {client_env}，"
                                       f"服务端为 {lark.env()} — 请刷新页面"}, 200)

        def run(progress):
            return appointment_create.commit(warehouse, text, approvals,
                                             client_env, progress=progress)
        operator = _operator()

        def log(job_id, res, elapsed):
            ops_log.record("create56", operator, lark.env(), warehouse, job_id, elapsed, res)
        try:
            # kind 'commit' => single-flight ACROSS both write flows
            job = sync_jobs.start("commit", run, on_done=log)
        except sync_jobs.Busy as e:
            return self._send_json({"ok": False, "busy": True, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    # ---- 库存导入（收货派送计划 → 3.1 新建记录）— same job pattern ----------
    def _handle_import_plan(self, payload):
        def run(progress):
            return inventory_import.plan(payload, progress=progress)
        try:
            job = sync_jobs.start("plan", run)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    def _handle_import_commit(self, payload):
        approvals = payload.get("approvals") or []
        client_env = payload.get("env") or ""
        if client_env != lark.env():
            return self._send_json(
                {"ok": False, "error": f"环境不匹配：页面为 {client_env}，"
                                       f"服务端为 {lark.env()} — 请刷新页面"}, 200)

        def run(progress):
            return inventory_import.commit(payload, approvals, client_env,
                                           progress=progress)
        operator = _operator()

        def log(job_id, res, elapsed):
            ops_log.record("import", operator, lark.env(),
                           payload.get("warehouse") or "", job_id, elapsed, res)
        try:
            # kind 'commit' => single-flight across ALL webapp write flows
            job = sync_jobs.start("commit", run, on_done=log)
        except sync_jobs.Busy as e:
            return self._send_json({"ok": False, "busy": True, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    # ---- 🗑️ 删除日志 search（local SQLite; field-name/operator enrichment
    #      reads Feishu metadata but never writes anything） -------------------
    def _handle_audit_search(self, payload):
        rows = audit_store.search(
            q=payload.get("q"),
            action=payload.get("action"),
            table_id=payload.get("table_id") or None,
            ts_from=payload.get("from") or None,
            ts_to=payload.get("to") or None,
            record_id=payload.get("record_id") or None,
            limit=payload.get("limit") or 200,
        )
        try:
            rows = audit_view.enrich(rows)
        except Exception as e:  # enrichment is best-effort — raw rows still ship
            for r in rows:
                r.setdefault("table_label", r.get("table_id"))
                r.setdefault("operator_name", r.get("operator_open_id"))
                r.setdefault("before", [])
                r.setdefault("after", [])
                r.pop("raw", None)
            return self._send_json({"ok": True, "rows": rows,
                                    "enrich_error": str(e), "tz": lark.tz_label()})
        return self._send_json({"ok": True, "rows": rows, "tz": lark.tz_label()})

    # ---- 🗑️ operator-identity harvest（read-only Lark sweeps → local json）--
    def _handle_audit_harvest(self, payload):
        def run(progress):
            return identity_harvest.harvest(progress=progress)
        try:
            job = sync_jobs.start("plan", run)   # 'plan' kind = read-only
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    # ---- 🗑️ audit-log pruning — deletes LOCAL log rows only, never Lark ----
    @staticmethod
    def _audit_filters(cond):
        """Condition JSON -> audit_store filter kwargs. Dates accept epoch or
        'YYYY-MM-DD[ HH:MM]' strings (interpreted in the display timezone)."""
        import datetime

        def ts_of(v, end=False):
            if v in (None, ""):
                return None
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                        "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    dt = datetime.datetime.strptime(s, fmt)
                    if fmt in ("%Y-%m-%d", "%Y/%m/%d") and end:
                        dt = dt.replace(hour=23, minute=59, second=59)
                    return int(dt.replace(tzinfo=lark.TZ).timestamp())
                except ValueError:
                    continue
            raise ValueError(f"无法解析时间「{s}」— 用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")

        return {
            "q": cond.get("q") or None,
            "action": cond.get("action") or None,
            "table_id": cond.get("table_id") or cond.get("table") or None,
            "record_id": cond.get("record_id") or None,
            "ts_from": ts_of(cond.get("from") or cond.get("after")),
            "ts_to": ts_of(cond.get("to") or cond.get("before"), end=True),
        }

    def _handle_audit_delete(self, payload):
        try:
            if payload.get("where") is not None:
                # condition-based bulk mode (power user): dry_run counts only
                filters = self._audit_filters(payload["where"] or {})
                if payload.get("dry_run"):
                    n = audit_store.count_where(**filters)
                    return self._send_json({"ok": True, "dry_run": True, "count": n})
                n = audit_store.delete_where(**filters)
                return self._send_json({"ok": True, "deleted": n,
                                        "status": audit_store.status()})
            ids = payload.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return self._send_json({"ok": False, "error": "没有选中要删除的日志"}, 200)
            n = audit_store.delete_events(ids)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"删除失败: {e}"}, 200)
        return self._send_json({"ok": True, "deleted": n,
                                "status": audit_store.status()})

    # ---- 🗑️ ad-hoc id resolver（opt/fld/tbl/ou → readable text; read-only）--
    def _handle_audit_resolve(self, payload):
        try:
            res = audit_view.resolve_token(payload.get("token") or "")
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"解析失败: {e}"}, 200)
        return self._send_json({"ok": True, "result": res})

    # ---- ③核对 (READ-ONLY audit of the finished assignments) ---------------
    def _handle_verify(self, payload):
        warehouse = payload.get("warehouse") or ""
        text = payload.get("text") or ""

        def run(progress):
            return verify_assignments.verify(warehouse, text, progress=progress)
        try:
            job = sync_jobs.start("plan", run)   # 'plan' kind = read-only
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "job": job})

    # ---- ⚙ 设置 · credentials / access control -----------------------------
    def _handle_settings(self, payload):
        """Patch non-secret settings. env/port take effect on the next start
        of the desktop app (this process keeps its env — see lark.ENV)."""
        patch = {}
        acc = access_control.status()
        admin = acc.get("mode") == "single" or "admin" in (acc.get("perms") or [])
        # 成员 may only change what is theirs: display name + own 授权码
        allowed = ("operator_name", "auth_table", "env", "port", "window") if admin \
            else ("operator_name", "window")
        for k in allowed:
            if k in payload:
                patch[k] = payload[k]
        if not admin and any(k in payload for k in ("auth_table", "env", "port")):
            return self._send_json({"ok": False, "forbidden": True,
                                    "error": "仅管理员可修改 授权表 / 环境 / 端口"}, 200)
        if "access_key" in payload and (payload["access_key"] or "").strip():
            patch["access_key"] = payload["access_key"].strip()   # blank = keep
        if payload.get("clear_access_key"):
            patch["access_key"] = ""
        if "env" in patch and patch["env"] not in ("prod", "dev"):
            return self._send_json({"ok": False, "error": "env 只能是 prod 或 dev"}, 200)
        if "port" in patch:
            try:
                patch["port"] = int(patch["port"])
                assert 1024 <= patch["port"] <= 65535
            except (ValueError, AssertionError):
                return self._send_json({"ok": False, "error": "端口应为 1024-65535 的整数"}, 200)
        st = app_settings.update_settings(patch)
        if "access_key" in patch or "auth_table" in patch:
            access_control.invalidate()
        return self._send_json({"ok": True, "settings": _settings_public(),
                                "access": access_control.status(force=True),
                                "restart_needed": any(k in patch for k in ("env", "port"))
                                and (st.get("env") != lark.env() or st.get("port") != PORT)})

    def _handle_credentials(self, payload):
        app_id = (payload.get("app_id") or "").strip()
        secret = (payload.get("app_secret") or "").strip()
        # verify BEFORE storing: a typo must not replace working credentials
        test = app_settings.test_connection(app_id, secret)
        if not test.get("ok"):
            return self._send_json({"ok": False, "error": test.get("error"), "test": test}, 200)
        try:
            app_settings.save_credentials(app_id, secret)
        except ValueError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        access_control.invalidate()
        return self._send_json({"ok": True, "test": test,
                                "credentials": app_settings.credential_status()})

    def _handle_credentials_clear(self, _payload):
        app_settings.clear_credentials()
        access_control.invalidate()
        return self._send_json({"ok": True, "credentials": app_settings.credential_status()})

    def _handle_settings_test(self, payload):
        test = app_settings.test_connection(payload.get("app_id"), payload.get("app_secret"))
        return self._send_json({"ok": True, "test": test})

    def _handle_access_create_table(self, payload):
        # Adds a table to the LIVE Base — the UI asks for explicit confirmation
        # and sends confirm=true; refuse anything else.
        if payload.get("confirm") is not True:
            return self._send_json({"ok": False, "error": "需要确认后才会在 Base 中创建授权表"}, 200)
        if app_settings.get_settings().get("auth_table"):
            return self._send_json({"ok": False, "error": "已配置授权表 — 如需重建，请先清空授权表 ID"}, 200)
        tid = access_control.create_auth_table((payload.get("name") or "").strip()
                                               or "LarkTunnel 授权表")
        return self._send_json({"ok": True, "table": tid, "settings": _settings_public()})

    def _handle_access_issue(self, payload):
        out = access_control.issue_key(payload.get("name") or "", payload.get("note") or "",
                                       payload.get("expiry") or "",
                                       payload.get("role") or access_control.MEMBER)
        return self._send_json({"ok": True, **out})

    def _handle_access_set_role(self, payload):
        rid = payload.get("record_id")
        if not rid:
            return self._send_json({"ok": False, "error": "缺少 record_id"}, 200)
        access_control.set_role(rid, payload.get("role") or "")
        return self._send_json({"ok": True, "members": access_control.list_members(),
                                "access": access_control.status(force=True)})

    def _handle_access_upgrade(self, payload):
        if payload.get("confirm") is not True:
            return self._send_json({"ok": False, "error": "需要确认后才会修改授权表结构"}, 200)
        out = access_control.upgrade_table()
        return self._send_json({"ok": True, **out, "access": access_control.status(force=True),
                                "members": access_control.list_members()})

    def _handle_access_set_status(self, payload):
        rid = payload.get("record_id")
        if not rid:
            return self._send_json({"ok": False, "error": "缺少 record_id"}, 200)
        access_control.set_status(rid, bool(payload.get("active")))
        return self._send_json({"ok": True, "members": access_control.list_members()})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        # NOTE: /api/upload · /api/dryrun_56 · /api/commit_56 were RETIRED
        # 2026-08-04 — upload_56.py was a third writer of 5.6 + 出库计划,
        # overlapping ①新建预约 / ②计划同步. 文件解析 is parse-only now.
        if parsed.path not in ("/api/query", "/api/parse",
                               "/api/sync/plan", "/api/sync/commit",
                               "/api/create56/plan", "/api/create56/commit",
                               "/api/import/plan", "/api/import/commit",
                               "/api/audit/search", "/api/audit/delete",
                               "/api/audit/harvest", "/api/audit/resolve",
                               "/api/verify", "/api/access/create_table",
                               "/api/access/issue", "/api/access/set_status",
                               "/api/access/set_role", "/api/access/upgrade",
                               *_OPEN_POSTS):
            return self.send_error(404, "Not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_UPLOAD_BYTES:
                return self._send_json({"ok": False, "error": "文件过大（上限 ~20MB）"}, 200)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            return self._send_json({"ok": False, "error": f"bad request: {e}"}, 200)

        # ACCESS GATE — every Feishu-touching route is refused while this
        # person's 授权 is off (team mode) so a switched-off teammate cannot
        # read or write anything, not merely lose the UI; and routes outside
        # the person's 角色 are refused too (hidden tabs are just courtesy).
        # Settings routes stay open so they can enter a new key / credentials.
        if parsed.path not in _OPEN_POSTS:
            denied = _gate(parsed.path)
            if denied:
                return self._send_json(denied, 200)
        elif parsed.path in _CRED_ROUTES and not _cred_routes_allowed():
            return self._send_json({"ok": False, "forbidden": True,
                                    "error": "凭据由程序内置并由管理员管理 — 成员无需也不能修改"}, 200)
        try:
            if parsed.path == "/api/settings":
                return self._handle_settings(payload)
            if parsed.path == "/api/settings/credentials":
                return self._handle_credentials(payload)
            if parsed.path == "/api/settings/credentials/clear":
                return self._handle_credentials_clear(payload)
            if parsed.path == "/api/settings/test":
                return self._handle_settings_test(payload)
            if parsed.path == "/api/access/create_table":
                return self._handle_access_create_table(payload)
            if parsed.path == "/api/access/issue":
                return self._handle_access_issue(payload)
            if parsed.path == "/api/access/set_status":
                return self._handle_access_set_status(payload)
            if parsed.path == "/api/access/set_role":
                return self._handle_access_set_role(payload)
            if parsed.path == "/api/access/upgrade":
                return self._handle_access_upgrade(payload)
        except lark.LarkError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)

        if parsed.path == "/api/parse":
            return self._handle_parse(payload)
        if parsed.path == "/api/sync/plan":
            return self._handle_sync_plan(payload)
        if parsed.path == "/api/sync/commit":
            return self._handle_sync_commit(payload)
        if parsed.path == "/api/create56/plan":
            return self._handle_create56_plan(payload)
        if parsed.path == "/api/create56/commit":
            return self._handle_create56_commit(payload)
        if parsed.path == "/api/import/plan":
            return self._handle_import_plan(payload)
        if parsed.path == "/api/import/commit":
            return self._handle_import_commit(payload)
        if parsed.path == "/api/audit/search":
            return self._handle_audit_search(payload)
        if parsed.path == "/api/audit/delete":
            return self._handle_audit_delete(payload)
        if parsed.path == "/api/audit/harvest":
            return self._handle_audit_harvest(payload)
        if parsed.path == "/api/audit/resolve":
            return self._handle_audit_resolve(payload)
        if parsed.path == "/api/verify":
            return self._handle_verify(payload)

        table_id = payload.get("table")
        view_id = payload.get("view_id") or None
        field_key = payload.get("field_key", "awb")
        value = (payload.get("value") or "").strip()
        mode = payload.get("mode")  # 'contains' | 'is' (optional override)

        if not table_id:
            return self._send_json({"ok": False, "error": "请选择要查询的表"}, 200)
        if not value:
            return self._send_json({"ok": False, "error": "请输入柜号或 ISA"}, 200)
        fld = QUERY_FIELDS.get(field_key)
        if not fld:
            return self._send_json({"ok": False, "error": f"unknown field_key {field_key}"}, 200)
        operator = mode or fld["default_op"]

        try:
            # 1. search the whole table (Feishu cannot filter *inside* a view)
            items = lark.search_records(table_id, fld["field"], value, operator=operator)
            total_found = len(items)
            # 2. apply the chosen view's own filter locally
            diag, hidden = None, set()
            if view_id:
                items, diag = lark.filter_items_by_view(items, table_id, view_id)
                hidden = lark.view_hidden_field_names(table_id, view_id)
            rows = lark.format_rows(items, table_id, hidden_names=hidden)
        except lark.LarkError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)

        # distinct matched values of the queried field (e.g. surfaces CAJU5283296A/B)
        matched = []
        for r in rows:
            for f in r["fields"]:
                if f["name"] == fld["field"] and f["display"] not in matched:
                    matched.append(f["display"])
        return self._send_json({
            "ok": True,
            "count": len(rows),
            "total_found": total_found,
            "view_filter": diag,
            "matched": matched,
            "query": {"field": fld["field"], "operator": operator, "value": value,
                      "view_id": view_id},
            "tz": lark.tz_label(),
            "rows": rows,
        })


class _Server(ThreadingHTTPServer):
    # Refuse to co-bind a port another server already holds. On Windows the
    # default SO_REUSEADDR lets two servers share a port and split requests
    # unpredictably (old + new code both answering) — this makes that fail loud.
    allow_reuse_address = False


def _safe_console():
    """Windows consoles/log redirects default to the GBK codec: a stray
    non-GBK character in a status print (env values, table labels) would kill
    the server at startup. Replace instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def build_server(port=None):
    """Create (but do not run) the HTTP server on 127.0.0.1:<port>. Shared by
    the console entry point below and the desktop shell (desktop.py), which
    runs serve_forever() in a thread and shuts it down when the window
    closes. Raises OSError when the port is taken."""
    _safe_console()
    port = PORT if port is None else int(port)
    try:
        cfg = lark.config_values()
        print(f"[LarkTunnel] v{VERSION} env={lark.env().upper()}  base={cfg['base_token']}  "
              f"tables={list(cfg['tables'])}")
        if lark.env() == "dev":
            print(f"[LarkTunnel] DEV MODE — 3.1/5.6 resolve to {cfg['dev_tables']}")
    except lark.LarkError as e:
        print(f"[LarkTunnel] CONFIG ERROR: {e}")
    cs = app_settings.credential_status()
    if cs["configured"]:
        print(f"[LarkTunnel] credentials: {cs['app_id']} (source={cs['source']})")
    else:
        # Not fatal any more: the ⚙ 设置 page collects them at runtime.
        print("[LarkTunnel] NO CREDENTIALS YET - open the Settings tab in the app to enter App ID / Secret")
    print(f"[LarkTunnel] data dir: {apppaths.data_dir()}  access mode: {access_control.mode()}")
    # Pre-warm the cross-table id index (opt/fld resolver) in the background —
    # cold build is 1-2 min of metadata calls; warmed, 解析 answers instantly.
    if cs["configured"]:
        try:
            audit_view.warm_indexes_async()
        except Exception as e:  # noqa
            print(f"[LarkTunnel] index warm-up not started: {e}")
    return _Server(("127.0.0.1", port), Handler)


def main():
    try:
        httpd = build_server(PORT)
    except OSError as e:
        print(f"[LarkTunnel] CANNOT BIND :{PORT} — already in use ({e}). "
              f"Set LARK_PORT to a free port.")
        raise SystemExit(1)
    print(f"[LarkTunnel] serving on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[LarkTunnel] stopped.")


if __name__ == "__main__":
    main()
