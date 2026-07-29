# -*- coding: utf-8 -*-
"""
lark_client.py — read-only Feishu Bitable client for the LarkTunnel web app.

Design notes
------------
* Stdlib only (urllib/json/threading) so it runs on the bare Python install —
  no `pip install` required.
* Reuses the project's SINGLE SOURCE OF TRUTH: it parses `config/config.js`
  for the base token and the table registry, so labels/ids never drift from
  the rest of LarkTunnel.
* Secrets (App ID / Secret) are read from `config/secrets.txt` SERVER-SIDE only
  and used to mint a tenant_access_token. The secret and the token are never
  returned to callers / the browser.
* READ ONLY. There are no write methods here on purpose.

Why view filtering happens locally
----------------------------------
Feishu's API cannot "search inside a view": passing `view_id` together with a
`filter` makes your filter REPLACE the view's own filter (verified against this
Base on both `/records/search` and `/records`). So we:
  1. search the whole table for the container / ISA (a handful of rows), then
  2. evaluate the view's own filter conditions against those rows locally.
Conditions we cannot interpret are reported back, never silently ignored.
"""

import os
import re
import json
import time
import threading
import datetime
import urllib.request
import urllib.error
import urllib.parse

# --- timezone for rendering datetime fields --------------------------------
# Western-Canada logistics data; Mountain time is the operationally relevant zone.
# Override with env LARK_TZ (an IANA name) if needed.
_TZ_NAME = os.environ.get("LARK_TZ", "America/Edmonton")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(_TZ_NAME)
except Exception:
    TZ = datetime.timezone(datetime.timedelta(hours=-6))
    _TZ_NAME = "UTC-6"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # LarkTunnel/
HOSTS = ["https://open.feishu.cn", "https://open.larksuite.com"]    # China first, then intl

DATE_TYPES = {5, 1001, 1002}  # datetime, created-time, modified-time


class LarkError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Config / secrets
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


_cfg_cache = {}


def config_values():
    """Return {'base_token':..., 'tables': {label: id}} parsed from config.js."""
    if _cfg_cache:
        return _cfg_cache
    cfg = _read(os.path.join(ROOT, "config", "config.js"))
    m = re.search(r"baseToken:\s*'([^']+)'", cfg)
    if not m:
        raise LarkError("Could not find baseToken in config/config.js")
    tables = {}
    tb = re.search(r"tables:\s*\{(.*?)\}", cfg, re.S)
    if tb:
        for label, tid in re.findall(r"'([^']+)'\s*:\s*'(tbl[^']+)'", tb.group(1)):
            tables[label] = tid
    _cfg_cache.update({"base_token": m.group(1), "tables": tables})
    return _cfg_cache


def _load_creds():
    raw = _read(os.path.join(ROOT, "config", "secrets.txt"))
    app_id = None
    m = re.search(r"\bcli_[A-Za-z0-9]+", raw)  # Feishu app ids look like cli_xxx
    if m:
        app_id = m.group(0)
    kv = {}
    for line in raw.splitlines():
        mm = re.match(r"\s*[\"']?([A-Za-z0-9_\- ]+?)[\"']?\s*[:=]\s*[\"']?([^\"',#]+)", line)
        if mm:
            kv[mm.group(1).strip().lower().replace(" ", "_")] = mm.group(2).strip()
    if not app_id:
        for k in ("app_id", "appid", "app_key", "id"):
            if k in kv:
                app_id = kv[k]
                break
    app_secret = None
    for k in ("app_secret", "appsecret", "secret", "app_secret_key"):
        if k in kv:
            app_secret = kv[k]
            break
    if not app_secret:
        cands = [c for c in re.findall(r"\b[A-Za-z0-9]{20,}\b", raw)
                 if c != app_id and not c.startswith("cli_")]
        if cands:
            app_secret = cands[0]
    if not app_id or not app_secret:
        raise LarkError("Could not parse App ID / Secret from config/secrets.txt")
    return app_id, app_secret


# ---------------------------------------------------------------------------
# HTTP + token cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_token = {"host": None, "value": None, "exp": 0}


def _http(method, url, headers=None, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    h = {"Content-Type": "application/json; charset=utf-8"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise LarkError(f"HTTP {e.code} from Feishu: {body[:300]}", code=e.code)
    except urllib.error.URLError as e:
        raise LarkError(f"Network error reaching Feishu: {getattr(e, 'reason', e)}")


def _get_token():
    with _lock:
        now = time.time()
        if _token["value"] and now < _token["exp"] - 60:
            return _token["host"], _token["value"]
        app_id, app_secret = _load_creds()
        last = None
        for host in HOSTS:
            try:
                r = _http("POST", f"{host}/open-apis/auth/v3/tenant_access_token/internal",
                          payload={"app_id": app_id, "app_secret": app_secret})
            except LarkError as e:
                last = str(e)
                continue
            if r.get("code") == 0:
                _token.update(host=host, value=r["tenant_access_token"],
                              exp=now + int(r.get("expire", 6000)))
                return host, _token["value"]
            last = f"code {r.get('code')}: {r.get('msg')}"
        raise LarkError(f"Auth failed (tenant_access_token). Last: {last}")


def _api(method, path, payload=None, query=None):
    host, tok = _get_token()
    url = f"{host}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    r = _http(method, url, headers={"Authorization": f"Bearer {tok}"}, payload=payload)
    if r.get("code") != 0:
        raise LarkError(f"Feishu API error code {r.get('code')}: {r.get('msg')}", code=r.get("code"))
    return r.get("data", {})


# ---------------------------------------------------------------------------
# Metadata caches (views, fields) — short TTL
# ---------------------------------------------------------------------------

_meta_cache = {}
_META_TTL = 300


def _cached(key, producer):
    now = time.time()
    hit = _meta_cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    val = producer()
    _meta_cache[key] = (now + _META_TTL, val)
    return val


def list_views(table_id):
    def produce():
        base = config_values()["base_token"]
        out, pt = [], None
        while True:
            q = {"page_size": 100}
            if pt:
                q["page_token"] = pt
            data = _api("GET", f"/open-apis/bitable/v1/apps/{base}/tables/{table_id}/views", query=q)
            for v in data.get("items", []):
                out.append({"view_id": v["view_id"], "view_name": v["view_name"],
                            "view_type": v.get("view_type")})
            if not data.get("has_more"):
                break
            pt = data.get("page_token")
        return out
    return _cached(f"views:{table_id}", produce)


def get_view(table_id, view_id):
    def produce():
        base = config_values()["base_token"]
        d = _api("GET", f"/open-apis/bitable/v1/apps/{base}/tables/{table_id}/views/{view_id}")
        return d.get("view", d)
    return _cached(f"view:{table_id}:{view_id}", produce)


def _extract_options(field):
    """Option id -> name. Plain selects expose property.options; formula fields
    whose result is a select nest them under property.type.ui_property.options."""
    prop = field.get("property") or {}
    opts = prop.get("options")
    if not opts:
        up = (prop.get("type") or {}).get("ui_property") or {}
        opts = up.get("options")
    out = {}
    for o in opts or []:
        if isinstance(o, dict) and o.get("id") is not None:
            out[str(o["id"])] = o.get("name", "")
    return out


def field_meta(table_id):
    """{'by_id': {field_id: meta}, 'by_name': {field_name: meta}} for ALL fields."""
    def produce():
        base = config_values()["base_token"]
        items, pt = [], None
        while True:
            q = {"page_size": 100}
            if pt:
                q["page_token"] = pt
            data = _api("GET", f"/open-apis/bitable/v1/apps/{base}/tables/{table_id}/fields", query=q)
            items += data.get("items", [])
            if not data.get("has_more"):
                break
            pt = data.get("page_token")
        by_id, by_name = {}, {}
        for f in items:
            m = {"field_id": f.get("field_id"), "field_name": f.get("field_name"),
                 "type": f.get("type"), "options": _extract_options(f)}
            by_id[m["field_id"]] = m
            by_name[m["field_name"]] = m
        return {"by_id": by_id, "by_name": by_name}
    return _cached(f"fieldmeta:{table_id}", produce)


def field_types(table_id):
    return {n: m["type"] for n, m in field_meta(table_id)["by_name"].items()}


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def _looks_like_ms(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return False
    return 1_000_000_000_000 <= n <= 2_000_000_000_000  # ~2001..2033 as ms epoch


def _timeish(name):
    return ("时间" in name) or ("日期" in name) or bool(re.search(r"time|date", name, re.I))


def _fmt_dt(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)


def display_value(name, value, ftype):
    """Render a raw Feishu field value to a compact human string."""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, list):
        parts = [display_value(name, x, ftype) for x in value]
        return ", ".join(p for p in parts if p != "")
    if isinstance(value, dict):
        if "link_record_ids" in value:
            return f"关联×{len(value['link_record_ids'])}"
        if "record_ids" in value:
            return f"关联×{len(value['record_ids'])}"
        for k in ("text", "name", "full_name", "en_name", "file_name"):
            if value.get(k):
                return str(value[k])
        if "value" in value:
            return display_value(name, value["value"], ftype)
        if "record_id" in value:
            return str(value.get("text") or value["record_id"])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "是" if value else ""
    if isinstance(value, (int, float)):
        if ftype in DATE_TYPES or (_timeish(name) and _looks_like_ms(value)):
            return _fmt_dt(value)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        if (ftype in DATE_TYPES or _timeish(name)) and _looks_like_ms(s):
            return _fmt_dt(s)
        return s
    return str(value)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_records(table_id, field_name, value, operator="contains", limit=500):
    """Search the whole table for rows where field_name <operator> value.
    (View scoping is applied afterwards, locally — see filter_items_by_view.)"""
    base = config_values()["base_token"]
    conditions = [{"field_name": field_name, "operator": operator, "value": [str(value)]}]
    items, pt = [], None
    while True:
        body = {"filter": {"conjunction": "and", "conditions": conditions},
                "automatic_fields": False}
        q = {"page_size": 200}
        if pt:
            q["page_token"] = pt
        data = _api("POST", f"/open-apis/bitable/v1/apps/{base}/tables/{table_id}/records/search",
                    payload=body, query=q)
        items.extend(data.get("items", []))
        if not data.get("has_more") or len(items) >= limit:
            break
        pt = data.get("page_token")
    return items[:limit]


# ---------------------------------------------------------------------------
# Local evaluation of a view's own filter
# ---------------------------------------------------------------------------

def _norm_values(raw):
    """Flatten a raw field value into a list of comparable strings."""
    if raw is None or raw == "" or raw == [] or raw == {}:
        return []
    if isinstance(raw, list):
        out = []
        for x in raw:
            out.extend(_norm_values(x))
        return out
    if isinstance(raw, dict):
        for k in ("text", "name", "full_name", "file_name"):
            if raw.get(k):
                return [str(raw[k])]
        if "value" in raw:
            return _norm_values(raw["value"])
        if "link_record_ids" in raw:
            return [str(i) for i in raw["link_record_ids"]]
        return [json.dumps(raw, ensure_ascii=False)]
    if isinstance(raw, bool):
        return ["true"] if raw else []
    return [str(raw)]


def _cond_targets(cond, options):
    """Condition target values, mapped from option ids to option names."""
    tv = cond.get("value")
    targets = []
    if isinstance(tv, str):
        try:
            parsed = json.loads(tv)
            targets = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            targets = [tv]
    elif isinstance(tv, list):
        targets = tv
    elif tv is not None:
        targets = [tv]
    return [options.get(str(t), str(t)) for t in targets]


def _cond_match(cond, fields, meta):
    """True / False, or None when the condition cannot be interpreted."""
    fdef = meta["by_id"].get(cond.get("field_id"))
    if not fdef:
        return None
    op = cond.get("operator")
    vals = _norm_values(fields.get(fdef["field_name"]))
    if op == "isEmpty":
        return len(vals) == 0
    if op == "isNotEmpty":
        return len(vals) > 0
    options = fdef.get("options") or {}
    targets = _cond_targets(cond, options)
    if not targets:
        return None
    if op in ("is", "isNot"):
        hit = any(v == targets[0] for v in vals)
        return hit if op == "is" else (not hit)
    if op in ("contains", "doesNotContain"):
        if options:                     # select-like: "is any of"
            hit = any(v in targets for v in vals)
        else:                           # text: substring
            low = [v.lower() for v in vals]
            hit = any(any(str(t).lower() in v for t in targets) for v in low)
        return hit if op == "contains" else (not hit)
    return None


def filter_items_by_view(items, table_id, view_id):
    """Apply a view's own filter conditions to already-fetched rows.
    Returns (kept_items, diagnostics)."""
    view = get_view(table_id, view_id)
    prop = view.get("property") or {}
    fi = prop.get("filter_info") or {}
    conds = fi.get("conditions") or []
    conj = (fi.get("conjunction") or "and").lower()
    meta = field_meta(table_id)

    diag = {"view_name": view.get("view_name"), "conditions": len(conds),
            "conjunction": conj, "unsupported": [], "excluded": 0}
    if not conds:
        return items, diag

    unsupported = set()
    kept = []
    for it in items:
        f = it.get("fields", {})
        results = []
        for c in conds:
            r = _cond_match(c, f, meta)
            if r is None:
                fd = meta["by_id"].get(c.get("field_id"))
                unsupported.add((fd or {}).get("field_name") or str(c.get("field_id")))
            results.append(r)
        known = [r for r in results if r is not None]
        ok = (any(known) if conj == "or" else all(known)) if known else True
        if ok:
            kept.append(it)
    diag["unsupported"] = sorted(unsupported)
    diag["excluded"] = len(items) - len(kept)
    return kept, diag


def view_hidden_field_names(table_id, view_id):
    view = get_view(table_id, view_id)
    hidden = (view.get("property") or {}).get("hidden_fields") or []
    by_id = field_meta(table_id)["by_id"]
    return {by_id[h]["field_name"] for h in hidden if h in by_id}


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def format_rows(items, table_id, hidden_names=None):
    """Raw search items -> display rows (non-empty fields only)."""
    hidden_names = hidden_names or set()
    tmap = field_types(table_id)
    rows = []
    for it in items:
        out_fields = []
        for name, raw in (it.get("fields") or {}).items():
            if name in hidden_names:
                continue
            disp = display_value(name, raw, tmap.get(name))
            if disp != "":
                out_fields.append({"name": name, "display": disp, "type": tmap.get(name)})
        rows.append({"record_id": it.get("record_id"), "fields": out_fields})
    return rows


def tz_label():
    return _TZ_NAME
