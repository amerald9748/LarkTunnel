# -*- coding: utf-8 -*-
"""
integration_workflow.py — LIVE end-to-end test of the operator's 3-step flow
against the DEV tables, using the real data from the session:

    ① 新建预约      create the missing 5.6 appointments
    ② 计划同步      wire 3.1 (实际板数 + 出库计划) using those appointments
    ③ 核对          prove the right appointment landed on the right 3.1 row

    python webapp/tests/integration_workflow.py

It also proves the de-overlap rules:
    · ② REFUSES a line whose ISA isn't in 5.6 (points back to ①)
    · ① never touches 3.1 / 出库计划
    · ③ writes nothing

Touches DEV 3.1 / DEV 5.6 / DEV 5.2 only. Artifacts are tracked in
.tmp/integration-workflow.json and swept on the next run; the user's other
real rows are never deleted.
"""
import io
import json
import os
import sys
import time

os.environ["LARK_ENV"] = "dev"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lark_client as lark            # noqa: E402
import appointment_create as c56      # noqa: E402
import appointment_sync as sync       # noqa: E402
import verify_assignments as va       # noqa: E402

DEV31, DEV56, DEV52 = "tblQcXC82tDveeSp", "tblIsKv8k3vvDs0B", "tblJaXrkXhZr3R6N"
STATE = os.path.join(lark.ROOT, ".tmp", "integration-workflow.json")
AWB = "ZCSU9034790B"          # the user's real container, already in DEV 3.1

# ① — the appointments (user's real paste, 目的地 ISA 时间)
APPTS = ("YVR4\t7404870996\t08/03/2026 13:00 PDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT \t\n"
         "YEG2\t147621024984\t 08/03/2026 09:00 MDT")
# ② — the update plan (user's real paste, 柜号 路线 板数 箱数 ISA 时间)
PLAN = (f"{AWB}\tYVR4\t1\t14\t7404870996\t08/03/2026 13:00 PDT\n"
        f"{AWB}\tYEG1\t1\t1\t7229372996\t08/05/2026 07:00 MDT \t\n"
        f"{AWB}\tYEG2\t3\t49\t147621024984\t 08/03/2026 09:00 MDT")
ISAS = [7404870996, 7229372996, 147621024984]
ROUTES = ["YVR4", "YEG1", "YEG2"]

PASS, FAIL = 0, []
CREATED = {"recs56": [], "trips52": []}
TOUCHED31 = []                # 3.1 rows we linked / filled (reset, not deleted)


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


def api(method, path, payload=None, query=None):
    return lark._api(method, path, payload=payload, query=query)


def search(tid, field, op, val, fields=None, page=100):
    body = {"filter": {"conjunction": "and", "conditions": [
        {"field_name": field, "operator": op, "value": [str(val)]}]},
        "automatic_fields": False}
    if fields:
        body["field_names"] = fields
    return api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/search",
               payload=body, query={"page_size": page}).get("items", [])


def save_state():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"created": CREATED, "touched31": TOUCHED31},
              io.open(STATE, "w", encoding="utf-8"))


def alive(tid, rids):
    if not rids:
        return []
    d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/batch_get",
            payload={"record_ids": list(dict.fromkeys(rids)), "automatic_fields": False})
    return [r["record_id"] for r in d.get("records", [])]


def cleanup():
    """Delete ONLY our tracked artifacts, and RESET (never delete) the real
    3.1 rows we touched, so the test is repeatable from a known state."""
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(io.open(STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    trips = list((prev.get("created") or {}).get("trips52") or [])
    recs = list((prev.get("created") or {}).get("recs56") or [])
    link56 = "5.2 BESTAR-CAL-P-01 副本-5.6 预约表 副本-5.2 出库计划 卡尔加里"
    for isa in ISAS:
        for r in search(DEV56, "ISA", "is", isa, [link56]):
            recs.append(r["record_id"])
            trips += lark.link_ids((r.get("fields") or {}).get(link56))
    for tid, ids, label in ((DEV52, trips, "出库计划"), (DEV56, recs, "预约")):
        keep = alive(tid, ids)
        if keep:
            api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}"
                "/records/batch_delete", payload={"records": keep})
        print(f"[cleanup] 删除 {len(keep)} 条{label}（仅测试产生的）")

    # reset the real 3.1 rows: clear 实际板数 + the dev plan link
    plan_link = sync._env_wiring("5.2 BESTAR-CAL")["plan_link_31"]
    rows = [r for r in search(DEV31, "柜号/AWB", "is", AWB, ["目的地路线"])
            if (r["fields"].get("目的地路线") in ROUTES)]
    if rows:
        api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV31}"
            "/records/batch_update",
            payload={"records": [{"record_id": r["record_id"],
                                  "fields": {"实际板数": "", plan_link: []}}
                                 for r in rows]})
        print(f"[cleanup] 复位 {len(rows)} 行真实 3.1（清空实际板数/关联，不删行）")
    for k in CREATED:
        CREATED[k] = []
    TOUCHED31.clear()
    c56._RECENT.clear()
    save_state()


def approve56(planned):
    return [{"line_no": r["line_no"], "sig": r["sig"]}
            for r in planned["rows"] if r["action"] == "create"]


def approve_sync(planned):
    return [{"line_no": r["line_no"], "sig": r["sig"]}
            for r in planned["rows"] if r["actions"] and not r["blockers"]]


def step2_blocked_before_step1():
    print("\n== 前置：② 在预约不存在时必须拦截（不再自动新建） ==")
    r = sync.plan("BESTAR", PLAN)
    for row in r["rows"]:
        st = (row.get("plan") or {}).get("status")
        check(f"line{row['line_no']} 拦截 isa_missing", st == "isa_missing", str(st))
        check(f"line{row['line_no']} 指向 ①新建预约",
              any("①新建预约" in b for b in row["blockers"]), str(row["blockers"]))
        check(f"line{row['line_no']} 无 create_isa 动作",
              "create_isa" not in [a["type"] for a in row["actions"]])
    check("整批 0 可执行", r["summary"]["actionable"] == 0, str(r["summary"]))


def count52():
    """How many 出库计划 records exist in DEV 5.2 right now."""
    items, pt = [], None
    while True:
        q = {"page_size": 200}
        if pt:
            q["page_token"] = pt
        d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV52}"
                "/records/search", payload={"automatic_fields": False}, query=q)
        items += d.get("items", [])
        if not d.get("has_more"):
            break
        pt = d.get("page_token")
    return len(items)


def step1_create_appointments():
    print("\n== ① 新建预约（仅写 5.6） ==")
    before31, before52 = snapshot31(), count52()
    p = c56.plan("BESTAR", APPTS)
    check("① 计划新建 3 条", p["summary"]["create"] == 3, str(p["summary"]))
    res = c56.commit("BESTAR", APPTS, approve56(p), "dev")
    made = [r for r in res["rows"] if (r.get("commit") or {}).get("record_id")]
    for r in made:
        CREATED["recs56"].append(r["commit"]["record_id"])
    save_state()
    check("① 创建并核实 3 条", len(made) == 3
          and all(r["commit"].get("verified") for r in made), str(len(made)))
    lat = res.get("latency") or {}
    print(f"  [latency] 搜索可见 avg={lat.get('avg')}s max={lat.get('max')}s")
    # ownership: ① must not have touched 3.1 or 出库计划
    check("① 未改动 3.1", snapshot31() == before31)
    after52 = count52()
    check("① 未创建出库计划", after52 == before52, f"{before52} → {after52}")
    # replay: nothing new
    p2 = c56.plan("BESTAR", APPTS)
    check("① 重跑 0 新建（幂等）", p2["summary"]["create"] == 0
          and p2["summary"]["exists"] == 3, str(p2["summary"]))


def snapshot31():
    """(实际板数, plan-link count) per relevant 3.1 row — to prove ① and ③
    change nothing."""
    plan_link = sync._env_wiring("5.2 BESTAR-CAL")["plan_link_31"]
    rows = search(DEV31, "柜号/AWB", "is", AWB,
                  ["目的地路线", "实际板数", plan_link])
    return {r["record_id"]: (lark.flat_text(r["fields"].get("实际板数")),
                             len(lark.link_ids(r["fields"].get(plan_link))))
            for r in rows if r["fields"].get("目的地路线") in ROUTES}


def step2_sync_plans():
    print("\n== ② 计划同步（3.1 板数 + 出库计划关联，复用①的预约） ==")
    p = sync.plan("BESTAR", PLAN)
    for row in p["rows"]:
        st = row["plan"]["status"]
        check(f"line{row['line_no']} 复用已存在预约（create_trip）",
              st == "create_trip" and row["plan"].get("isa_record_id"), str(st))
        types = [a["type"] for a in row["actions"]]
        check(f"line{row['line_no']} 动作=填板数+建出库计划+挂靠",
              types == ["fill_pallets", "create_trip", "link_trip"], str(types))
    res = sync.commit("BESTAR", PLAN, approve_sync(p), "dev")
    ok = [r for r in res["rows"] if r.get("approved")]
    for r in ok:
        c = r.get("commit") or {}
        if c.get("trip_record_id"):
            CREATED["trips52"].append(c["trip_record_id"])
        if r.get("match"):
            TOUCHED31.append(r["match"]["record_id"])
    save_state()
    check("② 3 行全部写入并回读核实",
          len(ok) == 3 and all((r.get("commit") or {}).get("verified") for r in ok),
          str([(r["line_no"], (r.get("commit") or {}).get("error")) for r in ok]))
    # each line had its own ISA -> 3 separate 出库计划 records
    trips = {(r.get("commit") or {}).get("trip_record_id") for r in ok}
    check("3 个不同 ISA → 3 个出库计划", len(trips) == 3, str(trips))
    # 实际板数 landed
    snap = snapshot31()
    vals = sorted(v[0] for v in snap.values())
    check("3.1 实际板数 = 1/1/3", vals == ["1", "1", "3"], str(vals))
    check("3.1 每行都挂上了出库计划",
          all(v[1] == 1 for v in snap.values()), str(snap))
    # replay: nothing left to do
    p2 = sync.plan("BESTAR", PLAN)
    states = [row["plan"]["status"] for row in p2["rows"]]
    check("② 重跑：全部 has_plan_match", states == ["has_plan_match"] * 3, str(states))
    check("② 重跑 0 可执行", p2["summary"]["actionable"] == 0, str(p2["summary"]))


def step3_verify():
    print("\n== ③ 核对（只读，确认预约挂对了行） ==")
    before = snapshot31()
    r = va.verify("BESTAR", PLAN)          # paste the same batch => expectations
    check("③ 3 个柜号行全部核对", r["summary"]["rows"] == 3, str(r["summary"]))
    check("③ 0 问题", r["summary"]["problems"] == 0, json.dumps(
        r["summary"], ensure_ascii=False))
    got = {}
    for res in r["results"]:
        for row in res["rows"]:
            got[row["route"]] = (row["appointment"] or {}).get("isa")
    check("YVR4 → ISA 7404870996", got.get("YVR4") == 7404870996, str(got))
    check("YEG1 → ISA 7229372996", got.get("YEG1") == 7229372996, str(got))
    check("YEG2 → ISA 147621024984", got.get("YEG2") == 147621024984, str(got))
    check("③ 未改动任何数据（只读）", snapshot31() == before)

    # negative control: wrong expected ISA must be caught
    bad = f"{AWB}\tYVR4\t1\t14\t7229372996\t08/03/2026 13:00 PDT"
    rb = va.verify("BESTAR", bad)
    flags = [f for x in rb["results"] for row in x["rows"] for f in row["flags"]]
    check("③ 期望 ISA 不符时报 isa_diff", "isa_diff" in flags, str(flags))


def main():
    assert lark.env() == "dev"
    assert lark.table_id("3.1") == DEV31 and lark.table_id("5.6") == DEV56 \
        and lark.table_id("5.2 BESTAR-CAL") == DEV52
    print(f"[env] dev  3.1={DEV31}  5.6={DEV56}  5.2={DEV52}  柜号={AWB}")
    t0 = time.time()
    cleanup()
    step2_blocked_before_step1()
    step1_create_appointments()
    step2_sync_plans()
    step3_verify()
    print(f"\n{'=' * 62}\nRESULT: {PASS} passed, {len(FAIL)} failed"
          + (f" -> {FAIL}" if FAIL else "") + f"  ({time.time() - t0:.0f}s)")
    print("\n—— 留存数据（DEV，供人工核对；下次运行前清理）——")
    print(f"  DEV 5.6: {' / '.join(map(str, ISAS))}（账号 BESTAR）")
    print(f"  DEV 5.2: {len(CREATED['trips52'])} 条出库计划 "
          f"{', '.join(CREATED['trips52'])}")
    print(f"  DEV 3.1: {AWB} 的 YVR4/YEG1/YEG2 三行（实际板数 1/1/3 + 已挂出库计划）")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
