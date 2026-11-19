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
