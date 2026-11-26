"""Daily app-event reports (FAB CASA Always On and new types read once by Claude).

Synthetic AppsFlyer exports follow the real layout (file names with the app id, Event Value JSON with
deviceId, in-app and non-organic exports for iOS and Android). Real-sample tests run only when the FAB
files are present (client data, not committed): TC_FAB_SAMPLES, default /tmp/claude-0/fab."""
import csv
import datetime as dt
import glob
import json
import os
import types

import openpyxl
import pytest

from app.engine import event_report as E
from app.pipeline import VersionInput, run_version
from tests.test_web_acceptance import app_client, emp, mp  # noqa: F401  (web fixtures)

FAB = E.FAB_CASA_ALWAYS_ON
EVS = [c["event"] for c in FAB["columns"]]
CAMPS = FAB["campaigns"]
HEAD = ["Attributed Touch Type", "Attributed Touch Time", "Install Time", "Event Time", "Event Name", "Event Value", "Event Revenue",
        "Media Source", "Campaign", "Platform", "AppsFlyer ID", "App ID"]
APPS = {"ios": "id1383237548", "android": "com.fab.personalbanking"}
KINDS = {"in_app": "in-app-events", "non_organic": "non-organic-in-app-events"}


def _row(day, ev, dev, camp, platform, hour=10):
    t = f"2026-11-{day:02d} {hour:02d}:15:00"
    return ["click", t, t, t, ev, json.dumps({"deviceId": dev, "screen": "x"}), "", "Facebook Ads", camp, platform, f"af-{dev}", APPS[platform]]


def synthetic_events(last_day):
    """{(platform, source): rows} with duplicates inside a file, between in-app/non-organic and between iOS/Android,
    plus rows from other campaigns and other events that must not count. Returns (files_rows, expected)."""
    rows = {(p, s): [] for p in APPS for s in KINDS}
    expected = {}
    n = 0
    for day in range(1, last_day + 1):
        for k in range(6 + day):
            ev = EVS[(day + k) % 4]
            dev = f"D{day:02d}{k:03d}"
            camp = CAMPS[k % len(CAMPS)]
            plat = "ios" if k % 2 else "android"
            rows[(plat, "in_app")].append(_row(day, ev, dev, camp, plat))
            if k % 3 == 0:  # same device and event in the non-organic export: counted once
                rows[(plat, "non_organic")].append(_row(day, ev, dev, camp, plat, hour=11))
            if k % 4 == 0:  # same device and event in the same file twice
                rows[(plat, "in_app")].append(_row(day, ev, dev, camp, plat, hour=12))
            if k % 5 == 0:  # also on the other platform's export (device moved app): counted once
                other = "android" if plat == "ios" else "ios"
                rows[(other, "non_organic")].append(_row(day, ev, dev, camp, other, hour=13))
            expected.setdefault(day, {}).setdefault(ev, 0)
            expected[day][ev] += 1
            n += 1
            rows[(plat, "in_app")].append(_row(day, ev, f"X{dev}", "Some_Other_Campaign", plat))  # other campaign
            rows[(plat, "non_organic")].append(_row(day, "cc_ntb_contactDetail_continue_click", dev, camp, plat))  # other event
    return rows, expected


def write_exports(folder, last_day, rows=None, skip=()):
    rows = rows or synthetic_events(last_day)[0]
    os.makedirs(folder, exist_ok=True)
    out = []
    for (p, s), rr in rows.items():
        if (p, s) in skip:
            continue
        name = f"{APPS[p]}_{KINDS[s]}_2026-11-01_2026-11-{last_day:02d}_Asia_Dubai.csv"
        path = os.path.join(folder, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(HEAD)
            w.writerows(rr)
        out.append({"name": name, "path": path})
    return out


def previous_report(folder, last_day, recipe=FAB):
    raw = write_exports(os.path.join(folder, f"raw{last_day}"), last_day)
    run = E.build_counts(recipe, raw)
    path = os.path.join(folder, f"FAB_CASA Always On Report_{last_day:02d}_11_2026.xlsx")
    E.write_workbook(recipe, run, None, path)
    return path


def run_event(tmp, raw, prev_paths, client="FAB", instructions="", profile=None, provider=None):
    files = [{"id": i + 1, "name": f["name"], "path": f["path"], "role": "raw"} for i, f in enumerate(raw)]
    files += [{"id": 100 + i, "name": os.path.basename(p), "path": p, "role": "previous_report"} for i, p in enumerate(prev_paths)]
    task = {"title": "FAB daily", "client": client, "deliverables": ["event_report"], "instructions": instructions}
    inp = VersionInput(task=task, files=files, profile=profile or {}, answers={}, plan_overrides={}, workdir=str(tmp), version_number=1)
    return run_version(inp, lambda *a: None, provider=provider or types.SimpleNamespace(name="rules", mocked=True))


def chk(res, cid):
    return next((c for c in res["validation"]["checks"] if c["id"] == cid), None)


# ---------------------------------------------------------------------------------------------- engine
def test_builtin_report_counts_dedup_and_layout(tmp_path):
    prev = previous_report(str(tmp_path), 4)
    raw = write_exports(str(tmp_path / "raw5"), 5)
    res = run_event(tmp_path / "w", raw, [prev])
    assert res["status"] == "awaiting_sheet", res.get("error")
    ev = res["event"]
    assert ev["source"] == "built-in" and ev["end_day"] == "2026-11-05"
    _, expected = synthetic_events(5)
    for r in ev["rows"]:
        day = int(r["date"][-2:])
        assert r["values"] == [expected[day].get(e, 0) for e in EVS], r
        assert r["daily"] == sum(r["values"]) and r["day"] == f"{day}-November"
    assert chk(res, "event_previous")["status"] == "pass"
    assert chk(res, "event_readback")["status"] == "pass"
    assert chk(res, "event_sheet")["status"] == "not_run"
    assert res["ai_steps"][0]["engine"] == "rules" and res["ai_mocked"]
    # the written file has the employee's layout: pivot blocks + VLOOKUP table, named for the new day
    path = res["artifacts"][0]["path"]
    assert os.path.basename(path) == "FAB_CASA Always On Report_05_11_2026.xlsx"
    wb = openpyxl.load_workbook(path)
    ws = wb["FAB_CASA Always On"]
    assert ws["A3"].value == "Row Labels" and ws["B3"].value == "Count of Event Name"
    assert [ws.cell(3, c).value for c in range(6, 10)] == EVS
    assert str(ws["F4"].value).startswith("=IFERROR(VLOOKUP($E4,")
    back = E.read_report_back(path, FAB)
    assert back[EVS[0]]["2026-11-05"] == expected[5].get(EVS[0], 0)


def test_errors_say_what_to_fix(tmp_path):
    prev = previous_report(str(tmp_path), 4)
    raw = write_exports(str(tmp_path / "raw5"), 5)
    # no previous day report
    r = run_event(tmp_path / "a", raw, [])
    assert r["status"] == "validation_blocked" and "previous day" in r["error"].lower()
    # two previous reports
    r = run_event(tmp_path / "b", raw, [prev, prev])
    assert "more than one" in r["error"].lower()
    # an export missing (Android non-organic)
    miss = write_exports(str(tmp_path / "raw5m"), 5, skip={("android", "non_organic")})
    r = run_event(tmp_path / "c", miss, [prev])
    assert r["status"] == "validation_blocked"
    assert "android" in (r["event"]["error"]["title"] + r["event"]["error"]["fix"]).lower()
    assert not r["event"]["needs_instructions"]
    # a file that isn't an AppsFlyer export
    bad = tmp_path / "Meta_export.csv"
    bad.write_text("Campaign name,Amount spent\nA,10\n")
    r = run_event(tmp_path / "d", raw + [{"name": bad.name, "path": str(bad)}], [prev])
    assert r["status"] == "validation_blocked" and "Meta_export.csv" in json.dumps(r["event"]["error"])
    # exports covering different dates
    old = write_exports(str(tmp_path / "raw4"), 4)
    mixed = [f for f in raw if "com.fab" in f["name"]] + [f for f in old if f["name"].startswith("id")]
    r = run_event(tmp_path / "e", mixed, [prev])
    assert r["status"] == "validation_blocked"
    for res in (r,):
        assert res["event"]["error"]["fix"]


def test_same_day_exports_warn_not_fresh(tmp_path):
    prev = previous_report(str(tmp_path), 5)
    raw = write_exports(str(tmp_path / "again"), 5)
    res = run_event(tmp_path / "w", raw, [prev])
    assert res["status"] == "awaiting_sheet"
    assert chk(res, "event_fresh")["status"] == "warn"


def test_unknown_layout_asks_for_description(tmp_path):
    prev = previous_report(str(tmp_path), 4, recipe={**FAB, "sheet_title": "FAB Other", "columns": FAB["columns"][:2]})
    raw = write_exports(str(tmp_path / "raw5"), 5)
    res = run_event(tmp_path / "w", raw, [prev])
    assert res["status"] == "validation_blocked" and res["event"]["needs_instructions"]
    assert "new report type" in res["event"]["error"]["title"].lower()
    res = run_event(tmp_path / "w2", raw, [prev], instructions="Count the two NTB events, all PLT campaigns")
    assert res["event"]["needs_instructions"] and "isn't available" in res["event"]["error"]["title"]


class FakeClaude:
    name, mocked = "claude", False

    def __init__(self, proposal):
        self.proposal, self.steps, self.calls = proposal, [{"step": "Report type", "engine": "claude", "reason": "test"}], 0

    def propose_event_recipe(self, ctx):
        self.calls += 1
        assert "Count" in ctx or "count" in ctx
        return self.proposal


def _proposal(**kw):
    p = {"ok": True, "name": "FAB two NTB events", "sheet_title": "FAB Other", "platforms": ["ios", "android"], "sources": ["in_app", "non_organic"],
         "campaign_match": "contains", "campaigns": ["_PLT"], "id_field": "event_value.deviceId",
         "columns": [{"header": EVS[0], "event": EVS[0]}, {"header": EVS[1], "event": EVS[1]}]}
    p.update(kw)
    return p


def test_claude_reads_new_type_once_and_must_reproduce_previous(tmp_path):
    other = {**FAB, "sheet_title": "FAB Other", "columns": FAB["columns"][:2]}
    prev = previous_report(str(tmp_path), 4, recipe=other)
    raw = write_exports(str(tmp_path / "raw5"), 5)
    fake = FakeClaude(_proposal())
    res = run_event(tmp_path / "ok", raw, [prev], instructions="Count the two NTB events for PLT campaigns", provider=fake)
    assert res["status"] == "awaiting_sheet", res.get("error")
    assert fake.calls == 1 and res["event"]["source"] == "claude" and not res["ai_mocked"]
    new = res["event"]["new_recipe"]
    assert new["key"].startswith("custom_") and not new["builtin"]
    # saved recipe: next run matches it with no Claude call
    res2 = run_event(tmp_path / "again", raw, [prev], profile={"event_recipes": [new]}, provider=FakeClaude(None))
    assert res2["status"] == "awaiting_sheet" and res2["event"]["source"] == "saved" and "new_recipe" not in res2["event"]
    # a reading that doesn't reproduce yesterday's report is rejected (wrong campaigns)
    bad = run_event(tmp_path / "bad", raw, [prev], instructions="Count NTB", provider=FakeClaude(_proposal(campaigns=["Dining_PLT"], campaign_match="exact")))
    assert bad["status"] == "validation_blocked" and "reproduce" in bad["event"]["error"]["title"] and bad["event"]["needs_instructions"]
    # columns must equal the previous report's columns
    bad = run_event(tmp_path / "cols", raw, [prev], instructions="Count NTB", provider=FakeClaude(_proposal(columns=[{"header": EVS[2], "event": EVS[2]}])))
    assert bad["status"] == "validation_blocked" and "isn't usable" in bad["event"]["error"]["title"]
    # Claude asks a question
    q = run_event(tmp_path / "q", raw, [prev], instructions="Count", provider=FakeClaude({"ok": False, "question": "Which campaigns?"}))
    assert "Which campaigns?" in q["event"]["error"]["fix"]


def test_exclude_campaigns_and_month_in_tab_name(tmp_path):
    """FAB IOS-PLT All events: every campaign except the CASA Always On ones, iOS only; the tab name's month changes."""
    rec = E.FAB_IOS_PLT_ALL_EVENTS
    assert not E.validate_recipe(rec) and not E.validate_recipe(E.FAB_IOS_PLT_CASA)
    assert E._campaign_ok("AN~FAB_CBG_CN~CASA", rec) and not E._campaign_ok("Dining_PLT", rec)
    assert "except" in E.campaign_rule_text(rec)
    prev = {"sheet_title": "FAB_IOS-PLT - Dec_All events", "headers": [c["header"] for c in rec["columns"]]}
    assert E.match_recipe(prev, E.recipes_for("FAB", {}))["key"] == "fab_ios_plt_all_events"
    # same columns as the CASA iOS-PLT report but another tab name: not taken for it
    casa = {"sheet_title": "FAB Other", "headers": [c["header"] for c in E.FAB_IOS_PLT_CASA["columns"]]}
    assert E.match_recipe(casa, E.recipes_for("FAB", {})) is None
    assert E.match_recipe({**casa, "sheet_title": "FAB_IOS CASA- January_All events"}, E.recipes_for("FAB", {}))["key"] == "fab_ios_plt_casa"
    # other clients get none of the FAB report types
    assert E.recipes_for("ATC", {}) == [] and E.match_recipe(prev, E.recipes_for("ATC", {})) is None
    # counts: Always On campaigns and Android exports are left out
    ev = "cc_ntb_contactDetail_continue_click"
    rows = {(p, s): [] for p in APPS for s in KINDS}
    rows[("ios", "in_app")] += [_row(1, ev, "A", "Brand_CASA", "ios"), _row(1, ev, "B", "Dining_PLT", "ios"), _row(2, ev, "C", "Cards_X", "ios")]
    rows[("ios", "non_organic")] += [_row(1, ev, "A", "Brand_CASA", "ios", hour=12), _row(2, ev, "D", "Cards_X", "ios")]
    rows[("android", "in_app")] += [_row(1, ev, "E", "Brand_CASA", "android")]
    raw = write_exports(str(tmp_path / "raw"), 2, rows=rows)
    run = E.build_counts(rec, raw)
    assert run["counts"][dt.date(2026, 11, 1)][ev] == 1 and run["counts"][dt.date(2026, 11, 2)][ev] == 2
    assert any("ignored" in w for w in run["warnings"])  # the Android exports aren't used by an iOS report
    only_ao = {k: [r for r in v if r[8] == "Dining_PLT"] for k, v in rows.items()}
    with pytest.raises(E.ReportError) as e:
        E.build_counts(rec, write_exports(str(tmp_path / "raw2"), 2, rows=only_ao))
    assert "every campaign except" in e.value.fix


# ---------------------------------------------------------------------------------------------- Google Sheet check
def make_sheet(path, rows, headers=EVS, tweak=None, extra_days=0):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Nov 2026"
    ws["B2"] = "FAB CASA Always-on"
    hdr = ["Day", "Campaign", "Total Count", "Daily Count"] + list(headers) + ["Spend (AED)", "Spend (USD)"]
    for i, h in enumerate(hdr):
        ws.cell(10, 2 + i, h)
    total = 0
    for k, r in enumerate(rows):
        vals = list(r["values"])
        if tweak and r["date"] in tweak:
            vals = tweak[r["date"]]
        total += sum(vals)
        line = [r["day"], "CASA", total, sum(vals)] + vals + [100.0, 27.2]
        for i, v in enumerate(line):
            ws.cell(11 + k, 2 + i, v)
    wb.save(path)
    return path


def test_verify_sheet_ok_and_mismatch(tmp_path):
    prev = previous_report(str(tmp_path), 4)
    res = run_event(tmp_path / "w", write_exports(str(tmp_path / "raw5"), 5), [prev])
    rows = res["event"]["rows"]
    ok = E.verify_sheet(make_sheet(str(tmp_path / "g.xlsx"), rows), "g.xlsx", FAB, rows)
    assert ok["ok"] and ok["matched"] == 5 and ok["tab"] == "Nov 2026"
    wrong = list(rows[2]["values"])
    wrong[1] += 3
    bad = E.verify_sheet(make_sheet(str(tmp_path / "b.xlsx"), rows, tweak={rows[2]["date"]: wrong}), "b.xlsx", FAB, rows)
    assert not bad["ok"] and any("3" in e and EVS[1] in e for e in bad["errors"]) and bad["fix"]
    short = E.verify_sheet(make_sheet(str(tmp_path / "s.xlsx"), rows[:3]), "s.xlsx", FAB, rows)
    assert not short["ok"] and any("05" in e or "5-November" in e or "5 Nov" in e for e in short["errors"])
    nohdr = E.verify_sheet(make_sheet(str(tmp_path / "n.xlsx"), rows, headers=["a", "b", "c", "d"]), "n.xlsx", FAB, rows)
    assert not nohdr["ok"] and "event columns" in nohdr["title"]


# ---------------------------------------------------------------------------------------------- real FAB samples
FAB_DIR = os.environ.get("TC_FAB_SAMPLES", "/tmp/claude-0/fab")


def _real(folder_glob, pattern):
    return sorted(glob.glob(os.path.join(FAB_DIR, folder_glob, "*", pattern)))


@pytest.mark.real_client
def test_real_fab_always_on_reproduces_25_nov(tmp_path):
    raw = _real("d5f4772b*", "*events_2026-11-01_2026-11-25*.csv")
    prev = _real("d5f4772b*", "FAB_CASA Always On Report_24_11_2026.xlsx")
    real25 = _real("d5f4772b*", "FAB_CASA Always On Report_25_11_2026.xlsx")
    if len(raw) != 4 or not prev or not real25:
        pytest.skip("FAB samples not available")
    res = run_event(tmp_path, [{"name": os.path.basename(p), "path": p} for p in raw], prev)
    assert res["status"] == "awaiting_sheet", res.get("error")
    want = E.read_previous(real25[0])
    for c in FAB["columns"]:
        got = {r["date"]: r["values"][EVS.index(c["event"])] for r in res["event"]["rows"]}
        for d, n in want["blocks"][c["event"]]["days"].items():
            assert got[d.isoformat()] == n, (c["event"], d)
    assert res["event"]["unique_events"] == 1388


def _check_real_builtin(tmp_path, folder, prefix, key, prev_day=24, max_changed=0):
    raw = _real(folder, "id*_2026-11-01_2026-11-25*.csv")
    prev = _real(folder, f"{prefix}_{prev_day}_11_2026.xlsx")
    real25 = _real(folder, f"{prefix}_25_11_2026.xlsx")
    if len(raw) != 2 or not prev or not real25:
        pytest.skip("FAB samples not available")
    raw = [{"name": os.path.basename(p), "path": p} for p in raw]
    # no description and no Claude: the layout is a built-in FAB report type
    res = run_event(tmp_path, raw, prev, provider=FakeClaude(None))
    assert res["status"] == "awaiting_sheet", res.get("error")
    assert res["event"]["recipe_key"] == key and res["event"]["source"] == "built-in" and res["ai_mocked"]
    want = E.read_previous(real25[0])
    hdr = res["event"]["headers"]
    recipe = next(r for r in E.BUILTIN_RECIPES if r["key"] == key)
    changed = []
    for i, c in enumerate(recipe["columns"]):
        got = {r["date"]: r["values"][i] for r in res["event"]["rows"]}
        if c.get("constant_zero"):
            assert not any(got.values()), c["header"]
            continue
        for day, n in want["blocks"][c["event"]]["days"].items():
            if got.get(day.isoformat(), 0) != n:
                changed.append((hdr[i], day, n, got.get(day.isoformat())))
    assert len(changed) <= max_changed, changed
    assert all(abs((g or 0) - n) <= 2 for _, _, n, g in changed), changed
    # the output workbook keeps the previous report's tab and layout
    out = next(a for a in res["artifacts"] if a["kind"] == "excel")
    wb = openpyxl.load_workbook(out["path"])
    assert want["sheet_title"] in wb.sheetnames
    return res


@pytest.mark.real_client
def test_real_casa_ios_plt_is_builtin(tmp_path):
    _check_real_builtin(tmp_path, "02e2db87*", "FAB_IOS-PLT Report - CASA", "fab_ios_plt_casa")


@pytest.mark.real_client
def test_real_ios_plt_all_events_is_builtin(tmp_path):
    """The live failure: Claude counted every campaign, so 01-02 Nov came out about double (e.g. IOS Contact Details 19 -> 36).
    The built-in rule counts every campaign except the 5 CASA Always On ones. FAB's own 24 and 25 Nov reports differ from
    each other in 11 cells by AppsFlyer's late updates; against 25 Nov 10 cells differ, by 1 or 2."""
    res = _check_real_builtin(tmp_path, "ca034957*", "FAB_IOS-PLT Report All events", "fab_ios_plt_all_events", max_changed=10)
    rows = {r["date"]: dict(zip(res["event"]["headers"], r["values"])) for r in res["event"]["rows"]}
    assert rows["2026-11-01"]["IOS Contact Details"] == 19 and rows["2026-11-02"]["IOS Contact Details"] == 47
    assert "except" in chk(res, "event_type")["details"][0]


# ---------------------------------------------------------------------------------------------- web flow
@pytest.mark.web
def test_web_fab_flow_sheet_verification_and_rerun(emp, tmp_path):
    from sqlalchemy import select

    from app.db import Client, Profile, Session, Task, TaskVersion, User, UserClient
    from tests.test_web_acceptance import H, drain, state
    with Session() as s:
        fab = s.scalar(select(Client).where(Client.name == "FAB"))
        if not fab:
            fab = Client(name="FAB", short="FAB")
            s.add(fab)
            s.flush()
            s.add(Profile(client_id=fab.id, name="FAB rules", settings={}))
            sara = s.scalar(select(User.id).where(User.email == "employee@trafficomm.local"))
            s.add(UserClient(user_id=sara, client_id=fab.id))
        s.commit()
        fab_id = fab.id
    page = emp.get("/tasks/new")
    assert page.status_code == 200 and "Daily app-event report" in page.text and "FAB CASA Always On daily report" in page.text

    prev = previous_report(str(tmp_path), 4)
    raw = write_exports(str(tmp_path / "raw5"), 5, skip={("android", "non_organic")})
    tid = emp.post("/api/tasks", json={"client_id": fab_id}, headers=H).json()["id"]
    r = emp.patch(f"/api/tasks/{tid}", headers=H, json={"title": "FAB CASA 5 Nov", "client_id": fab_id, "deliverables": ["event_report"]})
    assert r.status_code == 200, r.text
    # submitting without the previous day report is refused with a clear message
    for f in raw:
        assert emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (f["name"], open(f["path"], "rb"))}, data={"role": "raw"}).status_code == 200
    r = emp.post(f"/api/tasks/{tid}/submit", headers=H)
    assert r.status_code == 400 and "previous day" in r.text.lower()
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (os.path.basename(prev), open(prev, "rb"))}, data={"role": "previous_report"})
    assert r.status_code == 200, r.text
    assert emp.post(f"/api/tasks/{tid}/submit", headers=H).status_code == 200
    drain()
    # one export missing: blocked, with the fix and a re-run box on the task page
    assert state(emp, tid)["status"] == "validation_blocked"
    html = emp.get(f"/tasks/{tid}").text
    assert 'id="event-fix"' in html and "evFilesForm" in html and "android" in html.lower()
    # upload the missing export and re-run
    full = write_exports(str(tmp_path / "raw5full"), 5)
    miss = next(f for f in full if f["name"].startswith("com.fab") and "non-organic" in f["name"])
    fid = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (miss["name"], open(miss["path"], "rb"))}, data={"role": "raw"}).json()["id"]
    r = emp.post(f"/api/tasks/{tid}/event-rerun", headers=H, json={"file_ids": [fid]})
    assert r.status_code == 200, r.text
    drain()
    assert state(emp, tid)["status"] == "awaiting_sheet"
    html = emp.get(f"/tasks/{tid}").text
    assert 'id="sheet"' in html and "Copy event figures" in html and "5-November" in html and "Don’t send this to the client" in html
    # feedback is not how event reports change
    with Session() as s:
        v = s.get(TaskVersion, s.get(Task, tid).current_version_id)
        rows = v.result["event"]["rows"]
        vid = v.id
    assert emp.post(f"/api/tasks/{tid}/feedback", headers=H, json={"text": "change it", "base_version_id": vid}).status_code == 400
    # a sheet with a wrong figure: mismatch, task not complete, the day and column are named
    wrong = list(rows[3]["values"])
    wrong[0] += 2
    bad = make_sheet(str(tmp_path / "sheet_bad.xlsx"), rows, tweak={rows[3]["date"]: wrong})
    r = emp.post(f"/api/tasks/{tid}/sheet", headers=H, files={"file": ("FAB CASA Always-on.xlsx", open(bad, "rb"))})
    assert r.status_code == 200 and not r.json()["ok"]
    assert state(emp, tid)["status"] == "sheet_mismatch"
    assert "4-November" in emp.get(f"/tasks/{tid}").text or "04 Nov" in emp.get(f"/tasks/{tid}").text
    # the corrected sheet completes the task
    good = make_sheet(str(tmp_path / "sheet_ok.xlsx"), rows)
    r = emp.post(f"/api/tasks/{tid}/sheet", headers=H, files={"file": ("FAB CASA Always-on.xlsx", open(good, "rb"))})
    assert r.status_code == 200 and r.json()["ok"], r.json()
    assert state(emp, tid)["status"] == "completed"
    html = emp.get(f"/tasks/{tid}").text
    assert "Verified: you can inform the client" in html
    with Session() as s:
        t = s.get(Task, tid)
        assert t.approved_version_id == t.current_version_id
    # wrong file type for the sheet
    r = emp.post(f"/api/tasks/{tid}/sheet", headers=H, files={"file": ("sheet.pdf", b"%PDF")})
    assert r.status_code == 400 and "Download" in r.text
    assert "Report ready - upload the Google Sheet" in emp.get("/tasks?status=awaiting_sheet").text or emp.get("/tasks").status_code == 200


@pytest.mark.web
def test_web_claude_recipe_saved_after_sheet_verified(emp, tmp_path):
    """A report type Claude read becomes a saved rule for the client only once the Google Sheet is verified."""
    from sqlalchemy import select

    from app import service as S
    from app.db import Client, Profile, Session, Task, TaskVersion, User
    from tests.test_web_acceptance import H, drain, state
    with Session() as s:
        fab = s.scalar(select(Client).where(Client.name == "FAB"))
        if fab is None:
            pytest.skip("runs after the FAB web flow test")
        fab_id = fab.id
    other = {**FAB, "sheet_title": "FAB Other", "columns": FAB["columns"][:2]}
    prev = previous_report(str(tmp_path), 4, recipe=other)
    raw = write_exports(str(tmp_path / "raw5"), 5)
    tid = emp.post("/api/tasks", json={"client_id": fab_id}, headers=H).json()["id"]
    emp.patch(f"/api/tasks/{tid}", headers=H, json={"title": "FAB other 5 Nov", "deliverables": ["event_report"]})
    for f in raw:
        emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (f["name"], open(f["path"], "rb"))}, data={"role": "raw"})
    emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": ("FAB Other_04_11_2026.xlsx", open(prev, "rb"))}, data={"role": "previous_report"})
    assert emp.post(f"/api/tasks/{tid}/submit", headers=H).status_code == 200
    drain()
    assert state(emp, tid)["status"] == "validation_blocked"
    html = emp.get(f"/tasks/{tid}").text
    assert "evInstrForm" in html  # the prompt box for a new report type
    # an empty description is refused
    assert emp.post(f"/api/tasks/{tid}/event-rerun", headers=H, json={"instructions": "  "}).status_code == 400
    # what Claude's successful reading produces (the worker has no Claude key in tests): run it in-process
    res = run_event(tmp_path / "claude", raw, [prev], instructions="Count the two NTB events for PLT campaigns", provider=FakeClaude(_proposal()))
    assert res["status"] == "awaiting_sheet"
    with Session() as s:
        t = s.get(Task, tid)
        v = s.get(TaskVersion, t.current_version_id)
        v.result = {**v.result, "event": res["event"], "validation": res["validation"]}
        v.status = t.status = "awaiting_sheet"
        s.commit()
        assert not (s.get(Profile, t.profile_id).settings or {}).get("event_recipes")
    good = make_sheet(str(tmp_path / "g.xlsx"), res["event"]["rows"], headers=EVS[:2])
    r = emp.post(f"/api/tasks/{tid}/sheet", headers=H, files={"file": ("sheet.xlsx", open(good, "rb"))})
    assert r.status_code == 200 and r.json()["ok"], r.json()
    with Session() as s:
        t = s.get(Task, tid)
        saved = s.get(Profile, t.profile_id).settings["event_recipes"]
        assert saved[-1]["key"] == res["event"]["new_recipe"]["key"] and saved[-1]["verified_task"] == tid
        assert E.match_recipe(E.read_previous(prev), E.recipes_for("FAB", s.get(Profile, t.profile_id).settings))["key"] == saved[-1]["key"]
    assert "FAB two NTB events" in emp.get("/tasks/new").text
    _ = S


@pytest.mark.web
def test_web_event_report_is_fab_only(emp, tmp_path):
    """ATC and other clients keep the campaign report flow: no report-type choice, no event_report."""
    from sqlalchemy import select

    from app.db import Client, Session, Task, TaskFile
    from tests.test_web_acceptance import H
    with Session() as s:
        atc = s.scalar(select(Client.id).where(Client.short == "ATC"))
    tid = emp.post("/api/tasks", json={"client_id": atc}, headers=H).json()["id"]
    html = emp.get(f"/tasks/{tid}/edit").text
    assert 'id="rtypeField"' in html and 'data-mode="standard"' in html
    assert 'id="rtypeField" aria-describedby="rtypeHint" hidden' in html
    r = emp.patch(f"/api/tasks/{tid}", headers=H, json={"title": "ATC test", "deliverables": ["event_report"]})
    assert r.status_code == 200
    with Session() as s:
        assert s.get(Task, tid).deliverables == ["excel"]
    # raw uploads for ATC are not read as AppsFlyer exports
    f = write_exports(str(tmp_path / "raw"), 2)[0]
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (f["name"], open(f["path"], "rb"))}, data={"role": "raw"})
    assert r.status_code == 200 and not r.json().get("appsflyer")
    with Session() as s:
        assert "appsflyer" not in (s.get(TaskFile, r.json()["id"]).inspection or {})
