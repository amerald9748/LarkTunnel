# -*- coding: utf-8 -*-
r"""
ops_log.py — local, append-only record of who executed what.
================================================================================

Bitable stamps every write with the APP as 创建人/修改人, so once several
teammates share the tool, Lark alone cannot say which person ran a batch.
This log fills that gap on each machine:

    logs/ops.jsonl (repo) · %APPDATA%\LarkTunnel\logs\ops.jsonl (desktop exe)
    one JSON object per committed job

    {"ts": 1758400000, "when": "2026-09-21 10:32", "operator": "张三",
     "flow": "sync", "env": "prod", "warehouse": "BESTAR",
     "summary": {"approved": 6, "done": 6, "verified": 6, "failed": 0,
                 "skipped": 0}, "job": "3fa1…", "elapsed": 41.2}

Read-only inspection: the ⚙ 设置 page shows the last entries. Never contains
credentials or full record payloads — just counts and identifiers.
"""
import io
import os
import json
import time
import datetime
import threading

import apppaths

_lock = threading.Lock()


def path():
    return apppaths.state_path("logs", "ops.jsonl")


def record(flow, operator, env, warehouse, job_id, elapsed, result):
    """Summarize a finished commit job. `result` is the job result payload of
    the respective flow (sync / create56 / import). Best-effort: never raises."""
    try:
        rows = (result or {}).get("rows") or []
        approved = [r for r in rows if r.get("approved")]
        summary = {
            "approved": len(approved),
            "done": sum(1 for r in approved if (r.get("commit") or {}).get("done")
                        and not (r.get("commit") or {}).get("skipped")),
            "verified": sum(1 for r in approved if (r.get("commit") or {}).get("verified") is True),
            "failed": sum(1 for r in approved if (r.get("commit") or {}).get("error")),
            "skipped": sum(1 for r in approved if (r.get("commit") or {}).get("skipped")),
            "warnings": len((result or {}).get("warnings_summary") or []),
        }
        # flows without a rows[] shape (import) expose their own counts
        if not rows and isinstance((result or {}).get("summary"), dict):
            summary = dict((result or {}).get("summary"))
        entry = {"ts": int(time.time()),
                 "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "operator": operator or "", "flow": flow, "env": env,
                 "warehouse": warehouse or "", "summary": summary,
                 "job": (job_id or "")[:8], "elapsed": round(elapsed or 0, 1)}
        with _lock, io.open(path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def tail(n=50):
    try:
        with io.open(path(), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()][::-1]
    except (OSError, ValueError):
        return []
