"""Instructions schema sent to Claude, and X-style account summary tabs."""
import openpyxl
from anthropic.lib._parse._transform import transform_schema

from app.engine.ai import AnthropicProvider, PlanOut
from app.engine.inspect import inspect_file


def test_instructions_schema_has_no_optional_or_nullable_fields():
    schema = transform_schema(PlanOut)
    assert set(schema["required"]) == set(schema["properties"])
    assert "anyOf" not in str(schema)


def test_planout_maps_blanks_to_none():
    out = PlanOut(breakdowns=[["country"]], split_tabs_by=" ", click_metric="unspecified", conversion_metric="",
                  include_commentary=True, include_recommendations=False, short_summary=False, compare=False,
                  template_only=True, replace_thumbnails=False, add_missing_slides=False, remove_sections=[],
                  deck_notes=[], notes=["x"])
    p = out.to_plan()
    assert p.split_tabs_by is None and p.click_metric is None and p.conversion_metric is None
    assert p.template_only and p.breakdowns == [["country"]]


def test_interpret_instructions_uses_planout(monkeypatch):
    p = AnthropicProvider.__new__(AnthropicProvider)
    seen = {}

    def fake_parse(schema, user, step="request", **kw):
        seen["schema"] = schema
        return PlanOut(breakdowns=[], split_tabs_by="country", click_metric="link_clicks", conversion_metric="",
                       include_commentary=True, include_recommendations=True, short_summary=False, compare=False,
                       template_only=False, replace_thumbnails=False, add_missing_slides=False, remove_sections=[],
                       deck_notes=[], notes=[])
    p._parse = fake_parse
    plan = p.interpret_instructions("split by country", ["deck"], ["Campaign name"])
    assert seen["schema"] is PlanOut
    assert plan.split_tabs_by == "country" and plan.click_metric == "link_clicks"
    assert not any("fallback" in n for n in plan.notes)


def test_x_total_reach_sheet_not_counted(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append(["Campaign name", "Ad name", "Impressions", "Spend", "Reach"])
    ws.append(["C1", "Promoted post 1", 1000, 10.5, 800])
    ws.append(["C1", "Promoted post 2", 2000, 20.0, 1500])
    s2 = wb.create_sheet("Total Reach & Frequency")
    s2.append(["Impressions", "Reach", "Frequency"])
    s2.append([3000, 2100, 1.43])
    f = tmp_path / "ZSP_X_UAE_Oct2026.xlsx"
    wb.save(f)
    info = inspect_file(str(f), f.name)
    sheets = {s["name"]: s for s in info["sheets"]}
    assert sheets["results"]["is_data_sheet"]
    assert not sheets["Total Reach & Frequency"]["is_data_sheet"]
    assert sheets["Total Reach & Frequency"].get("summary_sheet")


def test_single_keyless_sheet_still_counts(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Impressions", "Reach", "Spend"])
    ws.append([3000, 2100, 15])
    f = tmp_path / "BWA_Meta_UAE_Oct2026.xlsx"
    wb.save(f)
    info = inspect_file(str(f), f.name)
    assert info["sheets"][0]["is_data_sheet"]


def test_google_youtube_views_and_unique_users(tmp_path):
    import csv

    import pytest

    from tests.helpers import qkeys, run
    rows = [["Campaign report"], ["October 1, 2026 - October 31, 2026"],
            ["Campaign", "Currency", "Cost", "Clicks", "Impr.", "TrueView views", "Unique users", "Campaign type"],
            ["ZSP_Post-2_05-Oct_(100 days)_(https://youtube.com/shorts/abc)", "AED", "197.94", "400", "52,661", "18,250", "40,100", "Video"],
            ["ZSP_Post_30-Oct_(Voices)_(https://youtu.be/def)", "AED", "115.66", "179", "58,530", "21,004", "47,900", "Video"]]
    f = tmp_path / "ZSP_Google_UAE_Oct2026.csv"
    with open(f, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    r = run([(str(f), "raw")], answers={"conversion_metric": "none", "clicks_def:google_ads": "link_clicks"}, period=("2026-10-01", "2026-10-31"))
    assert r["status"] == "ready_for_review", qkeys(r)
    g = r["summary"]["tables"]["platform"]["rows"][0]
    assert g["views"] == 18250 + 21004
    assert g["reach"] == pytest.approx(40100 + 47900)
