"""Upload guidelines: the file-name check (pure) and its place on the New task page."""
from app.naming import check_name, has_period, name_platforms


def test_convention_names_pass():
    for n in ["BWA_Meta_UAE_Oct2026.xlsx", "BWA_TikTok_KSA_Oct2026_clicks.xlsx", "BWA_Snapchat_UAE_Sep-Oct2026.csv",
              "ATC_Google_UAE_2026-10.csv", "meta_acme_sept.csv", "BWA FB+IG KSA October 2026.xlsx"]:
        assert check_name(n, "raw", [{"platform_key": None, "rows": 10}]) == [], n


def test_default_download_name_is_flagged():
    notes = check_name("export (1).csv", "raw")
    assert len(notes) == 1 and "default download name" in notes[0]
    assert "default download name" in check_name("Untitled.xlsx", "raw")[0]


def test_missing_platform_and_period():
    notes = check_name("BWA_UAE_report.xlsx", "raw")
    assert any("platform" in n for n in notes) and any("period" in n for n in notes)


def test_platform_mismatch_with_columns():
    notes = check_name("BWA_TikTok_UAE_Oct2026.xlsx", "raw", [{"platform_key": "meta", "rows": 5}])
    assert notes and "columns look like a Meta export" in notes[0]
    assert check_name("BWA_TikTok_UAE_Oct2026.xlsx", "raw", [{"platform_key": "tiktok", "rows": 5}]) == []


def test_duplicate_and_empty():
    assert any("duplicate" in n for n in check_name("BWA_Meta_UAE_Oct2026 (1).xlsx", "raw"))
    assert any("No data rows" in n for n in check_name("BWA_Meta_UAE_Oct2026.xlsx", "raw", [{"platform_key": "meta", "rows": 0}]))


def test_only_raw_reports_are_checked():
    assert check_name("deck.pptx", "client_deck") == [] and check_name("template.xlsx", "template") == []


def test_helpers():
    assert name_platforms("ATC_snapchat_KSA.csv") == {"snapchat"}
    assert name_platforms("BWAtiktokOct.xlsx") == {"tiktok"}
    assert not has_period("BWA_Meta_UAE.xlsx") and has_period("BWA_Meta_UAE_Q3.xlsx")


def test_sanitised_stored_names_still_flagged():
    # storage.safe_name turns "export (1).csv" into "export _1_.csv"
    assert "default download name" in check_name("export _1_.csv", "raw")[0]
    assert any("duplicate" in n for n in check_name("BWA_Meta_UAE_Oct2026 _2_.xlsx", "raw"))
    assert check_name("BWA_Meta_UAE_Oct2026_1.xlsx", "raw") == []
