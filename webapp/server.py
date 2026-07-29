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
import json
import base64
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lark_client as lark
import file_parse
import upload_56

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


# --- Upload target ---------------------------------------------------------
# For now every "上传" writes ONE text row into the sandbox test table
# '3.1 测试表'. This is the safest possible destination: it touches no
# production data table, creates no select options, and is trivially
# reversible. Re-point UPLOAD_TABLE once the real destination (5.6 / 3.1 /
# a structured staging table) is decided.
UPLOAD_TABLE = "tblmAEVldrBdd460"   # 3.1 测试表 (sandbox, single Text field)
UPLOAD_TABLE_NAME = "3.1 测试表"


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
                return self._send_json({"ok": True})
            if route == "/api/tables":
                cfg = lark.config_values()
                tables = [{"label": lbl, "id": tid} for lbl, tid in cfg["tables"].items()]
                return self._send_json({"ok": True, "tables": tables,
                                        "query_fields": QUERY_FIELDS, "tz": lark.tz_label()})
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

    # ---- 5.6 dry-run (READ-ONLY manifest) ---------------------------------
    def _handle_dryrun56(self, payload):
        records = payload.get("records") or []
        warehouse = payload.get("warehouse") or ""
        try:
            res = upload_56.plan(records, warehouse)
        except lark.LarkError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "dryrun": True, "target": "5.6 预约表", **res})

    # ---- 5.6 commit (WRITES the actionable creates) -----------------------
    def _handle_commit56(self, payload):
        records = payload.get("records") or []
        warehouse = payload.get("warehouse") or ""
        try:
            res = upload_56.commit(records, warehouse)
        except lark.LarkError as e:
            return self._send_json({"ok": False, "error": str(e)}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        return self._send_json({"ok": True, "committed": True, "target": "5.6 预约表", **res})

    # ---- upload ONE record to 5.6 (guarded single create) -----------------
    def _handle_upload(self, payload):
        rec = payload.get("record") or {}
        warehouse = payload.get("warehouse") or ""
        if not (rec.get("awb") or "").strip():
            return self._send_json({"ok": False, "error": "该记录没有柜号，无法上传"}, 200)
        try:
            res = upload_56.commit([rec], warehouse)
        except lark.LarkError as e:
            msg = str(e)
            hint = ("（App 可能没有该 Base 的编辑权限）"
                    if any(k in msg.lower() for k in ("permission", "forbidden")) else "")
            return self._send_json({"ok": False, "error": f"写入失败: {msg}{hint}"}, 200)
        except Exception as e:  # noqa
            return self._send_json({"ok": False, "error": f"server error: {e}"}, 200)
        it = (res.get("items") or [{}])[0]
        return self._send_json({"ok": True, "target": "5.6 预约表", "item": it})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api/query", "/api/parse", "/api/upload",
                               "/api/dryrun_56", "/api/commit_56"):
            return self.send_error(404, "Not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_UPLOAD_BYTES:
                return self._send_json({"ok": False, "error": "文件过大（上限 ~20MB）"}, 200)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            return self._send_json({"ok": False, "error": f"bad request: {e}"}, 200)

        if parsed.path == "/api/parse":
            return self._handle_parse(payload)
        if parsed.path == "/api/upload":
            return self._handle_upload(payload)
        if parsed.path == "/api/dryrun_56":
            return self._handle_dryrun56(payload)
        if parsed.path == "/api/commit_56":
            return self._handle_commit56(payload)

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


def main():
    # Fail fast with a clear message if config/secrets are unreadable.
    try:
        cfg = lark.config_values()
        print(f"[LarkTunnel] base={cfg['base_token']}  tables={list(cfg['tables'])}")
    except lark.LarkError as e:
        print(f"[LarkTunnel] CONFIG ERROR: {e}")
    try:
        httpd = _Server(("127.0.0.1", PORT), Handler)
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
