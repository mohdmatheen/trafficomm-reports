"""Platform detection on real-shaped exports for every supported platform.

Every platform is checked three ways: with a convention file name, with a neutral name (columns only),
and with the kind of name employees actually use (e.g. "Campaign report _40__Google_rawdata_oct1-31.csv").
These guard against the two regressions seen in Nov 2026: generic headers ("Cost", "Conversions") made a
LinkedIn export and then a Google export look like TikTok.
"""
import pytest

from app.engine import fields as F
from app.engine.inspect import inspect_sheet
from app.engine.parsing import RawSheet
from app.naming import check_name
from tests.test_linkedin_x import LINKEDIN_ROWS, X_ROWS

GOOGLE_ROWS = [  # Google Ads > Campaigns > Download (CSV): report name and date range above the header
    ["Campaign report"],
    ["October 1, 2026 - October 31, 2026"],
    ["Campaign status", "Campaign", "Budget", "Budget type", "Status", "Optimization score", "Account",
     "Campaign type", "Impr.", "Interactions", "Interaction rate", "Avg. cost", "Cost", "Clicks", "Conv. rate",
     "Conversions", "Cost / conv.", "Currency code", "Avg. CPC", "Bid strategy type"],
    ["Enabled", "ZSP_Search_Brand", "100.00", "Daily", "Eligible", "88%", "ZSP", "Search", "12000", "900", "7.5%",
     "2.10", "1890.00", "900", "3.1%", "28", "67.50", "AED", "2.10", "Maximize clicks"],
    ["Enabled", "ZSP_PMax", "200.00", "Daily", "Eligible", "91%", "ZSP", "Performance Max", "88000", "1500", "1.7%",
     "1.40", "2100.00", "1500", "1.2%", "18", "116.67", "AED", "1.40", "Maximize conversions"],
]
# the same Google export without the metadata rows and fewer columns: only Cost / Conversions-style headers
GOOGLE_MIN_ROWS = [
    ["Campaign", "Impr.", "Clicks", "Cost", "Conversions", "Currency code"],
    ["ZSP_Search_Brand", "12000", "900", "1890.00", "28", "AED"],
]
META_ROWS = [
    ["Reporting starts", "Reporting ends", "Campaign name", "Ad set name", "Ad name", "Amount spent (AED)",
     "Impressions", "Reach", "Frequency", "Link clicks", "CPC (cost per link click)", "CTR (link click-through rate)",
     "3-second video plays", "ThruPlays", "Results", "Result indicator", "Cost per results"],
    ["2026-10-01", "2026-10-31", "BWA_UAE_Awareness", "UAE 18-45", "BWA_Video_EN", "1500.00", "250000", "90000",
     "2.78", "1800", "0.83", "0.72", "60000", "21000", "90000", "reach", "16.67"],
]
TIKTOK_ROWS = [
    ["By Day", "Campaign name", "Ad group name", "Ad name", "Cost", "Impressions", "Reach", "Clicks (destination)",
     "CTR (destination)", "CPC (destination)", "Video views", "2-second video views", "6-second video views",
     "Conversions", "Currency"],
    ["2026-10-01", "BWA_TT_UAE", "UAE 18-34", "BWA_TT_Video", "120.00", "80000", "40000", "600", "0.75%", "0.20",
     "30000", "18000", "9000", "0", "USD"],
]
TIKTOK_MIN_ROWS = [  # a real BWA TikTok export: only one TikTok-only header
    ["Campaign name", "Spend", "Impressions", "Reach", "Clicks (destination)", "Currency"],
    ["BWA_TT_UAE", "120.00", "80000", "40000", "600", "USD"],
]
SNAPCHAT_ROWS = [
    ["Campaign Name", "Ad Squad Name", "Ad Name", "Day", "Spend", "Paid Impressions", "Swipe Ups", "Video Views",
     "Story Opens", "Paid Reach", "eCPM", "eCPSU"],
    ["BWA_SC_UAE", "UAE 18-34", "BWA_SC_Story", "2026-10-01", "100.00", "50000", "400", "20000", "0", "30000",
     "2.00", "0.25"],
]

CASES = {
    "google_ads": (GOOGLE_ROWS, "ZSP_Google_UAE_Oct2026.csv", "Campaign report _40__Google_rawdata_oct1-31.csv"),
    "meta": (META_ROWS, "BWA_Meta_UAE_Oct2026.csv", "BWA FB+IG KSA October 2026.csv"),
    "tiktok": (TIKTOK_ROWS, "BWA_TikTok_UAE_Oct2026.xlsx", "ATC_ Zayed Sustainability Prize -Campaign Report-2026-10-01 to 2026-10-31_tiktok.xlsx"),
    "snapchat": (SNAPCHAT_ROWS, "BWA_Snapchat_UAE_Oct2026.csv", "snap_rawdata_oct.csv"),
    "linkedin": (LINKEDIN_ROWS, "ZSP_LinkedIn_UAE_Oct2026.csv", "ZSP_linkedin_rawdata_oct1-31.csv"),
    "x": (X_ROWS, "ZSP_X_UAE_Oct2026.xlsx", "ZSP_X_Rawdata_oct-1-31.xlsx"),
}


def _detect(rows, filename):
    return inspect_sheet(RawSheet(name="CSV", rows=rows, truncated=False), filename)


@pytest.mark.parametrize("platform", list(CASES))
def test_convention_name(platform):
    rows, name, _ = CASES[platform]
    si = _detect(rows, name)
    assert si["platform"] == platform
    assert check_name(name, "raw", [{"platform_key": si["platform"], "rows": 5}]) == []


@pytest.mark.parametrize("platform", list(CASES))
def test_employee_style_name(platform):
    rows, _, name = CASES[platform]
    si = _detect(rows, name)
    assert si["platform"] == platform
    assert not any("columns look like" in n for n in check_name(name, "raw", [{"platform_key": si["platform"], "rows": 5}]))


@pytest.mark.parametrize("platform", list(CASES))
def test_columns_alone(platform):
    rows, _, _ = CASES[platform]
    assert _detect(rows, "export_oct.csv")["platform"] == platform


def test_name_wins_when_columns_are_generic():
    # Cost + Conversions used to be TikTok "signatures": a Google file must stay Google
    assert _detect(GOOGLE_MIN_ROWS, "Google_rawdata_oct1-31.csv")["platform"] == "google_ads"
    assert _detect(TIKTOK_MIN_ROWS, "BWA_TikTok_UAE_Oct2026.xlsx")["platform"] == "tiktok"


def test_generic_columns_without_a_name_ask_the_employee():
    plat, _, _ = F.detect_platform(["campaign", "cost", "conversions", "impressions", "clicks"], "export.csv", "")
    assert plat == "unknown"


def test_wrong_file_is_still_flagged():
    # name says TikTok but every column is Meta's: columns win and the upload check warns
    si = _detect(META_ROWS, "BWA_TikTok_UAE_Oct2026.csv")
    assert si["platform"] == "meta"
    notes = check_name("BWA_TikTok_UAE_Oct2026.csv", "raw", [{"platform_key": "meta", "rows": 5}])
    assert notes and "columns look like a Meta export" in notes[0]


def test_no_generic_header_is_a_signature():
    generic = {"cost", "spend", "conversions", "clicks", "impressions", "reach", "frequency", "ctr", "cpc", "cpm",
               "campaign", "campaign name", "ad name", "currency", "date", "day", "video views", "results", "leads"}
    for p, sigs in F.PLATFORM_SIGNATURES.items():
        assert not generic & {s.rstrip("*") for s in sigs}, p
    # and no header is a signature of two platforms
    seen = {}
    for p, sigs in F.PLATFORM_SIGNATURES.items():
        for s in sigs:
            assert s not in seen, f"'{s}' is a signature of both {seen.get(s)} and {p}"
            seen[s] = p
