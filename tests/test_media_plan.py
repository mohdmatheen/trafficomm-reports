"""Media plan upload: reading booked figures (rules, Claude for new layouts, saved layouts), booked vs
delivered with proration and pacing, and where it shows up (Excel, client deck, validation).

The plan fixture copies the structure of a real client plan (campaign tabs with client details and a
flight line above the table, objective sections with subtotals, merged market/buy-type cells, a Total
row followed by fee/VAT notes, and a summary tab in another currency). Figures are synthetic."""
import datetime as dt
import os
import tempfile
import types

import openpyxl
from pptx import Presentation
from pptx.util import Inches

from app.engine import ai as AI
from app.engine import media_plan as MP
from app.naming import check_name
from tests.helpers import check, fx, run

HDR = ["Channel", "Markets", "Language", "Targeting Approach", "Unit/ Format", "Unit Buy Type ", "Audience Size", "Booked Volume", "Frequency",
       "Estimated Impressions", "Estimated Reach", "Reach", "Estimated VTR", "Estimated Views", "Estimated CTR", "Estimated Clicks",
       "Unit Net Rate\n(AED)", "Total Net to Media\n(AED)", "Total Net to Media\n(USD)", "Allocated Budget\n(AED) — input"]


def _campaign_tab(ws, name, flight, lines):
    ws["C3"], ws["D3"] = "Client Name", "Acme Nutrition"
    ws["C4"], ws["D4"] = "Campaign Name", f"Acme x Racing — {name}"
    ws["C6"], ws["D6"] = "Market/s", "UAE"
    ws["C7"], ws["D7"] = "Flight", flight
    for i, h in enumerate(HDR):
        ws.cell(row=10, column=3 + i, value=h)
    r = 12
    tot = {"imp": 0, "clk": 0, "aed": 0, "views": 0}
    for section, rows in lines:
        ws.cell(row=r, column=3, value=section)
        ws.cell(row=r, column=20, value=sum(x[4] for x in rows))  # section subtotal in the budget column
        r += 1
        for k, (lang, buy, imp, clicks, aed, views) in enumerate(rows):
            vals = ["Meta", "UAE" if k == 0 else None, lang, "Fitness fans" if k == 0 else None, "Reels", buy if k == 0 else None, 1000000, imp if buy == "CPM" else clicks,
                    3, imp, imp / 3, 0.3, 0.04 if views else "n/a", views or "n/a", 0.002, clicks, 4.5, aed, aed / 3.6725, aed]
            for i, v in enumerate(vals):
                ws.cell(row=r, column=3 + i, value=v)
            tot["imp"] += imp
            tot["clk"] += clicks
            tot["aed"] += aed
            tot["views"] += views or 0
            r += 1
        r += 1
    ws.cell(row=r, column=3, value="Total")
    ws.cell(row=r, column=12, value=tot["imp"])
    ws.cell(row=r, column=18, value=tot["clk"])
    ws.cell(row=r, column=16, value=tot["views"])
    ws.cell(row=r, column=20, value=tot["aed"])
    ws.cell(row=r + 2, column=3, value="Notes")
    ws.cell(row=r + 2, column=22, value="Total Net to Media (Excl. VAT & Fees)")
    ws.cell(row=r + 2, column=20, value=tot["aed"])
    ws.cell(row=r + 3, column=3, value="USD 1 = AED 3.6725")
    ws.cell(row=r + 3, column=20, value=tot["aed"] * 0.08)
    return tot


def make_plan(path, c1_lines=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Per Campaign"
    for i, h in enumerate(["Campaign", "Market", "Estimated Impressions", "Estimated Clicks", "Net to Media", "Agency Fee\n8%", "Net to Client \n(Media, Agency Fee & VAT)"]):
        ws.cell(row=5, column=2 + i, value=h)
    ws.cell(row=7, column=2, value="C1 · Launch")
    ws.cell(row=7, column=6, value=999.0)  # USD, header doesn't say so: must not be read
    t1 = _campaign_tab(wb.create_sheet("C1 - Launch"), "C1 Launch", "October 2026 — 4 weeks", c1_lines or [
        ("Engagement", [("English", "CPM", 2_000_000, 3000, 9000, 80000), ("Arabic", "CPM", 500_000, 750, 2250, 20000)]),
        ("Site Visits", [("English", "CPC", 1_000_000, 4000, 6000, 0), ("Arabic", "CPC", 250_000, 1000, 1500, 0)])])
    t2 = _campaign_tab(wb.create_sheet("C2 - Challenge"), "C2 Race Challenge", "15–25 November 2026 — 10 days", [
        ("Engagement", [("English", "CPM", 1_100_000, 1650, 4950, 44000)])])
    wb.save(path)
    return t1, t2


def plan_path(**kw):
    d = tempfile.mkdtemp(prefix="tc_plan_")
    p = os.path.join(d, "ACME_MediaPlan_UAE_2026.xlsx")
    make_plan(p, **kw)
    return p


def meta_export(rows, name="ACME_Meta_UAE_Oct2026.csv"):
    d = tempfile.mkdtemp(prefix="tc_meta_")
    p = os.path.join(d, name)
    hdr = "Campaign name,Ad set name,Ad name,Reporting starts,Reporting ends,Amount spent (AED),Impressions,Reach,Link clicks,3-second video plays\n"
    with open(p, "w") as fh:
        fh.write(hdr + "\n".join(",".join(str(x) for x in r) for r in rows) + "\n")
    return p


OCT = [("Acme_C1_Launch_Engagement_UAE", "EN", "Video", "2026-10-01", "2026-10-31", 10200, 2300000, 900000, 3300, 90000),
       ("Acme_C1_Launch_SiteVisits_UAE", "EN", "Static", "2026-10-01", "2026-10-31", 6900, 1100000, 400000, 4600, 0)]


def test_rules_read_the_campaign_tab_layout():
    p = plan_path()
    res = MP.read_media_plans([{"name": "plan.xlsx", "path": p}], AI.RuleProvider(), {}, {"period_start": dt.date(2026, 10, 1)}, "AED")
    assert res["engines"][0]["engine"] == "rules"
    assert any("Summary" in n for n in res["notes"])  # summary tab not double counted
    lines = res["lines"]
    assert len(lines) == 5 and all(ln["platform"] == "meta" for ln in lines)
    c1 = [ln for ln in lines if ln["sheet"] == "C1 - Launch"]
    assert sum(ln["budget"] for ln in c1) == 18750  # AED net to media, not USD, not the allocated input twice
    assert sum(ln["impressions"] for ln in c1) == 3_750_000
    assert all(ln["market"] == "UAE" for ln in lines)  # merged market cell repeated
    assert [ln["buy_type"] for ln in c1] == ["CPM", "CPM", "CPC", "CPC"]  # merged buy type repeated within a section
    assert {ln["objective"] for ln in c1} == {"Engagement", "Site Visits"}
    assert c1[0]["start"] == dt.date(2026, 10, 1) and c1[0]["end"] == dt.date(2026, 10, 31)
    c2 = [ln for ln in lines if ln["sheet"] == "C2 - Challenge"][0]
    assert (c2["start"], c2["end"]) == (dt.date(2026, 11, 15), dt.date(2026, 11, 25))
    assert c2["reach"] and c2["reach"] > 1  # 'Estimated Reach', not the reach % column
    assert res["total_checks"] and all(c["ok"] for c in res["total_checks"])


def test_flight_parsing():
    assert MP.parse_flight("8 Nov – 5 Dec 2026 — 4 weeks (post live 8 Nov)") == (dt.date(2026, 11, 8), dt.date(2026, 12, 5))
    assert MP.parse_flight("October 2026 — 4 weeks") == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert MP.parse_flight("15–25 November 2026") == (dt.date(2026, 11, 15), dt.date(2026, 11, 25))
    assert MP.parse_flight("01/10/2026 - 31/10/2026") == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert MP.parse_flight("28 Dec - 10 Jan 2027") == (dt.date(2026, 12, 28), dt.date(2027, 1, 10))


def test_booked_vs_delivered_in_excel_and_commentary():
    r = run([(meta_export(OCT), "raw"), (plan_path(), "media_plan")], period=("2026-10-01", "2026-10-31"), answers={"conversion_metric": "none"})
    assert r["status"] == "ready_for_review", r.get("validation")
    cmp = r["media_plan"]["comparison"]
    assert len(cmp["rows"]) == 1  # only C1 runs in October; C2 (November) is outside the period
    row = cmp["rows"][0]
    assert row["budget"]["booked"] == 18750 and row["budget"]["delivered"] == 17100
    assert round(row["budget"]["delivery_pct"], 2) == round(17100 / 18750 * 100, 2)
    assert row["clicks"]["delivered"] == 7900 and row["clicks"]["booked"] == 8750
    assert row["status"] == "Delivered"  # flight over, within 90-110%
    assert check(r, "media_plan")["status"] == "pass" and check(r, "media_plan_totals")["status"] == "pass"
    assert any("media plan" in c["text"] for c in r["commentary"])
    xl = next(a["path"] for a in r["artifacts"] if a["kind"] == "excel_consolidated")
    wb = openpyxl.load_workbook(xl)
    assert "Booked vs Delivered" in wb.sheetnames and "Media Plan" in wb.sheetnames
    ws = wb["Booked vs Delivered"]
    hdr = [c.value for c in ws[4]]
    assert "Booked budget (this period)" in hdr and "% of budget used" in hdr
    vals = [c.value for c in ws[5]]
    assert vals[hdr.index("Spend")] == 17100
    assert ws.title and wb["Media Plan"].max_row >= 9  # 5 lines + header block


def test_proration_and_pacing_mid_flight():
    # a plan line running Oct 1 - Nov 30, reported for October: half the line is booked for the period
    d = tempfile.mkdtemp()
    p = os.path.join(d, "Acme_MediaPlan_UAE_Q4-2026.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Platform", "Start Date", "End Date", "Planned Impressions", "Planned Clicks", "Net Budget (USD)"])
    ws.append(["Facebook & Instagram", dt.date(2026, 10, 1), dt.date(2026, 11, 29), 6_000_000, 10000, 10000])
    ws.append(["Total", None, None, 6_000_000, 10000, 10000])
    wb.save(p)
    rows = [("Acme_Awareness_UAE", "EN", "Video", "2026-10-01", "2026-10-15", 9000, 1_500_000, 600000, 2000, 0)]
    r = run([(meta_export(rows), "raw"), (p, "media_plan")], period=("2026-10-01", "2026-10-31"), answers={"conversion_metric": "none"}, fx_usd_aed=3.6725)
    row = r["media_plan"]["comparison"]["rows"][0]
    share = 31 / 60
    assert abs(row["budget"]["booked"] - 10000 * 3.6725 * share) < 0.01  # USD plan converted to AED at the task rate, then prorated
    assert abs(row["impressions"]["expected"] - 6_000_000 * 15 / 60) < 0.01  # data ends 15 Oct
    assert abs(row["impressions"]["pacing_pct"] - 100.0) < 0.01
    assert row["status"] == "On track"
    assert any("USD" in a for a in r["assumptions"])


def _deck_with_plan_table(path):
    prs = Presentation(fx("acme_client_deck.pptx"))
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Media plan delivery"
    t = s.shapes.add_table(3, 5, Inches(0.5), Inches(1.5), Inches(9), Inches(1.5)).table
    for i, h in enumerate(["Platform", "Booked Impressions", "Delivered Impressions", "% Delivered", "Pacing"]):
        t.cell(0, i).text = h
    for ri, lab in enumerate(["Meta", "Total"], 1):
        t.cell(ri, 0).text = lab
        for c in range(1, 5):
            t.cell(ri, c).text = "0"
    prs.save(path)


def _plan_for_acme(d):
    p = os.path.join(d, "ACME_MediaPlan_UAE_Sep2026.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "All figures in AED"
    ws.append([])
    ws.append(["Platform", "Market", "Planned Impressions", "Planned Clicks", "Net Budget"])
    ws.append(["Facebook & Instagram", "UAE", 2_000_000, 15000, 12000])
    ws.append(["Total", None, 2_000_000, 15000, 12000])
    wb.save(p)
    return p


def _run_deck(deck, plan, **kw):
    ans = {"conversion_metric": "none"}
    for _ in range(3):
        r = run([(fx("meta_acme_sept.csv"), "raw"), (plan, "media_plan"), (deck, "client_deck")], deliverables=("excel", "deck_update"), answers=ans, **kw)
        if r["status"] != "awaiting_clarification":
            return r
        for q in r["questions"]:
            ans[q["key"]] = q.get("default") or q["options"][0]["value"]
    return r


def test_deck_plan_table_is_filled_and_not_touched_by_creative_matching():
    d = tempfile.mkdtemp()
    deck = os.path.join(d, "deck.pptx")
    _deck_with_plan_table(deck)
    r = _run_deck(deck, _plan_for_acme(d))
    assert r["status"] == "ready_for_review"
    out = next(a["path"] for a in r["artifacts"] if a["kind"] == "deck_update")
    prs = Presentation(out)
    assert len(prs.slides) == 8  # no slide added: the deck has its own booked vs delivered table
    tbl = next(sh.table for sh in prs.slides[7].shapes if sh.has_table)
    row = [c.text for c in tbl.rows[1].cells]
    meta = r["media_plan"]["comparison"]["rows"][0]
    assert row[1] == f"{meta['impressions']['booked']:,.0f}" and row[2] == f"{meta['impressions']['delivered']:,.0f}"
    assert row[3] == f"{meta['impressions']['delivery_pct']:.1f}%"
    assert check(r, "deck")["status"] == "pass"  # read-back covers the plan cells


def test_deck_without_plan_table_gets_one_slide_unless_asked_not_to():
    d = tempfile.mkdtemp()
    plan = _plan_for_acme(d)
    r = _run_deck(fx("acme_client_deck.pptx"), plan)
    out = next(a["path"] for a in r["artifacts"] if a["kind"] == "deck_update")
    prs = Presentation(out)
    assert len(prs.slides) == 8
    added = [s for s in prs.slides if any(sh.name == "Booked vs delivered" for sh in s.shapes)]
    assert len(added) == 1 and "Delivered vs booked" in added[0].shapes.title.text
    assert any("added from the media plan" in f["message"] for f in r["deck"]["flags"])
    rp = AI.RuleProvider().interpret_feedback("Please remove the booked vs delivered slide", False)
    assert any(o.op == "plan_slide" and o.value == "0" for o in rp.ops)
    r2 = _run_deck(fx("acme_client_deck.pptx"), plan, plan_overrides={"plan_slide": False})
    out2 = next(a["path"] for a in r2["artifacts"] if a["kind"] == "deck_update")
    assert len(Presentation(out2).slides) == 7


class FakeClaude:
    def __init__(self):
        self.calls = []

    def parse(self, model, max_tokens, system, output_config, messages, output_format):
        self.calls.append(output_format.__name__)
        if output_format is AI.MediaPlanMapOut:
            assert "<untrusted>" in messages[0]["content"]
            out = AI.MediaPlanMapOut(sheets=[AI.PlanSheetOut(sheet_index=0, sheet_name="Plan", header_row=2, last_row=0, currency="AED", columns=[
                AI.PlanColumn(column=1, field="platform"), AI.PlanColumn(column=2, field="impressions"), AI.PlanColumn(column=3, field="budget")])], notes=["fake"])
        elif output_format is AI.Commentary:
            out = AI.Commentary(statements=[])
        else:
            out = AI.PlanOut(**{**AI.Plan().model_dump(), "split_tabs_by": "", "conversion_metric": "", "click_metric": "unspecified"})
        return types.SimpleNamespace(stop_reason="end_turn", parsed_output=out)


def _hybrid():
    p = AI.HybridProvider.__new__(AI.HybridProvider)
    p.model, p.name, p.fallback, p.steps, p.mocked, p.use_claude, p.reason = "claude-test", "Hybrid", AI.RuleProvider(), [], False, False, ""
    fake = FakeClaude()
    p.client = types.SimpleNamespace(messages=types.SimpleNamespace(parse=fake.parse))
    return p, fake


def _odd_plan():
    # headers the built-in rules don't know (another language)
    d = tempfile.mkdtemp()
    p = os.path.join(d, "Acme_MediaPlan_UAE_Sep2026.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Mediaplan Acme"])
    ws.append(["Kanal", "Sichtkontakte", "Betrag"])
    ws.append(["Meta", 1_000_000, 5000])
    ws.append(["Snapchat", 400_000, 2000])
    wb.save(p)
    return p


def test_claude_locates_a_new_layout_and_the_reading_is_reused():
    p = _odd_plan()
    task = {"period_start": dt.date(2026, 9, 1)}
    assert MP.read_media_plans([{"name": "x.xlsx", "path": p}], AI.RuleProvider(), {}, task, "AED")["engines"] == []  # rules alone can't read it
    prov, fake = _hybrid()
    res = MP.read_media_plans([{"name": "x.xlsx", "path": p}], prov, {}, task, "AED")
    assert res["engines"][0]["engine"] == "claude" and fake.calls == ["MediaPlanMapOut"]
    assert [ln["budget"] for ln in res["lines"]] == [5000, 2000]  # figures come from the cells, not from Claude
    assert any(s["step"] == "Media plan" and s["engine"] == "claude" for s in prov.steps)
    saved = res["save_layouts"]
    assert len(saved) == 1
    prov2, fake2 = _hybrid()
    res2 = MP.read_media_plans([{"name": "x.xlsx", "path": p}], prov2, saved, task, "AED")
    assert res2["engines"][0]["engine"] == "saved" and fake2.calls == []
    assert [ln["budget"] for ln in res2["lines"]] == [5000, 2000]


def test_structure_fingerprint_unchanged_without_a_media_plan():
    a = AI.structure_fingerprint([], None, None, ["excel"], "", {})
    b = AI.structure_fingerprint([], None, None, ["excel"], "", {}, [])
    c = AI.structure_fingerprint([], None, None, ["excel"], "", {}, ["abc"])
    assert a == b and a[0] != c[0] and "media plan layout" in c[1]


def test_plan_file_name_check_and_role():
    assert check_name("ON_MediaPlan_UAE_Q4-2026.xlsx", "media_plan") == []
    assert check_name("Acme_Racing_2026_Media_Plan_V6.xlsx", "media_plan") == []
    assert "MediaPlan" in check_name("Acme_UAE_Oct2026.xlsx", "media_plan")[0]
    assert "default download name" in check_name("export (1).xlsx", "media_plan")[0]
    assert "media plan" in check_name("Acme_MediaPlan_UAE_Oct2026.xlsx", "raw")[0]
    from app.engine.inspect import suggest_role
    assert suggest_role("Acme_Media_Plan_V6.xlsx", ".xlsx", []) == "media_plan"
    assert suggest_role("BWA_Meta_UAE_Oct2026.xlsx", ".xlsx", []) == "raw"


def test_deck_plan_columns():
    assert MP.deck_plan_column("Booked Impressions") == ("booked", "impressions")
    assert MP.deck_plan_column("% Delivered") == ("pct", None)
    assert MP.deck_plan_column("Pacing") == ("pacing", None)
    assert MP.deck_plan_column("Impressions") is None and MP.deck_plan_column("Spend") is None
    assert MP.is_plan_header(["Platform", "Booked Impressions", "Delivered Impressions"])
    assert not MP.is_plan_header(["Name", "Impressions", "Reach", "Spends AED"])
