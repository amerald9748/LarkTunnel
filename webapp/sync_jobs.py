# -*- coding: utf-8 -*-
"""
sync_jobs.py — background jobs + live progress for the 预约同步 flow.

WHY THIS EXISTS
    plan()/commit() are chains of dozens of sequential Feishu calls: on a
    real batch they take 10–90 s. Run inside the HTTP request they look like
    a hung button — and a second click just queues invisibly on WRITE_LOCK.
    So the endpoints now return a job id IMMEDIATELY; the work runs in a
    daemon thread that reports progress, and the browser polls
    GET /api/sync/job?id=… (~2 cheap in-process calls/second, no Feishu I/O).

CONTRACT
    start(kind, runner) -> job dict            kind: 'plan' | 'commit'
        runner(progress) does the work and returns the result payload.
        progress(stage=None, done=None, total=None, current=None) may be
        called from anywhere inside the runner (thread-safe).
    get(job_id)   -> public snapshot (result only included once finished)
    latest()      -> newest job snapshot (page-refresh re-attach)
    Single-flight: at most ONE running commit job per process — a second
    submission is REFUSED with a clear error instead of silently queueing
    (that invisible queueing was the "sometimes hangs" report).
"""

import threading
import time
import uuid

_LOCK = threading.Lock()
_JOBS = {}           # id -> job dict (internal, mutable)
_ORDER = []          # insertion order for latest() / retention
_KEEP = 25           # finished jobs retained for late polls / re-attach


class Busy(Exception):
    """Another commit job is already running."""


def _snapshot(job, with_result):
    out = {k: job[k] for k in ("id", "kind", "state", "stage", "done", "total",
                               "current", "started", "finished")}
    out["elapsed"] = round((job["finished"] or time.time()) - job["started"], 1)
    if job["state"] == "error":
        out["error"] = job["error"]
    if with_result and job["state"] == "done":
        out["result"] = job["result"]
    return out


def start(kind, runner):
    """Spawn `runner(progress)` in a daemon thread. Returns the job snapshot."""
    with _LOCK:
        if kind == "commit":
            for j in _JOBS.values():
                if j["kind"] == "commit" and j["state"] == "running":
                    raise Busy("另一个写入任务正在执行 — 请等待其完成后重试"
                               f"（任务 {j['id'][:8]}，已运行 "
                               f"{int(time.time() - j['started'])} 秒）")
        job = {"id": uuid.uuid4().hex, "kind": kind, "state": "running",
               "stage": "启动中", "done": 0, "total": 0, "current": "",
               "started": time.time(), "finished": None,
               "result": None, "error": None}
        _JOBS[job["id"]] = job
        _ORDER.append(job["id"])
        # retention: drop the oldest FINISHED jobs beyond _KEEP
        finished = [i for i in _ORDER if _JOBS[i]["state"] != "running"]
        for i in finished[:-_KEEP] if len(finished) > _KEEP else []:
            _JOBS.pop(i, None)
            _ORDER.remove(i)

    def progress(stage=None, done=None, total=None, current=None):
        # thread-safe, monotonic-enough updates; cheap on purpose
        with _LOCK:
            if stage is not None:
                job["stage"] = stage
            if done is not None:
                job["done"] = done
            if total is not None:
                job["total"] = total
            if current is not None:
                job["current"] = current

    def run():
        try:
            res = runner(progress)
            with _LOCK:
                job["result"] = res
                job["state"] = "done"
                job["stage"] = "完成"
        except Exception as e:  # noqa — surfaced to the poller, never lost
            with _LOCK:
                job["error"] = str(e)
                job["state"] = "error"
                job["stage"] = "失败"
        finally:
            with _LOCK:
                job["finished"] = time.time()

    threading.Thread(target=run, name=f"sync-{kind}-{job['id'][:8]}",
                     daemon=True).start()
    return _snapshot(job, with_result=False)


def get(job_id, with_result=True):
    with _LOCK:
        job = _JOBS.get(job_id)
        return _snapshot(job, with_result) if job else None


def latest():
    """Newest job (running preferred) — lets a refreshed page re-attach."""
    with _LOCK:
        running = [i for i in _ORDER if _JOBS[i]["state"] == "running"]
        target = running[-1] if running else (_ORDER[-1] if _ORDER else None)
        return _snapshot(_JOBS[target], with_result=False) if target else None
