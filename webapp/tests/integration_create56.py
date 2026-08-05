# -*- coding: utf-8 -*-
"""
integration_create56.py — LIVE test of 新建预约 (5.6-only) against DEV 5.6,
using the user's two waves. The waves share ISA 7229372996 — the test proves
it is created exactly ONCE, and measures whether Bitable search latency could
ever let a duplicate through.

    python webapp/tests/integration_create56.py

Touches DEV 5.6 (tblIsKv8k3vvDs0B) only. Artifacts tracked in
.tmp/integration-create56.json + swept by exact test ISAs; the user's real
data is never touched. Records are left in place for inspection.
"""
import io
import json
import os
import sys
import time
import threading

os.environ["LARK_ENV"] = "dev"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lark_client as lark          # noqa: E402
import appointment_create as c56    # noqa: E402

DEV56 = "tblIsKv8k3vvDs0B"
STATE = os.path.join(lark.ROOT, ".tmp", "integration-create56.json")

WAVE1 = ("YEG1\t7227802996\t08/02/2026 07:00 MDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT \t\n"
         "YYC4\t129695028975\t08/02/2026 10:00 MDT")
WAVE2 = ("YVR4\t7404870996\t08/03/2026 13:00 PDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT \t\n"
         "YEG2\t147621024984\t 08/03/2026 09:00 MDT")
DUP_ISA = 7229372996
ALL_ISAS = [7227802996, 7229372996, 129695028975, 7404870996, 147621024984]

PASS, FAIL = 0, []
CREATED = []      # record ids we created (tracked for next-run cleanup)


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


def base():
    return lark.config_values()["base_token"]


def search_isa(isa, fields=("ISA",)):
    return lark._api(
        "POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV56}/records/search",
        payload={"filter": {"conjunction": "and", "conditions": [
            {"field_name": "ISA", "operator": "is", "value": [str(isa)]}]},
            "field_names": list(fields), "automatic_fields": False},
        query={"page_size": 50}).get("items", [])


def save_state():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(CREATED, io.open(STATE, "w", encoding="utf-8"))


def cleanup():
    rids = []
    if os.path.exists(STATE):
        try:
            rids = json.load(io.open(STATE, encoding="utf-8"))
        except Exception:
            rids = []
    for isa in ALL_ISAS:
        rids += [r["record_id"] for r in search_isa(isa)]
    rids = list(dict.fromkeys(rids))
    if rids:
        # tolerate ghosts: keep only ids that still exist
        d = lark._api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV56}"
                      "/records/batch_get",
                      payload={"record_ids": rids, "automatic_fields": False})
        alive = [r["record_id"] for r in d.get("records", [])]
        if alive:
            lark._api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV56}"
                      "/records/batch_delete", payload={"records": alive})
    CREATED.clear()
    save_state()
    c56._RECENT.clear()      # this process must start cold, like a fresh server
    print(f"[cleanup] removed {len(rids)} prior test appointment(s); "
          "recent-creates cache cleared")


def approve(planned):
    return [{"line_no": r["line_no"], "sig": r["sig"]}
            for r in planned["rows"] if r["action"] == "create"]


def commit_tracked(text):
    planned = c56.plan("BESTAR", text)
    res = c56.commit("BESTAR", text, approve(planned), "dev")
    for r in res["rows"]:
        rid = (r.get("commit") or {}).get("record_id")
        if rid:
            CREATED.append(rid)
    save_state()
    return res


def main():
    assert lark.env() == "dev" and lark.table_id("5.6") == DEV56
    print(f"[env] dev 5.6={DEV56}")
    cleanup()

    # ---- Wave 1 ------------------------------------------------------------
    print("\n== Wave 1: 3 new ISAs ==")
    t0 = time.time()
    p1 = c56.plan("BESTAR", WAVE1)
    t_plan1 = time.time() - t0
    check("wave1 plans 3 creates", p1["summary"]["create"] == 3, str(p1["summary"]))
    t0 = time.time()
    r1 = c56.commit("BESTAR", WAVE1, approve(p1), "dev")
    t_commit1 = time.time() - t0
    for r in r1["rows"]:
        rid = (r.get("commit") or {}).get("record_id")
        if rid:
            CREATED.append(rid)
    save_state()
    ok1 = [r for r in r1["rows"] if (r.get("commit") or {}).get("record_id")]
    check("wave1 created 3", len(ok1) == 3)
    check("wave1 all verified", all(r["commit"].get("verified") for r in ok1))
    lat = r1["latency"]
    print(f"  [perf] plan={t_plan1:.1f}s  commit={t_commit1:.1f}s  "
          f"搜索可见延迟: avg={lat['avg']}s max={lat['max']}s")

    # ---- Wave 2, IMMEDIATELY (the duplicated-ISA test) ----------------------
    print("\n== Wave 2 immediately: shares ISA 7229372996 ==")
    t0 = time.time()
    p2 = c56.plan("BESTAR", WAVE2)
    t_plan2 = time.time() - t0
    acts = [r["action"] for r in p2["rows"]]
    check("wave2 = create/exists/create", acts == ["create", "exists", "create"],
          str(acts))
    dup_row = p2["rows"][1]
    src = (dup_row.get("existing") or {}).get("source")
    check("duplicate ISA detected", dup_row["action"] == "exists")
    print(f"  [latency answer] 重复 ISA 由「{src}」层捕获"
          f"（search=索引已可见 / recent=本机缓存兜底）· wave2 预检 {t_plan2:.1f}s")
    r2 = commit_tracked(WAVE2)
    made2 = [r for r in r2["rows"] if (r.get("commit") or {}).get("record_id")]
    check("wave2 created exactly 2", len(made2) == 2, str(len(made2)))

    # the shared ISA exists exactly ONCE in the table (retry: index may lag)
    n = None
    for _ in range(10):
        n = len(search_isa(DUP_ISA))
        if n >= 1:
            break
        time.sleep(1)
    check("shared ISA exists exactly once", n == 1, f"count={n}")

    # ---- replay wave 2: nothing new -----------------------------------------
    p3 = c56.plan("BESTAR", WAVE2)
    check("replay wave2: zero creates", p3["summary"]["create"] == 0,
          str(p3["summary"]))

    # ---- concurrent double-submit stress (the strongest uniqueness test) ----
    print("\n== Concurrent double-submit of one NEW ISA ==")
    text = "YVR2\t7405010996\t08/06/2026 20:00 PDT"
    results = [None, None]

    def submit(i):
        p = c56.plan("BESTAR", text)
        results[i] = c56.commit("BESTAR", text, approve(p), "dev")
    th = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    made = [(r["rows"][0].get("commit") or {}).get("record_id")
            for r in results if r]
    made = [m for m in made if m]
    CREATED.extend(made)
    save_state()
    check("two racing submits -> ONE create", len(made) == 1, str(made))
    time.sleep(2)
    check("racing ISA exists exactly once", len(search_isa(7405010996)) == 1)

    print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {len(FAIL)} failed"
          + (f" -> {FAIL}" if FAIL else ""))
    print("\n—— 留存数据（DEV 5.6，供人工核对；下次运行前自动清理）——")
    print(f"  Wave1: 7227802996 / {DUP_ISA} / 129695028975（账号 BESTAR）")
    print(f"  Wave2: 7404870996 / 147621024984（{DUP_ISA} 未重复创建 ✓）")
    print(f"  并发竞速: 7405010996（仅 1 条）")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
