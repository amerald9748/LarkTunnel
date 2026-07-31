# -*- coding: utf-8 -*-
"""
integration_dev.py — LIVE full-pipeline test against the DEV tables.

    python webapp/tests/integration_dev.py

WHAT IT TOUCHES (dev copies ONLY — nothing prod):
    * DEV 3.1  tblQcXC82tDveeSp   * DEV 5.6  tblIsKv8k3vvDs0B
    * DEV 5.2  tblJaXrkXhZr3R6N

CLEANUP CONTRACT — the user keeps REAL copied data in these tables, so the
test must only ever delete ITS OWN artifacts:
    * every record the test creates is tracked in .tmp/integration-artifacts.json;
    * plus marker-based belt-and-braces: 3.1 rows whose 客户批次号 starts with
      TEST-SYNC, 5.6 records with the exact test ISAs (99000000xx), and dev-5.2
      trips LINKED to those test 5.6 records.
    Real rows (e.g. CSGU6249922 / CSNU8938529) are never deleted; the test
    READS them and — in the real-row phase — fills CSNU's empty 实际板数 and
    wires it into a test trip (removed again on the next run's cleanup;
    实际板数 stays, matching 预计板数).
"""
import io
import json
import os
import sys
import time
import threading

os.environ["LARK_ENV"] = "dev"                      # BEFORE importing the client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lark_client as lark        # noqa: E402
import appointment_sync as sync   # noqa: E402

DEV31, DEV56, DEV52 = "tblQcXC82tDveeSp", "tblIsKv8k3vvDs0B", "tblJaXrkXhZr3R6N"
STATE = os.path.join(lark.ROOT, ".tmp", "integration-artifacts.json")

MARK = "TEST-SYNC"
ISA_EXISTING = 9900000001     # seeded into dev 5.6 WITHOUT a trip  (4B-ii)
ISA_GROUP = 9900000002        # created by the group batch          (4B-iii)
ISA_REAL = 9900000003         # created by the REAL-row full chain
ISAS = [ISA_EXISTING, ISA_GROUP, ISA_REAL]

# The user's real copied rows (read + demonstrated on, never deleted):
REAL_OCCUPIED = "CSGU6249922"   # BESTAR YYC4 — 实际板数=18, 预计=18, 箱数=399
REAL_EMPTY = "CSNU8938529"      # BESTAR YVR4 — 实际板数 empty, 预计=11, 箱数=203

ROWS = [
    ("TESTU0000011", "BESTAR", "YYC4", 141, None, "FILL"),
    ("TESTU0000022", "BESTAR", "YYC4", 100, "7", "OCC"),
    ("TESTU0000044", "VAST",   "YVR2", 40,  None, "DEVOFF"),
    ("TESTU0000055", "BESTAR", "YYC4", 80,  None, "GRP-A"),
    ("TESTU0000066", "BESTAR", "YYC4", 70,  None, "GRP-B"),
    ("TESTU0000077", "BESTAR", "YYC1", 50,  None, "BACKFILL"),
    ("TESTU0000088", "BESTAR", "YYC4", 90,  None, "W3JOIN"),
]

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


def api(method, path, payload=None, query=None):
    return lark._api(method, path, payload=payload, query=query)


def base():
    return lark.config_values()["base_token"]


def search(tid, field, op, val, fields=None):
    body = {"filter": {"conjunction": "and",
                       "conditions": [{"field_name": field, "operator": op,
                                       "value": [str(val)]}]},
            "automatic_fields": False}
    if fields:
        body["field_names"] = fields
    return api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/search",
               payload=body, query={"page_size": 100}).get("items", [])


def save_state():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with io.open(STATE, "w", encoding="utf-8") as f:
        json.dump(CREATED, f)


def track(kind, rid):
    if rid and rid not in CREATED[kind]:
        CREATED[kind].append(rid)
        save_state()


def existing_only(tid, rids):
    """Filter ids to those that still exist (batch_delete errors on ghosts)."""
    if not rids:
        return []
    found = []
    for i in range(0, len(rids), 100):
        payload = {"record_ids": rids[i:i + 100], "automatic_fields": False}
        d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/batch_get",
                payload=payload)
        found += [r["record_id"] for r in d.get("records", [])]
    return found


def delete_records(tid, rids):
    rids = existing_only(tid, list(dict.fromkeys(rids)))
    for i in range(0, len(rids), 100):
        api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{tid}/records/batch_delete",
            payload={"records": rids[i:i + 100]})
    return len(rids)


def cleanup():
    """Delete ONLY tracked/marked test artifacts — real data is untouchable."""
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(io.open(STATE, encoding="utf-8"))
        except Exception:
            prev = {}

    # marker sweep: test 5.6 records + the dev trips they link
    trips, recs56 = list(prev.get("trips52") or []), list(prev.get("recs56") or [])
    link56 = "5.2 BESTAR-CAL-P-01 副本-5.6 预约表 副本-5.2 出库计划 卡尔加里"
    for isa in ISAS:
        for r in search(DEV56, "ISA", "is", isa, [link56]):
            recs56.append(r["record_id"])
            trips += lark.link_ids((r.get("fields") or {}).get(link56))
    rows31 = list(prev.get("rows31") or [])
    rows31 += [r["record_id"] for r in search(DEV31, "客户批次号", "contains", MARK)]

    n_t = delete_records(DEV52, trips)
    n_a = delete_records(DEV56, recs56)
    n_r = delete_records(DEV31, rows31)
    for k in CREATED:
        CREATED[k] = []
    save_state()
    print(f"[cleanup] tracked+marked only: trips={n_t}  5.6={n_a}  3.1={n_r}"
          f"（真实数据不受影响）")


def seed():
    recs = []
    for awb, wh, dest, boxes, actual, tag in ROWS:
        f = {"柜号/AWB": awb, "仓库供应商": wh, "目的地路线": dest,
             "箱数": boxes, "客户批次号": f"{MARK}-{tag}"}
        if actual is not None:
            f["实际板数"] = actual
        recs.append({"fields": f})
    made = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV31}/records/batch_create",
               payload={"records": recs}).get("records", [])
    for r in made:
        track("rows31", r.get("record_id"))
    made56 = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV56}/records/batch_create",
                 payload={"records": [{"fields": {"ISA": ISA_EXISTING, "目的地": "YYC1",
                                                  "预约账号": "BESTAR",
                                                  "复制时间列": "2026/08/01 09:00",
                                                  "SourceID": MARK}}]}).get("records", [])
    for r in made56:
        track("recs56", r.get("record_id"))
    print(f"[seed] {len(recs)} dev-3.1 rows + dev-5.6 ISA {ISA_EXISTING} (BESTAR, 无行程)")


def approve_all(planned):
    return [{"line_no": r["line_no"], "sig": r["sig"]}
            for r in planned["rows"] if r["actions"] and not r["blockers"]]


def commit_tracked(warehouse, text, progress=None):
    planned = sync.plan(warehouse, text)
    res = sync.commit(warehouse, text, approve_all(planned), "dev", progress=progress)
    for r in res["rows"]:
        c = r.get("commit") or {}
        track("recs56", c.get("isa_record_id"))
        track("trips52", c.get("trip_record_id"))
    return res


def trip_state(trip_id):
    d = api("POST", f"/open-apis/bitable/v1/apps/{base()}/tables/{DEV52}/records/batch_get",
            payload={"record_ids": [trip_id], "automatic_fields": False})
    recs = d.get("records", [])
    if not recs:
        return None
    ff = recs[0].get("fields") or {}
    return {"inv": lark.link_ids(ff.get("3.1 库存总表 副本-5.2 BESTAR-CAL")),
            "isa": lark.link_ids(ff.get("5.6 预约表 副本-5.2 出库计划 卡尔加里"))}


def phase_plan():
    print("\n== 1 · plan() branches (read-only) ==")
    row = sync.plan("BESTAR", "TESTU0000011 YYC4 4 141")["rows"][0]
    check("fill planned on empty 实际板数",
          [a["type"] for a in row["actions"]] == ["fill_pallets"])
    check("fresh row status = no_plan", row["plan"]["status"] == "no_plan")

    row = sync.plan("BESTAR", "TESTU0000022 YYC4 4 100")["rows"][0]
    check("occupied 实际板数 never overwritten",
          "fill_pallets" not in [a["type"] for a in row["actions"]])

    row = sync.plan("BESTAR", "NOSUCH0000000 YYC4 4 141")["rows"][0]
    check("nomatch reported", bool(row["match_error"]))

    row = sync.plan("VAST", f"TESTU0000044 YVR2 3 40 {ISA_GROUP} "
                            "08/02/2026 10:00 PDT")["rows"][0]
    check("VAST in dev: trips disabled", row["plan"]["status"] == "no_dev_plan_table",
          row["plan"].get("status"))
    check("VAST in dev: only pallet action",
          [a["type"] for a in row["actions"]] == ["fill_pallets"],
          str(row["actions"]))


def phase_real_readonly():
    print("\n== 1b · REAL copied rows (read-only asserts) ==")
    # the user's Example-3 shape stays rejected with the real AWB
    p = sync.parse_line(f"{REAL_OCCUPIED}\tYVR4\t\t96")
    check("real AWB + missing 板数 rejected", "error" in p)

    row = sync.plan("BESTAR", f"{REAL_OCCUPIED} YYC4 18 399")["rows"][0]
    check("real occupied row matched", not row["match_error"],
          str(row.get("match_error")))
    check("real occupied: keep, no actions", row["actions"] == [], str(row["actions"]))
    check("real occupied: no W2 (18 vs 预计 18)",
          not [w for w in row["warnings"] if w.startswith("W2")], str(row["warnings"]))
    check("real occupied: 箱数 399 matches", (row["boxes"] or {}).get("in_31") == 399.0)

    row = sync.plan("BESTAR", f"{REAL_EMPTY} YVR4 11 203")["rows"][0]
    check("real empty row plans fill=11",
          [(a["type"], a.get("value")) for a in row["actions"]] == [("fill_pallets", "11")],
          str(row["actions"]))
    check("real empty: no W1 (11 vs 预计 11)",
          not [w for w in row["warnings"] if w.startswith("W1")], str(row["warnings"]))


def phase_perf_progress():
    print("\n== 1c · batch performance + progress stream (8 lines) ==")
    lines = ["TESTU0000011 YYC4 4 141", "TESTU0000022 YYC4 4 100",
             "TESTU0000055 YYC4 4 80", "TESTU0000066 YYC4 1 70",
             "TESTU0000077 YYC1 2 50", "TESTU0000088 YYC4 25 90",
             f"{REAL_OCCUPIED} YYC4 18 399", f"{REAL_EMPTY} YVR4 11 203"]
    ticks, tlock = [], threading.Lock()

    def progress(**kw):
        with tlock:
            ticks.append(dict(kw))
    t0 = time.time()
    r = sync.plan("BESTAR", "\n".join(lines), progress=progress)
    took = time.time() - t0
    print(f"  [perf] plan 8 行 = {took:.1f}s（并行 {sync.PLAN_WORKERS} worker）")
    check("all 8 rows planned", len(r["rows"]) == 8)
    check("rows keep input order",
          [row["parsed"]["awb"] for row in r["rows"]]
          == [l.split()[0] for l in lines])
    dones = [t["done"] for t in ticks if t.get("done") is not None]
    check("progress reached 8/8", 8 in dones, str(sorted(set(dones))))
    check("progress carried row labels",
          any((t.get("current") or "").startswith("第") for t in ticks))
    check("plan under 60s", took < 60, f"{took:.1f}s")


def phase_group_create():
    print("\n== 2 · 4B(iii): unknown ISA -> ONE 5.6 + ONE trip for the group ==")
    text = (f"TESTU0000055 YYC4 4 80 {ISA_GROUP} 08/02/2026 10:00 PDT\n"
            f"TESTU0000066 YYC4 1 70 {ISA_GROUP} 08/02/2026 10:00 PDT")
    planned = sync.plan("BESTAR", text)
    for r in planned["rows"]:
        check(f"line {r['line_no']} plans create_isa_and_trip",
              r["plan"]["status"] == "create_isa_and_trip", r["plan"].get("status"))

    stages, slock = [], threading.Lock()

    def progress(**kw):
        if kw.get("stage"):
            with slock:
                stages.append(kw["stage"])
    res = sync.commit("BESTAR", text, approve_all(planned), "dev", progress=progress)
    for r in res["rows"]:
        c = r.get("commit") or {}
        track("recs56", c.get("isa_record_id"))
        track("trips52", c.get("trip_record_id"))
    rows = [r for r in res["rows"] if r.get("approved")]
    for r in rows:
        check(f"line {r['line_no']} verified", r["commit"].get("verified") is True,
              str(r["commit"]))
    trip_ids = {r["commit"].get("trip_record_id") for r in rows}
    isa_recs = {r["commit"].get("isa_record_id") for r in rows}
    check("group produced exactly ONE trip", len(trip_ids) == 1, str(trip_ids))
    check("group produced exactly ONE 5.6 record", len(isa_recs) == 1, str(isa_recs))

    joined = " | ".join(stages)
    check("commit streamed stage progress",
          "复检" in joined and "写入 1/5" in joined and "核实 5/5" in joined
          and stages[-1] == "完成", joined[:200])

    trip_id, isa_rec = trip_ids.pop(), isa_recs.pop()
    st = trip_state(trip_id)
    check("trip links BOTH dev-3.1 rows", st and len(st["inv"]) == 2, str(st))
    check("trip links the new dev-5.6 record", st and st["isa"] == [isa_rec], str(st))

    row = sync.plan("BESTAR", text)["rows"][0]
    check("replan shows has_plan_match", row["plan"]["status"] == "has_plan_match",
          row["plan"].get("status"))
    return trip_id


def phase_mismatch():
    print("\n== 3 · 4A mismatch: new time -> edit the LINKED 5.6 record ==")
    text = f"TESTU0000055 YYC4 4 80 {ISA_GROUP} 08/03/2026 11:00 PDT"
    planned = sync.plan("BESTAR", text)
    row = planned["rows"][0]
    check("mismatch detected", row["plan"]["status"] == "has_plan_mismatch",
          row["plan"].get("status"))
    res = sync.commit("BESTAR", text, approve_all(planned), "dev")
    c = (res["rows"][0].get("commit") or {})
    check("update verified", c.get("verified") is True, str(c))
    got = search(DEV56, "ISA", "is", ISA_GROUP, ["复制时间列"])
    t = sync.norm_time(lark.flat_text((got[0].get("fields") or {}).get("复制时间列"))) \
        if got else None
    check("dev 5.6 time now 2026/08/03 11:00", t == "2026/08/03 11:00", str(t))
    row2 = sync.plan("BESTAR", f"TESTU0000066 YYC4 1 70 {ISA_GROUP} "
                               "08/03/2026 11:00 PDT")["rows"][0]
    check("sibling now matches too", row2["plan"]["status"] == "has_plan_match",
          row2["plan"].get("status"))


def phase_backfill():
    print("\n== 4 · 4B(ii): ISA exists w/o trip -> create trip + link ==")
    text = f"TESTU0000077 YYC1 2 50 {ISA_EXISTING} 08/01/2026 09:00 MDT"
    planned = sync.plan("BESTAR", text)
    row = planned["rows"][0]
    types = [a["type"] for a in row["actions"]]
    check("plans create_trip WITHOUT create_isa",
          "create_trip" in types and "create_isa" not in types, str(types))
    res = commit_tracked("BESTAR", text)
    c = (res["rows"][0].get("commit") or {})
    check("backfill verified", c.get("verified") is True, str(c))
    st = trip_state(c.get("trip_record_id"))
    check("trip links the seeded 5.6 ISA", st and len(st["isa"]) == 1, str(st))
    check("trip links the 3.1 row", st and len(st["inv"]) == 1, str(st))


def phase_w3_join(group_trip):
    print("\n== 5 · 4B(i): join existing trip + W3 overflow ==")
    text = f"TESTU0000088 YYC4 25 90 {ISA_GROUP} 08/03/2026 11:00 PDT"
    planned = sync.plan("BESTAR", text)
    row = planned["rows"][0]
    check("plans link_existing", row["plan"]["status"] == "link_existing",
          row["plan"].get("status"))
    check("W3 overflow warned (5+25=30 > 28)",
          any(w.startswith("W3") for w in row["warnings"]), str(row["warnings"]))
    res = commit_tracked("BESTAR", text)
    c = (res["rows"][0].get("commit") or {})
    check("join verified", c.get("verified") is True, str(c))
    st = trip_state(group_trip)
    check("group trip now links 3 shipments", st and len(st["inv"]) == 3, str(st))
    check("warnings summary carries W3",
          any("W3" in w for w in res.get("warnings_summary") or []))


def phase_real_full_chain():
    print("\n== 6 · REAL row full chain: fill + create ISA + trip + link ==")
    text = f"{REAL_EMPTY} YVR4 11 203 {ISA_REAL} 08/04/2026 09:00 PDT"
    planned = sync.plan("BESTAR", text)
    row = planned["rows"][0]
    types = [a["type"] for a in row["actions"]]
    check("real row plans fill + create_isa + trip + link",
          types == ["fill_pallets", "create_isa", "create_trip", "link_trip"],
          str(types))
    res = commit_tracked("BESTAR", text)
    c = (res["rows"][0].get("commit") or {})
    check("real-row chain verified", c.get("verified") is True, str(c))
    live = search(DEV31, "柜号/AWB", "is", REAL_EMPTY, ["实际板数"])
    got = lark.flat_text((live[0].get("fields") or {}).get("实际板数")) if live else "?"
    check("real row 实际板数 = '11'", got == "11", got)
    # replay safety on the REAL row: nothing further to do
    row2 = sync.plan("BESTAR", text)["rows"][0]
    check("real row replan = has_plan_match, no actions",
          row2["plan"]["status"] == "has_plan_match" and row2["actions"] == [],
          f"{row2['plan'].get('status')} {row2['actions']}")
    return c.get("trip_record_id")


def main():
    assert lark.env() == "dev", "refusing to run outside LARK_ENV=dev"
    assert lark.table_id("3.1") == DEV31 and lark.table_id("5.6") == DEV56 \
        and lark.table_id("5.2 BESTAR-CAL") == DEV52, \
        "table_id() did not resolve to the dev copies — check config.js devTables"
    print(f"[env] dev  3.1={DEV31}  5.6={DEV56}  5.2={DEV52}")
    t0 = time.time()
    cleanup()
    seed()
    phase_plan()
    phase_real_readonly()
    phase_perf_progress()
    trip = phase_group_create()
    phase_mismatch()
    phase_backfill()
    phase_w3_join(trip)
    real_trip = phase_real_full_chain()

    print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {len(FAIL)} failed"
          + (f" -> {FAIL}" if FAIL else "") + f"  ({time.time() - t0:.0f}s)")
    print("\n—— 留存数据（供人工核对；仅测试数据会在下次运行前清理）——")
    print(f"  DEV 3.1: 7 行 TEST-SYNC-* + 真实行 {REAL_EMPTY}（实际板数=11 已由流程填入并保留）")
    print(f"  DEV 5.6: ISA {ISA_EXISTING} / {ISA_GROUP}（时间已改 08/03 11:00）/ {ISA_REAL}")
    print(f"  DEV 5.2: 行程 {trip}（3 票）+ 补建行程 + 真实行行程 {real_trip}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
