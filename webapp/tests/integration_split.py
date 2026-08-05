# -*- coding: utf-8 -*-
"""
integration_split.py — LIVE test of split-shipment disambiguation with the
operator's exact 10-line dataset (same 柜号+路线 appearing twice, 箱数 0 rows,
XCAB destination), run through the full ①→②→③ flow on the DEV tables.

    python webapp/tests/integration_split.py

Seeds DEV 3.1 with rows marked 客户批次号 TEST-SPLIT-*; tracks everything it
creates in .tmp/integration-split.json and removes only that on the next run.
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
STATE = os.path.join(lark.ROOT, ".tmp", "integration-split.json")
MARK = "TEST-SPLIT"

# The operator's dataset, verbatim (incl. trailing tabs / double spaces).
PLAN = ("OOCU9733972\tYVR4\t3\t98\t7404870996\t08/03/2026 13:00 PDT\n"
        "OOCU9733972\tYEG1\t8\t706\t7227802996\t08/02/2026 07:00 MDT\n"
        "OOCU9733972\tYEG1\t23\t0\t7229372996\t08/05/2026 07:00 MDT \t\n"
        "OOCU9733972\tYYC4\t2\t30\t129695028975\t08/02/2026 10:00 MDT\n"
        "OOCU9733972\tXCAB\t1\t20\t14174251296\t08/03/2026 15:00 MDT\n"
        "OOCU7443104\tYYC4\t13\t0\t129713028975\t07/31/2026 18:00 MDT\t\n"
        "FFAU6002174\tYEG1\t10\t593\t7230852996\t08/05/2026 23:00 MDT\n"
        "FFAU6002174\tYEG1\t21\t0\t7230862996\t 08/06/2026 23:00 MDT\n"
        "FFAU6002174\tYEG2\t2\t26\t147658024984\t08/04/2026 11:00 MDT\n"
        "FFAU6002174\tYYC4\t11\t271\t129638028975\t08/03/2026 12:00 MDT")

# 3.1 seed rows: (awb, dest, 箱数). 实际板数 left EMPTY.
SEED31 = [("OOCU9733972", "YVR4", 98), ("OOCU9733972", "YEG1", 706),
          ("OOCU9733972", "YEG1", 0), ("OOCU9733972", "YYC4", 30),
          ("OOCU9733972", "XCAB", 20), ("OOCU7443104", "YYC4", 0),
          ("FFAU6002174", "YEG1", 593), ("FFAU6002174", "YEG1", 0),
          ("FFAU6002174", "YEG2", 26), ("FFAU6002174", "YYC4", 271)]

# provided 板数 per (awb, dest, 箱数). The EXPECTED post-commit 实际板数 is
# computed at seed time: rows that ALREADY carry a value keep it (fill-only,
# never overwrite) — the operator has real pre-filled rows in dev.
PROVIDED = {("OOCU9733972", "YVR4", 98): "3", ("OOCU9733972", "YEG1", 706): "8",
            ("OOCU9733972", "YEG1", 0): "23", ("OOCU9733972", "YYC4", 30): "2",
            ("OOCU9733972", "XCAB", 20): "1", ("OOCU7443104", "YYC4", 0): "13",
            ("FFAU6002174", "YEG1", 593): "10", ("FFAU6002174", "YEG1", 0): "21",
            ("FFAU6002174", "YEG2", 26): "2", ("FFAU6002174", "YYC4", 271): "11"}
EXPECT = {}      # filled by seed(): pre-existing value or the provided one

APPTS = ("YVR4\t7404870996\t08/03/2026 13:00 PDT\n"
         "YEG1\t7227802996\t08/02/2026 07:00 MDT\n"
         "YEG1\t7229372996\t08/05/2026 07:00 MDT\n"
         "YYC4\t129695028975\t08/02/2026 10:00 MDT\n"
         "XCAB\t14174251296\t08/03/2026 15:00 MDT\n"
         "YYC4\t129713028975\t07/31/2026 18:00 MDT\n"
         "YEG1\t7230852996\t08/05/2026 23:00 MDT\n"
         "YEG1\t7230862996\t08/06/2026 23:00 MDT\n"
         "YEG2\t147658024984\t08/04/2026 11:00 MDT\n"
         "YYC4\t129638028975\t08/03/2026 12:00 MDT")
ISAS = [7404870996, 7227802996, 7229372996, 129695028975, 14174251296,
        129713028975, 7230852996, 7230862996, 147658024984, 129638028975]

PASS, FAIL = 0, []
CREATED = {"rows31": [], "recs56": [], "trips52": []}


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


def search(tid, field, op, val, fields=None):
    body = {"filter": {"conjunction": "and", "conditions": [
        {"field_name": field, "operator": op, "value": [str(val)]}]},
        "automatic_fields": False}
    if fields:
        body["field_names"] = fields
    return api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/search",
               payload=body, query={"page_size": 100}).get("items", [])


def save_state():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(CREATED, io.open(STATE, "w", encoding="utf-8"))


def track(kind, rid):
    if rid and rid not in CREATED[kind]:
        CREATED[kind].append(rid)
        save_state()


def delete_tolerant(tid, rids):
    rids = list(dict.fromkeys(r for r in rids if r))
    if not rids:
        return 0
    d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/batch_get",
            payload={"record_ids": rids, "automatic_fields": False})
    alive = [r["record_id"] for r in d.get("records", [])]
    if alive:
        api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/batch_delete",
            payload={"records": alive})
    return len(alive)


def cleanup():
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(io.open(STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    trips = list(prev.get("trips52") or [])
    recs = list(prev.get("recs56") or [])
    link56 = "5.2 BESTAR-CAL-P-01 副本-5.6 预约表 副本-5.2 出库计划 卡尔加里"
    for isa in ISAS:
        for r in search(DEV56, "ISA", "is", isa, [link56]):
            recs.append(r["record_id"])
            trips += lark.link_ids((r.get("fields") or {}).get(link56))
    rows = list(prev.get("rows31") or [])
    rows += [r["record_id"] for r in search(DEV31, "客户批次号", "contains", MARK)]
    n_t = delete_tolerant(DEV52, trips)
    n_a = delete_tolerant(DEV56, recs)
    n_r = delete_tolerant(DEV31, rows)
    for k in CREATED:
        CREATED[k] = []
    save_state()
    c56._RECENT.clear()
    print(f"[cleanup] trips={n_t}  5.6={n_a}  3.1={n_r}（仅本套件产物）")


def seed():
    """ADOPT existing rows, create only the missing ones. The operator keeps
    real hand-made rows in dev (e.g. the two FFAU6002174 YEG1 split rows with
    实际板数 pre-filled) — blindly seeding duplicates would create 4-way
    ambiguity that even 箱数+板数 cannot split (caught live on first run:
    the planner correctly REFUSED to guess). Never delete or modify theirs."""
    have = {}
    for awb in {a for a, _, _ in SEED31}:
        for r in search(DEV31, "柜号/AWB", "is", awb,
                        ["目的地路线", "箱数", "实际板数", "仓库供应商"]):
            f = r["fields"]
            if f.get("仓库供应商") != "BESTAR":
                continue
            key = (awb, f.get("目的地路线"), int(lark.num_of(f.get("箱数")) or 0))
            have[key] = lark.flat_text(f.get("实际板数")) or None
    to_create = []
    for i, (awb, dest, boxes) in enumerate(SEED31):
        key = (awb, dest, boxes)
        if key in have:
            EXPECT[key] = have[key] or PROVIDED[key]   # occupied keeps its value
        else:
            EXPECT[key] = PROVIDED[key]
            to_create.append({"fields": {"柜号/AWB": awb, "仓库供应商": "BESTAR",
                                         "目的地路线": dest, "箱数": boxes,
                                         "客户批次号": f"{MARK}-{i}"}})
    if to_create:
        made = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV31}"
                   "/records/batch_create",
                   payload={"records": to_create}).get("records", [])
        for r in made:
            track("rows31", r.get("record_id"))
    print(f"[seed] 采用现有 {len(have)} 行 + 新建 {len(to_create)} 行"
          f"（含拆行、箱数 0、XCAB）")


def step1():
    print("\n== ① 新建预约：10 个 ISA（已存在的自动跳过） ==")
    p = c56.plan("BESTAR", APPTS)
    s = p["summary"]
    check("① 全部可处理（新建+已存在 = 10）",
          s["create"] + s["exists"] == 10 and s["block"] == 0, str(s))
    res = c56.commit("BESTAR", APPTS,
                     [{"line_no": r["line_no"], "sig": r["sig"]}
                      for r in p["rows"] if r["action"] == "create"], "dev")
    for r in res["rows"]:
        track("recs56", (r.get("commit") or {}).get("record_id"))
    made = [r for r in res["rows"] if (r.get("commit") or {}).get("record_id")]
    check("① 新建的全部核实", all(r["commit"].get("verified") for r in made))
    n = 0
    for isa in ISAS:
        n += len(search(DEV56, "ISA", "is", isa))
    check("① 10 个 ISA 各恰好 1 条", n == 10, str(n))


def rows_by_key():
    out = {}
    plan_link = sync._env_wiring("5.2 BESTAR-CAL")["plan_link_31"]
    for awb in ("OOCU9733972", "OOCU7443104", "FFAU6002174"):
        for r in search(DEV31, "柜号/AWB", "is", awb,
                        ["目的地路线", "箱数", "实际板数", plan_link]):
            f = r["fields"]
            key = (awb, f.get("目的地路线"),
                   int(lark.num_of(f.get("箱数")) or 0))
            out[key] = {"rid": r["record_id"],
                        "actual": lark.flat_text(f.get("实际板数")) or None,
                        "links": lark.link_ids(f.get(plan_link))}
    return out


def step2():
    print("\n== ② 计划同步：10 行（含拆行、箱数 0、XCAB） ==")
    p = sync.plan("BESTAR", PLAN)
    errs = [(r["line_no"], r.get("parse_error") or r.get("match_error"),
             r["blockers"]) for r in p["rows"]
            if r.get("parse_error") or r.get("match_error") or r["blockers"]]
    check("② 10 行全部无错误/无拦截", not errs, str(errs))
    check("② 10 行全部可执行", p["summary"]["actionable"] == 10,
          str(p["summary"]))
    # the two split pairs matched DIFFERENT records
    for pair in ((2, 3), (7, 8)):
        matches = [(next(r for r in p["rows"] if r["line_no"] == n)
                    .get("match") or {}).get("record_id") for n in pair]
        check(f"② 第{pair[0]}/{pair[1]}行（同柜号+路线）匹配到不同记录",
              all(matches) and matches[0] != matches[1], str(matches))
    # ≥4: the two seeded split pairs, PLUS however many of the operator's own
    # hand-made rows share a 柜号+路线 with a batch line (real dev data).
    notes = [n for r in p["rows"] for n in r["notes"] if "精确匹配" in n]
    check("② 拆行匹配有明确说明（≥4）", len(notes) >= 4, str(len(notes)))

    res = sync.commit("BESTAR", PLAN,
                      [{"line_no": r["line_no"], "sig": r["sig"]}
                       for r in p["rows"] if r["actions"] and not r["blockers"]],
                      "dev")
    ok = [r for r in res["rows"] if r.get("approved")]
    for r in ok:
        track("trips52", (r.get("commit") or {}).get("trip_record_id"))
    check("② 10 行全部写入并回读核实",
          len(ok) == 10 and all((r.get("commit") or {}).get("verified") for r in ok),
          str([(r["line_no"], (r.get("commit") or {}).get("error")) for r in ok]))

    state = rows_by_key()
    good = all(state.get(k, {}).get("actual") == v for k, v in EXPECT.items())
    check("② 实际板数逐行落在正确的记录上（10/10）", good,
          json.dumps({f"{k}": (state.get(k) or {}).get("actual")
                      for k in EXPECT}, ensure_ascii=False))
    # link check on the EXACT records the plan matched (keying by
    # awb+dest+箱数 can collide with the operator's other hand-made rows —
    # e.g. two YEG1/箱0 rows where only the 板数 tie-break separated them)
    plan_link = sync._env_wiring("5.2 BESTAR-CAL")["plan_link_31"]
    rids = [r["match"]["record_id"] for r in ok]
    d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV31}"
            "/records/batch_get",
            payload={"record_ids": rids, "automatic_fields": False})
    links = {rec["record_id"]: lark.link_ids((rec.get("fields") or {}).get(plan_link))
             for rec in d.get("records", [])}
    check("② 批内 10 行（按匹配到的记录）都挂上了各自的出库计划",
          len(links) == 10 and all(len(v) == 1 for v in links.values()),
          json.dumps(links, ensure_ascii=False))

    p2 = sync.plan("BESTAR", PLAN)
    states = {r["plan"]["status"] for r in p2["rows"]}
    check("② 重跑：全部 has_plan_match（拆行匹配稳定）",
          states == {"has_plan_match"}, str(states))


def step3():
    print("\n== ③ 核对：同一批数据（含拆行缩窄） ==")
    r = va.verify("BESTAR", PLAN)
    check("③ 每行缩窄到 1 条记录（共 10）", r["summary"]["rows"] == 10,
          str(r["summary"]))
    check("③ 0 问题", r["summary"]["problems"] == 0,
          json.dumps(r["summary"], ensure_ascii=False))
    # the split lines each point at THEIR OWN appointment
    got = {}
    for res in r["results"]:
        for row in res["rows"]:
            got[(row["awb"], row["route"], int(row["boxes"] or 0))] = \
                (row["appointment"] or {}).get("isa")
    check("③ YEG1 拆行各自挂对预约",
          got.get(("OOCU9733972", "YEG1", 706)) == 7227802996
          and got.get(("OOCU9733972", "YEG1", 0)) == 7229372996
          and got.get(("FFAU6002174", "YEG1", 593)) == 7230852996
          and got.get(("FFAU6002174", "YEG1", 0)) == 7230862996, str(got))
    check("③ XCAB 行挂对预约", got.get(("OOCU9733972", "XCAB", 20)) == 14174251296)


def main():
    assert lark.env() == "dev" and lark.table_id("3.1") == DEV31
    print(f"[env] dev  3.1={DEV31}  5.6={DEV56}  5.2={DEV52}")
    t0 = time.time()
    cleanup()
    seed()
    step1()
    step2()
    step3()
    print(f"\n{'=' * 62}\nRESULT: {PASS} passed, {len(FAIL)} failed"
          + (f" -> {FAIL}" if FAIL else "") + f"  ({time.time() - t0:.0f}s)")
    print("\n—— 留存数据（DEV，供人工核对；下次运行前清理）——")
    print(f"  DEV 3.1: 10 行 TEST-SPLIT-*（板数已按行填入，拆行各自挂计划）")
    print(f"  DEV 5.6: {len(ISAS)} 个 ISA · DEV 5.2: {len(CREATED['trips52'])} 条出库计划")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
