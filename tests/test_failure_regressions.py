"""Regressions from a real task that failed in production (Nov 2026, Optimum Nutrition monthly report):

* a multi-month client template (one tab per month, UAE/KSA blocks with merged title rows and TOTAL rows)
  crashed the template filler ("'MergedCell' object attribute 'value' is read-only") and would otherwise
  have overwritten every month's history with the same figures;
* TikTok exports with a wrong <dimension ref="A1"/> were read as empty, silently dropping TikTok;
* two TikTok exports of the same campaigns with different columns were treated as conflicting re-exports;
* Snapchat/Google 'Clicks' confirmed as link clicks were still shown as N/A;
* the task only showed "Processing hit an unexpected error after 3 attempts" with no real reason.

All fixtures are SYNTHETIC (built here), shaped like the real files."""
import csv
import shutil
import zipfile

import openpyxl
import pytest
from openpyxl.styles import Font

from app import jobs
from app.engine.parsing import read_xlsx
from app.run_job import describe_exception
from tests.helpers import check, fx, qkeys, run

UAE_SPEND = 4200.50 + 3100.00
KSA_SPEND = 2650.25 + 1800.00


def _block(ws, top, title, rows):
    """Title row merged across A:J, header, platform rows, TOTAL row with SUM formulas (like the client's tabs)."""
    ws.cell(row=top, column=1, value=title).font = Font(bold=True)
    ws.merge_cells(start_row=top, start_column=1, end_row=top, end_column=10)
    ws.append([])
    hr = top + 1
    for c, h in enumerate(["Platform", "Amount Spent", "Impressions", "Reach", "Views", "Clicks", "Engagement", "CPM", "CPV", "CPE"], 1):
        ws.cell(row=hr, column=c, value=h)
    for i, (plat, vals) in enumerate(rows):
        r = hr + 1 + i
        ws.cell(row=r, column=1, value=plat)
        for c, v in enumerate(vals, 2):
            ws.cell(row=r, column=c, value=v)
        ws.cell(row=r, column=8, value=f"=(B{r}/C{r})*1000")
        ws.cell(row=r, column=9, value=f"=B{r}/E{r}")
        ws.cell(row=r, column=10, value=f"=B{r}/G{r}")
    tr = hr + 1 + len(rows)
    ws.cell(row=tr, column=1, value="TOTAL")
    for c in range(2, 8):
        col = openpyxl.utils.get_column_letter(c)
        ws.cell(row=tr, column=c, value=f"=SUM({col}{hr + 1}:{col}{tr - 1})")
    return tr


def _monthly_template(path):
    hist = [("Meta", [111.0, 1000, 500, 300, 40, 20]), ("Tiktok", [222.0, 2000, 900, 700, 60, 10]), ("Snapchat", ["N/A"] * 6)]
    wb = openpyxl.Workbook()
    ytd = wb.active
    ytd.title = "YTD"
    ytd["A1"] = "UAE YTD"
    ytd.merge_cells("A1:G1")
    ytd.append(["Month", "Amount Spent", "Impressions", "Reach", "Views", "Clicks", "Engagement"])
    ytd.append(["Aug - UAE", 999.0, 9999, 999, 99, 9, 9])
    ytd.append(["TOTAL YTD", "=SUM(B3:B3)", "=SUM(C3:C3)", "=SUM(D3:D3)", "=SUM(E3:E3)", "=SUM(F3:F3)", "=SUM(G3:G3)"])
    for name, month in (("Aug Data", "Aug"), ("Sep Data", "Sep")):
        ws = wb.create_sheet(name)
        end = _block(ws, 3, f"{month} - UAE", hist)
        _block(ws, end + 3, f"{month} - KSA", hist)
    wb.save(path)


def _snapshot(path):
    wb = openpyxl.load_workbook(path)
    return {ws.title: {c.coordinate: c.value for row in ws.iter_rows() for c in row if c.value is not None} for ws in wb.worksheets}


def test_multi_month_market_template_fills_only_the_matching_block(tmp_path):
    tpl = str(tmp_path / "Monthly Media Data_Template.xlsx")
    _monthly_template(tpl)
    before = _snapshot(tpl)
    r = run([(fx("meta_acme_sept.csv"), "raw"), (tpl, "template")], answers={"conversion_metric": "none"}, workdir=str(tmp_path / "w"))
    assert r["status"] == "ready_for_review", (qkeys(r), r.get("error"))
    out = next(a["path"] for a in r["artifacts"] if a["kind"] == "excel")
    after = _snapshot(out)
    # other months and the YTD history are kept exactly as supplied
    assert after["Aug Data"] == before["Aug Data"]
    assert after["YTD"] == before["YTD"]
    sep = openpyxl.load_workbook(out)["Sep Data"]
    # merged title rows and TOTAL rows untouched; formulas kept
    assert sep["A3"].value == "Sep - UAE" and sep["A8"].value == "TOTAL" and sep["B8"].value == "=SUM(B5:B7)"
    # the UAE block gets UAE rows only, the KSA block KSA rows only (market read from campaign names)
    assert sep["A5"].value == "Meta" and sep["B5"].value == pytest.approx(UAE_SPEND)
    ksa_meta = next(rr for rr in range(9, sep.max_row + 1) if sep.cell(rr, 1).value == "Meta")
    assert sep.cell(ksa_meta, 2).value == pytest.approx(KSA_SPEND)
    # platforms with no data keep their template values; columns with no engine metric stay as they were
    assert sep["B6"].value == 222.0 and sep["G5"].value == 20
    t = check(r, "template")
    assert t["status"] == "pass", t
    assert any("other months were kept exactly as supplied" in w for w in r["warnings"])


def test_template_failure_does_not_fail_the_report(tmp_path, monkeypatch):
    import app.pipeline as P

    def boom(*a, **k):
        raise AttributeError("'MergedCell' object attribute 'value' is read-only")
    monkeypatch.setattr(P, "fill_template", boom)
    r = run([(fx("meta_acme_sept.csv"), "raw"), (fx("acme_template.xlsx"), "template")], answers={"conversion_metric": "none"},
            workdir=str(tmp_path / "w"))
    assert r["status"] == "ready_for_review"
    assert [a["kind"] for a in r["artifacts"] if a["kind"].startswith("excel")] == ["excel_consolidated"]
    assert any("could not be filled" in w and "MergedCell" in w for w in r["warnings"])
    assert check(r, "template")["status"] == "warn"


def _tiktok_xlsx(path, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    # TikTok Ads Manager writes <dimension ref="A1"/> although the sheet has many rows
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for it in src.infolist():
            data = src.read(it.filename)
            if it.filename.startswith("xl/worksheets/sheet"):
                import re
                data = re.sub(rb'<dimension ref="[^"]+"\s*/>', b'<dimension ref="A1"/>', data)
            dst.writestr(it, data)
    shutil.move(tmp, path)


TT_ROWS = [["ACME_UAE_TT_Video", "100.00", 50000, 20000, 900, "USD"], ["ACME_KSA_TT_Video", "50.00", 30000, 10000, 400, "USD"]]


def test_xlsx_with_wrong_dimension_is_read_fully(tmp_path):
    p = str(tmp_path / "Campaign Report_TIKTOK.xlsx")
    _tiktok_xlsx(p, ["Campaign name", "Spend", "Impressions", "Reach", "Clicks (destination)", "Currency"], TT_ROWS)
    sheets = read_xlsx(p)
    assert len(sheets[0].rows) == 3 and sheets[0].rows[1][0] == "ACME_UAE_TT_Video"


def test_complementary_tiktok_exports_are_combined_not_double_counted(tmp_path):
    a = str(tmp_path / "Campaign Report_TIKTOK.xlsx")
    b = str(tmp_path / "Campaign Report_tiktok with 2 sec video views.xlsx")
    _tiktok_xlsx(a, ["Campaign name", "Spend", "Impressions", "Reach", "Clicks (destination)", "Currency"], TT_ROWS)
    _tiktok_xlsx(b, ["Campaign name", "Spend", "Impressions", "Reach", "2-second video views", "Currency"],
                 [[r[0], r[1], r[2], r[3], v, r[5]] for r, v in zip(TT_ROWS, (7000, 3000))])
    r = run([(a, "raw"), (b, "raw")], answers={"conversion_metric": "none"}, fx_usd_aed=3.6725, workdir=str(tmp_path / "w"))
    assert r["status"] == "ready_for_review", qkeys(r)
    t = r["summary"]["totals"]
    assert t["spend"] == pytest.approx(150 * 3.6725)  # counted once
    assert t["clicks"] == 1300 and t["views"] == 10000  # link clicks from one file, 2-second views from the other
    assert any(o["type"] == "complementary_rows" for o in r["overlaps"])


def test_confirmed_snapchat_clicks_count_as_link_clicks(tmp_path):
    p = str(tmp_path / "snap.csv")
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Campaign name", "Amount spent", "Clicks", "Paid impressions", "Currency"])
        w.writerow(["acme_ksa_snap_aw", "100.00", "75", "40000", "AED"])
    r = run([(p, "raw")], answers={"conversion_metric": "none", "clicks_def:snapchat": "link_clicks"}, workdir=str(tmp_path / "w"))
    assert r["status"] == "ready_for_review", qkeys(r)
    assert r["summary"]["totals"]["clicks"] == 75


def test_crash_diagnostics_show_the_real_error():
    try:
        openpyxl.Workbook().active.merge_cells("A1:B1")
        raise AttributeError("'MergedCell' object attribute 'value' is read-only")
    except AttributeError as e:
        d = describe_exception(e)
    assert d.startswith("AttributeError: 'MergedCell' object attribute 'value' is read-only (at ")
    out = "Traceback (most recent call last):\n  ...\nAttributeError: boom\nCRASH: AttributeError: boom (at template_fill.py:115)\n"
    assert jobs._crash_detail(out, jobs.EXIT_FAIL) == "AttributeError: boom (at template_fill.py:115)"
    assert "SIGKILL" in jobs._crash_detail("", -9) and "memory" in jobs._crash_detail("", -9)
    assert "time limit" in jobs._crash_detail("", "timeout")
