"""LinkedIn Campaign Manager and X (Twitter) Ads exports: platform detection, column mapping, name check."""
from app.engine import fields as F
from app.engine.inspect import inspect_sheet
from app.engine.parsing import RawSheet
from app.naming import check_name, name_platforms

# LinkedIn Campaign Manager "Campaign performance" CSV: metadata lines, then the header row
LINKEDIN_ROWS = [
    ["Campaign Performance Report (in UTC)"],
    ["Report Start: 10/1/2026, 12:00 AM"],
    ["Report End: 10/31/2026, 11:59 PM"],
    ["Date Generated: 11/2/2026, 9:14 AM"],
    [],
    ["Start Date (in UTC)", "Account Name", "Campaign Group Name", "Campaign Name", "Campaign ID", "Campaign Type",
     "Cost Type", "Currency", "Total Spent", "Impressions", "Clicks", "Click Through Rate", "Average CPM",
     "Average CPC", "Reactions", "Comments", "Shares", "Follows", "Total Social Actions", "Total Engagements",
     "Conversions", "Post-Click Conversions", "View-Through Conversions", "Cost per Conversion", "Leads",
     "Clicks to Landing Page", "Video Views", "Video Completions", "Reach", "Average Frequency"],
    ["10/13/2026", "ZSP", "Awareness", "ZSP_Prize_Awareness", "123", "Sponsored Update", "CPM", "USD", "120.50",
     "40000", "310", "0.78%", "3.01", "0.39", "40", "3", "2", "1", "46", "356", "0", "0", "0", "0", "0", "220",
     "5100", "900", "15000", "2.6"],
    ["10/14/2026", "ZSP", "Awareness", "ZSP_Prize_Video", "124", "Video", "CPV", "USD", "80.00",
     "21000", "150", "0.71%", "3.81", "0.53", "10", "1", "0", "0", "11", "161", "0", "0", "0", "0", "0", "95",
     "7300", "1200", "9000", "2.3"],
]

X_ROWS = [
    ["Time period", "Campaign name", "Campaign ID", "Ad Group name", "Post ID", "Post text", "Funding source name",
     "Currency", "Spend", "Impressions", "Results", "Result type", "Cost per result", "Engagements",
     "Billed engagements", "Link clicks", "Url clicks", "App clicks", "Reposts", "Likes", "Replies", "Follows",
     "Media views", "Video views", "Video completions"],
    ["Oct 01, 2026 - Oct 31, 2026", "ZSP_Prize_Reach", "c1", "UAE 25-45", "p1", "Nominate now", "Card", "USD",
     "300.00", "150000", "120000", "Reach", "2.50", "4200", "4200", "900", "900", "0", "30", "250", "12", "5",
     "60000", "41000", "8000"],
]


def _sheet(rows, name, filename):
    return inspect_sheet(RawSheet(name=name, rows=rows, truncated=False), filename)


def test_linkedin_export_detected_as_linkedin_not_tiktok():
    si = _sheet(LINKEDIN_ROWS, "CSV", "ZSP_linkedin_rawdata_oct1-31.csv")
    assert si["platform"] == "linkedin"
    cols = {c["original"]: c["canonical"] for c in si["columns"]}
    assert cols["Total Spent"] == "spend"
    assert cols["Clicks to Landing Page"] == "link_clicks"   # agency rule: clicks = link clicks
    assert cols["Clicks"] == "clicks_all"
    assert cols["Video Views"] == "video_plays_2s"
    assert cols["Start Date (in UTC)"] == "date"
    assert si["header_row"] == 6


def test_linkedin_detected_from_columns_alone():
    assert _sheet(LINKEDIN_ROWS, "CSV", "report.csv")["platform"] == "linkedin"


def test_x_export_detected():
    assert _sheet(X_ROWS, "results", "ZSP_X_Rawdata_oct-1-31.xlsx")["platform"] == "x"
    assert _sheet(X_ROWS, "results", "campaign_export.xlsx")["platform"] == "x"


def test_tiktok_cost_signature_needs_whole_header():
    plat, _, _ = F.detect_platform(["cost type", "cost per conversion", "conversions"], "", "")
    assert plat != "tiktok"


def test_x_and_twitter_names():
    assert name_platforms("ZSP_X_Rawdata_oct-1-31.xlsx") == {"x"}
    assert name_platforms("ZSP_Twitter_UAE_Oct2026.xlsx") == {"x"}
    assert "x" not in name_platforms("BWA_Max_UAE_Oct2026.xlsx")
    assert check_name("ZSP_X_UAE_Oct2026.xlsx", "raw", [{"platform_key": "x", "rows": 18}]) == []
    assert check_name("ZSP_linkedin_UAE_Oct2026.csv", "raw", [{"platform_key": "linkedin", "rows": 18}]) == []
    msg = check_name("ZSP_UAE_Oct2026.xlsx", "raw")[0]
    assert "LinkedIn or X" in msg


def test_linkedin_and_x_end_to_end(tmp_path):
    import csv

    import openpyxl
    import pytest

    from tests.helpers import qkeys, run

    li = tmp_path / "ZSP_LinkedIn_UAE_Oct2026.csv"
    with open(li, "w", newline="") as fh:
        csv.writer(fh).writerows(LINKEDIN_ROWS)
    xf = tmp_path / "ZSP_X_UAE_Oct2026.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "results"
    for r in X_ROWS:
        ws.append(r)
    wb.save(xf)
    r = run([(str(li), "raw"), (str(xf), "raw")], answers={"conversion_metric": "none"}, fx_usd_aed=3.6725,
            period=("2026-10-01", "2026-10-31"))
    assert r["status"] == "ready_for_review", qkeys(r)
    plats = {row["platform"]: row for row in r["summary"]["tables"]["platform"]["rows"]}
    assert set(plats) == {"linkedin", "x"}
    # USD exports converted to AED at the task rate
    assert plats["linkedin"]["spend"] == pytest.approx(200.5 * 3.6725)
    assert plats["x"]["spend"] == pytest.approx(300 * 3.6725)
    # clicks = link clicks (LinkedIn "Clicks to Landing Page", X "Link clicks"), not all clicks
    assert plats["linkedin"]["clicks"] == 315
    assert plats["x"]["clicks"] == 900
    # views = video views (2 seconds, 50% in view)
    assert plats["linkedin"]["views"] == 12400 and plats["x"]["views"] == 41000
