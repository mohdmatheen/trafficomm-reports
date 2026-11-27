"""Local day x event reconciliation (AI Support FAB pilot). Synthetic, privacy-safe AppsFlyer exports only:
device ids are generated strings, campaigns are invented. The 'verified' report is built by an independent
calculation of a hidden rule; the engine must find that rule from the data, on every date, without being told."""
import csv
import datetime as dt
import json
import os

from app.engine import event_reconcile as R
from app.engine import event_report as E

REC = E.FAB_IOS_PLT_ALL_EVENTS
EVS = [c["event"] for c in REC["columns"] if c.get("event")]
HEAD = ["Event Time", "Event Name", "Event Value", "Media Source", "Campaign", "Platform", "AppsFlyer ID", "App ID", "Is Retargeting"]
APP = "id1383237548"


def _row(day, ev, dev, camp, hour=10, media="Facebook Ads", rt="false"):
    t = f"2026-11-{day:02d} {hour:02d}:15:00"
    return [t, ev, json.dumps({"deviceId": dev}), media, camp, "ios", f"af-{dev}", APP, rt]


def _write(folder, rows_by_kind, last_day):
    os.makedirs(folder, exist_ok=True)
    out = []
    for kind, rows in rows_by_kind.items():
        name = f"{APP}_{kind}_2026-11-01_2026-11-{last_day:02d}_Asia_Dubai.csv"
        p = os.path.join(folder, name)
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(HEAD)
            w.writerows(rows)
        out.append({"name": name, "path": p})
    return out


def _data(hidden=None, days=(25, 26)):
    inapp, nonorg, verified = [], [], {}
    for day in days:
        for k in range(40):
            ev, camp, dev = EVS[k % len(EVS)], f"Promo_{k % 4}_PLT", f"D{day}{k:03d}"
            inapp.append(_row(day, ev, dev, camp))
            if k % 3 == 0:
                nonorg.append(_row(day, ev, dev, camp, hour=11))  # duplicate across exports: counted once
            verified[(dt.date(2026, 11, day), ev)] = verified.get((dt.date(2026, 11, day), ev), 0) + 1
        inapp.append(_row(day, EVS[0], f"AO{day}", "Dining_PLT"))  # Always On: excluded by the recipe
        for j in range(day - 14):  # the hidden rule's rows: 11 on the 25th, 12 on the 26th
            rows = hidden(day, j)
            inapp.extend(rows)
    return {"in-app-events": inapp, "non-organic-in-app-events": nonorg}, verified


def test_finds_a_campaign_exclusion_that_holds_on_every_date(tmp_path):
    rows, verified = _data(lambda day, j: [_row(day, EVS[j % len(EVS)], f"T{day}{j}", "Internal_QA_Test")])
    files = _write(str(tmp_path), rows, 26)
    out = R.reconcile(REC, files, verified)
    b = out["baseline"]
    assert [x["portal"] - x["verified"] for x in b["per_date"]] == [11, 12]
    assert out["rule_candidate"]["key"] == "exclude_campaign:Internal_QA_Test"
    assert out["rule_candidate"]["spec"] == {"campaign_match": "exclude", "add_campaign_exclusions": ["Internal_QA_Test"]}
    assert all(c["difference"] > 0 for c in out["mismatched_cells"])
    assert "proposed for approval, not applied" in out["verdict"]
    # aggregates only: no device or AppsFlyer ids in the result
    blob = json.dumps(out, default=str)
    assert "T25" not in blob and "af-" not in blob and "D25" not in blob


def test_finds_retargeting_and_refuses_a_single_date(tmp_path):
    rows, verified = _data(lambda day, j: [_row(day, EVS[j % len(EVS)], f"R{day}{j}", f"Promo_{j % 4}_PLT", rt="true")])
    files = _write(str(tmp_path / "a"), rows, 26)
    out = R.reconcile(REC, files, verified)
    assert out["rule_candidate"]["key"] == "exclude_retargeting"
    one_day = {k: v for k, v in verified.items() if k[0].day == 26}
    rows1, _ = _data(lambda day, j: [_row(day, EVS[j % len(EVS)], f"R{day}{j}", f"Promo_{j % 4}_PLT", rt="true")], days=(26,))
    out1 = R.reconcile(REC, _write(str(tmp_path / "b"), rows1, 26), one_day)
    assert out1["rule_candidate"] is None and "at least 2 dates" in out1["verdict"]


def test_baseline_matches_the_production_counter(tmp_path):
    rows, verified = _data(lambda day, j: [_row(day, EVS[j % len(EVS)], f"T{day}{j}", "Internal_QA_Test")])
    files = _write(str(tmp_path), rows, 26)
    run = E.build_counts(REC, files)
    slots = R.load_rows(REC, files)
    mine = R.count(REC, slots, R.Variant("baseline", ""))
    prod = {(d, e): n for d, c in run["counts"].items() for e, n in c.items()}
    assert {k: v for k, v in mine.items() if v} == {k: v for k, v in prod.items() if v}


def test_no_explanation_when_nothing_fits(tmp_path):
    rows, verified = _data(lambda day, j: [_row(day, EVS[j % len(EVS)], f"Z{day}{j}", f"Promo_{j % 4}_PLT")])
    out = R.reconcile(REC, _write(str(tmp_path), rows, 26), verified)
    assert out["rule_candidate"] is None and "No single candidate" in out["verdict"]
    assert out["baseline"]["total_error"] == 23
