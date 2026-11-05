"""Agency-wide standing rules (Nov 2026) on SYNTHETIC fixtures: USD->AED for Snapchat/TikTok,
per-platform Views, link clicks only, client-owned slides untouched, screenshots with a black border."""
import csv
import io
import os
import zipfile

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from app.engine import rules as R
from tests.helpers import fx, qkeys, run


def _png(path, size=(400, 700), color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path, "PNG")


def _snap_csv(path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Campaign Name", "Ad Squad Name", "Ad Name", "Day", "Spend", "Paid Impressions", "Swipe Ups", "Video Views"])
        w.writerow(["ACME_UAE_SC", "SQ", "ACME_UAE_EN_SC_Story", "2026-09-03", "100.00", "50000", "400", "20000"])
        w.writerow(["ACME_UAE_SC", "SQ", "ACME_UAE_EN_SC_Story", "2026-09-12", "200.00", "70000", "600", "30000"])


def test_views_clicks_and_usd_rules(tmp_path):
    snap = str(tmp_path / "snapchat_sept.csv")
    _snap_csv(snap)
    r = run([(fx("meta_acme_sept.csv"), "raw"), (snap, "raw")], answers={"conversion_metric": "none"}, fx_usd_aed=3.6725)
    assert r["status"] == "ready_for_review", qkeys(r)
    plats = {row["platform"]: row for row in r["summary"]["tables"]["platform"]["rows"]}
    # Snapchat has no currency column -> read as USD and converted at the task rate
    assert plats["snapchat"]["spend"] == pytest.approx(300 * 3.6725)
    assert any("Snapchat values read as USD" in a for a in r["assumptions"])
    # Views: Meta 3-second plays + Snapchat 2-second views
    t = r["summary"]["totals"]
    meta_3s = sum(row.get("video_plays_3s") or 0 for row in [plats["meta"]])
    assert t["views"] == pytest.approx(meta_3s + 50000)
    # Clicks: link clicks (Snapchat swipe ups)
    assert plats["snapchat"]["clicks"] == 1000


def test_client_slides_untouched_and_screenshots_bordered(tmp_path):
    src = Presentation(fx("acme_client_deck.pptx"))
    # slide 3: add the client marker to a metric slide - it must be left exactly as it is
    s3 = src.slides[2]
    s3.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(0.5)).text_frame.text = "Update to Kyra"
    before_s3 = [[c.text for c in row.cells] for row in next(sh for sh in s3.shapes if sh.has_table).table.rows]
    # creative slides 2 and 4 get a placeholder picture
    ph = str(tmp_path / "ph.png")
    _png(ph, (300, 300), (240, 240, 240))
    for i in (1, 3):
        src.slides[i].shapes.add_picture(ph, Inches(9), Inches(3.6), Inches(3), Inches(3))
    deck = str(tmp_path / "deck.pptx")
    src.save(deck)
    # screenshots: one by creative name inside a ZIP, one by slide number as a plain image
    a = str(tmp_path / "ACME_UAE_EN_Video_Ramadan.png")
    _png(a)
    zp = str(tmp_path / "shots.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "ACME_UAE_EN_Video_Ramadan.png")
    b = str(tmp_path / "slide 4.png")
    _png(b, (700, 400), (30, 30, 200))
    r = run([(fx("meta_acme_sept.csv"), "raw"), (deck, "client_deck"), (zp, "screenshots"), (b, "screenshots")],
            deliverables=("excel", "deck_update"), answers={"conversion_metric": "leads_meta_form"}, workdir=str(tmp_path / "w"))
    assert r["status"] == "ready_for_review", qkeys(r)
    out = Presentation(next(x["path"] for x in r["artifacts"] if x["kind"] == "deck_update"))
    o3 = out.slides[2]
    assert [[c.text for c in row.cells] for row in next(sh for sh in o3.shapes if sh.has_table).table.rows] == before_s3
    assert next(m for m in r["deck"]["slide_map"] if m["slide"] == 3)["status"] == "client content - preserved"
    shots = [c for c in r["deck"]["changes"] if c["metric"] == "screenshot"]
    assert {c["slide"] for c in shots} == {2, 4}
    for i, (w, h) in ((1, (400, 700)), (3, (700, 400))):
        pics = [sh for sh in out.slides[i].shapes if sh.shape_type == 13]
        assert len(pics) == 1
        p = pics[0]
        assert p.line.color.rgb == (0, 0, 0) and p.line.width.pt == R.SCREENSHOT_BORDER_PT
        assert abs(p.width / p.height - w / h) < 0.01  # aspect ratio kept, fitted in the old box
        assert p.width <= Inches(3) + 1 and p.height <= Inches(3) + 1
        img = Image.open(io.BytesIO(p.image.blob))
        assert img.size == (w, h)


def test_client_marker_variants():
    for t in ["Update to Kyra", "KYRA TO UPDATE", "Figures to be shared by client", "to be shared by the client"]:
        assert R.is_client_slide_text(t)
    assert not R.is_client_slide_text("Updated figures for Kyra Media")


def test_screenshot_upload_validation(tmp_path):
    from app.engine.inspect import inspect_file
    good = str(tmp_path / "a.png")
    _png(good)
    assert inspect_file(good, "a.png")["role_suggested"] == "screenshots"
    bad = str(tmp_path / "b.png")
    open(bad, "wb").write(b"not an image")
    assert inspect_file(bad, "b.png")["ok"] is False


def test_tiktok_views_are_2_second_plays(tmp_path):
    import openpyxl
    p = str(tmp_path / "tiktok_sept.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Campaign name", "Ad group name", "Ad name", "By Day", "Cost", "Impressions", "Clicks (destination)", "2-second video views", "Video views at 6s"])
    ws.append(["TT", "AG", "ACME_TT_Video", "2026-09-05", 100.0, 40000, 300, 12000, 5000])
    ws.append(["TT", "AG", "ACME_TT_Video", "2026-09-06", 50.0, 20000, 100, 6000, 2500])
    wb.save(p)
    r = run([(p, "raw")], answers={"conversion_metric": "none"})
    assert r["status"] == "ready_for_review", qkeys(r)
    t = r["summary"]["totals"]
    assert t["views"] == 18000  # 2-second plays, not 6-second views
    assert t["spend"] == pytest.approx(150 * 3.6725)  # TikTok USD -> AED


def test_insights_use_professional_language():
    from app.engine.ai import validate_commentary
    facts = {"a": {"value": 1.2}}
    stmts = [{"type": "observation", "text": "Static ads were cheap while video was expensive!", "facts": []},
             {"type": "observation", "text": "Snapchat had the cheapest CPC; a huge result, not a flop.", "facts": []}]
    kept, dropped = validate_commentary(stmts, facts, [])
    text = " ".join(k["text"] for k in kept)
    assert not R.unprofessional_terms(text) and "!" not in text
    assert "cost-efficient" in text and "higher-cost" in text and "lowest CPC" in text
