"""Decks that list posts/segments one per row (ZSP and Mayfair failures, Nov 2026): the updater used
to match none of these tables and still returned 'Ready for review' with the template unchanged."""
import datetime as dt
import io

import pandas as pd
import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt

from app.engine.calc import calculate
from app.engine.pptx_inspect import label_to_metric
from app.engine.pptx_update import DeckUpdater
from app.engine.validate import _deck_checks
from app.profiles import ATC_PROFILE

OCT = {"data_start": dt.date(2026, 10, 1), "data_end": dt.date(2026, 10, 31)}


def _png(path, color=(30, 30, 60)):
    Image.new("RGB", (540, 960), color).save(path)
    return path


def _title(slide, text, top=0.3, size=28):
    tb = slide.shapes.add_textbox(Inches(2), Inches(top), Inches(9), Inches(0.8))
    tb.text_frame.text = text
    tb.text_frame.paragraphs[0].runs[0].font.size = Pt(size)
    return tb


def _post_table(slide, rows):
    hdr = ["Name", "Links", "Impressions", "Reach", "Views", "Spends AED"]
    shp = slide.shapes.add_table(len(rows) + 1, len(hdr), Inches(0.5), Inches(1.2), Inches(12), Inches(0.4 * (len(rows) + 1)))
    for c, h in enumerate(hdr):
        shp.table.cell(0, c).text = h
    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row):
            shp.table.cell(r, c).text = v
    return shp


def make_rolling_deck(path, tmp_path):
    """Cover, August section, September section (Facebook table + screenshot grid), closing slide."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.33), Inches(7.5)
    blank = prs.slide_layouts[6]
    _title(prs.slides.add_slide(blank), "CLIENT PRIZE Boosting August-September Report", top=3)
    pic = _png(tmp_path / "old.png", (200, 200, 200))
    for mon, posts in (("August", [("FB-Post-06-Aug_(What if food)", "https://facebook.com/a1", "2,671,161", "1,725,429", "-", "360")]),
                       ("September", [("FB-Post-01-Sep_(Meet our Committee)", "https://facebook.com/s1", "2,862,197", "1,527,869", "114,790", "240"),
                                      ("FB-Post-05-Sep_(Climate Week)", "https://facebook.com/s2", "1,000", "900", "50", "20")])):
        _title(prs.slides.add_slide(blank), f"{mon} Performance", top=3, size=40)
        s = prs.slides.add_slide(blank)
        _title(s, "Facebook Performance")
        _post_table(s, posts)
        g = prs.slides.add_slide(blank)
        _title(g, "Facebook")
        for k in range(3):
            g.shapes.add_picture(str(pic), Inches(1 + 3.5 * k), Inches(1.5), Inches(2.5), Inches(4.5))
    _title(prs.slides.add_slide(blank), "THANK YOU", top=3)
    prs.save(path)
    return path


def oct_dataset(extra_rows=()):
    base = {"_file": "ZSP_Meta_UAE_Oct2026.csv", "_sheet": "CSV", "_excluded": None, "_period": "current", "platform": "meta", "currency": "AED"}
    rows = [
        {**base, "_row": 1, "campaign_name": "BO#1_ZSP_Facebook", "adset_name": "FB_Pro_Interest_ZSP_Post_01-Oct_(Episode 61)",
         "ad_name": "FB_Pro_Interest_ZSP_Post_01-Oct_(Episode 61)_(https://www.facebook.com/share/v/AAA/?x=1)",
         "impressions": 1000.0, "reach": 800.0, "spend": 150.0, "video_plays_3s": 40.0},
        {**base, "_row": 2, "campaign_name": "BO#1_ZSP_Facebook", "adset_name": "FB_Pro_Interest_ZSP_Post-2_05-Oct_(100 days to go)",
         "ad_name": "FB_Pro_Interest_ZSP_Post-2_05-Oct_(100 days to go)_(https://www.facebook.com/share/v/BBB/)",
         "impressions": 500.0, "reach": 450.0, "spend": 149.99, "video_plays_3s": 20.0},
        # a second ad for the same post: summed into one row
        {**base, "_row": 3, "campaign_name": "BO#1_ZSP_Facebook", "adset_name": "FB_Pro_Interest_ZSP_Post-2_05-Oct_(100 days to go)",
         "ad_name": "FB_Pro_Interest_ZSP_Post-2_05-Oct_(100 days to go)_(https://www.facebook.com/share/v/BBB/)",
         "impressions": 100.0, "reach": 90.0, "spend": 10.0, "video_plays_3s": 5.0},
        # a September post with no delivery in October: left out
        {**base, "_row": 4, "campaign_name": "BO#1_ZSP_Facebook", "adset_name": "FB_Pro_Interest_ZSP_Post-21-Sep_(Old)",
         "ad_name": "FB_Pro_Interest_ZSP_Post-21-Sep_(Old)_(https://www.facebook.com/share/v/OLD/)",
         "impressions": 0.0, "reach": 0.0, "spend": 0.0, "video_plays_3s": 0.0},
        # Instagram row of the same campaign family: not on the Facebook slide
        {**base, "_row": 5, "campaign_name": "BO#2_ZSP_Instagram", "adset_name": "IG_Pro_Interest_ZSP_Post_01-Oct_(Episode 61)",
         "ad_name": "IG_Pro_Interest_ZSP_Post_01-Oct_(Episode 61)_(https://www.instagram.com/reel/CCC/)",
         "impressions": 7777.0, "reach": 7000.0, "spend": 99.0, "video_plays_3s": 1.0},
    ] + list(extra_rows)
    df = pd.DataFrame(rows)
    return calculate(df, {"_click_metric": "link_clicks", "breakdowns": []}, {}, "AED")


def _table_rows(prs, title, nth=-1):
    hits = [s for s in prs.slides if any(sh.has_text_frame and sh.text_frame.text == title for sh in s.shapes)]
    t = [sh for sh in hits[nth].shapes if sh.has_table][0].table
    return [[c.text for c in r.cells] for r in t.rows]


def _shots(tmp_path):
    return [{"path": str(_png(tmp_path / n)), "name": n} for n in ("FB-Post-01-Oct__Episode 61_.png", "FB-Post-05-Oct__100 days to go_.png",
                                                             "IG-Post-01-Oct__Episode 61_.png")]


def test_spend_labels_with_currency_are_recognised():
    assert label_to_metric("Spends AED") == "spend"
    assert label_to_metric("Amount Spent (AED)") == "spend"
    assert label_to_metric("3 sec views") == "video_plays_3s"
    assert label_to_metric("Targeting") is None


def test_rolling_deck_adds_new_month_and_drops_oldest(tmp_path):
    src = make_rolling_deck(str(tmp_path / "deck.pptx"), tmp_path)
    up = DeckUpdater(src, oct_dataset(), OCT, ATC_PROFILE, {},
                     {"screenshots": _shots(tmp_path), "instructions": "Remove August completely. Keep September unchanged. Add October."})
    up.match_slides()
    assert not up.questions
    out = str(tmp_path / "out.pptx")
    res = up.run(out)
    prs = Presentation(out)
    texts = [" | ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert "August Performance" not in " ".join(texts)
    assert any("September Performance" in t for t in texts) and any("October Performance" in t for t in texts)
    assert "Boosting September-October Report" in texts[0]
    # September kept exactly as it was
    sep = _table_rows(prs, "Facebook Performance", 0)
    assert sep[1][0] == "FB-Post-01-Sep_(Meet our Committee)" and sep[1][2] == "2,862,197"
    # October rebuilt from the data: one row per post, ads of one post summed, idle posts and Instagram left out
    octr = _table_rows(prs, "Facebook Performance", -1)
    assert [r[0] for r in octr[1:]] == ["FB-Post-01-Oct_(Episode 61)", "FB-Post-05-Oct_(100 days to go)"]
    assert octr[1][1].startswith("https://www.facebook.com/share/v/AAA")
    assert octr[1][2:] == ["1,000", "800", "40", "150.00"]
    assert octr[2][2:] == ["600", "540", "25", "159.99"]
    # screenshots of the platform placed on the new grid slide with a black border
    grid = [s for s in prs.slides if any(sh.has_text_frame and sh.text_frame.text == "Facebook" for sh in s.shapes)][-1]
    pics = [sh for sh in grid.shapes if sh.shape_type == 13]
    assert len(pics) == 2 and all(p.line.width == Pt(1.5) for p in pics)
    chk = {c["id"]: c for c in _deck_checks({**res, "path": out, "na": "-"}, None)}
    assert chk["deck_updated"]["status"] == "pass"
    assert chk["deck"]["status"] == "pass"


def test_rolling_deck_asks_what_to_do_with_oldest_month(tmp_path):
    src = make_rolling_deck(str(tmp_path / "deck.pptx"), tmp_path)
    up = DeckUpdater(src, oct_dataset(), OCT, ATC_PROFILE, {}, {"screenshots": [], "instructions": "Add October data."})
    up.match_slides()
    q = next(q for q in up.questions if q["key"] == "deck_roll")
    assert q["blocking"] and q["default"] == "drop"
    up2 = DeckUpdater(src, oct_dataset(), OCT, ATC_PROFILE, {"deck_roll": "keep"}, {"screenshots": [], "instructions": "Add October data."})
    out = str(tmp_path / "out.pptx")
    res = up2.run(out)
    texts = " ".join(sh.text_frame.text for s in Presentation(out).slides for sh in s.shapes if sh.has_text_frame)
    assert all(f"{m} Performance" in texts for m in ("August", "September", "October"))
    assert any("No screenshots were uploaded" in f["message"] for f in res["flags"])


def test_totals_row_without_names_is_not_a_post(tmp_path):
    src = make_rolling_deck(str(tmp_path / "deck.pptx"), tmp_path)
    total = {"_file": "ZSP_X.xlsx", "_sheet": "Total Reach & Frequency", "_row": 9, "_excluded": None, "_period": "current", "platform": "meta",
             "impressions": 999999.0, "reach": 5.0}
    up = DeckUpdater(src, oct_dataset([total]), OCT, ATC_PROFILE, {"deck_roll": "drop"}, {"screenshots": []})
    out = str(tmp_path / "out.pptx")
    res = up.run(out)
    names = [r[0] for r in _table_rows(Presentation(out), "Facebook Performance", -1)[1:]]
    assert "nan" not in names and len(names) == 2
    assert any("without a campaign, ad set or ad name" in f["message"] for f in res["flags"])


def _segment_deck(path):
    prs = Presentation()
    blank = prs.slide_layouts[6]
    _title(prs.slides.add_slide(blank), "23rd September – 8th October 2026", top=3)
    s = prs.slides.add_slide(blank)
    _title(s, "Performance Overview")
    hdr = ["Targeting", "Reach", "Impressions", "Link Clicks", "3 sec views", "CTR", "Amount Spent (AED)"]
    shp = s.shapes.add_table(3, len(hdr), Inches(0.3), Inches(1.5), Inches(9), Inches(1.5))
    for c, h in enumerate(hdr):
        shp.table.cell(0, c).text = h
    shp.table.cell(1, 0).text = "West Bay"
    shp.table.cell(2, 0).text = "Geo-target Doha"
    prs.save(path)
    return path


def test_unmatched_segment_table_blocks_instead_of_returning_template(tmp_path):
    src = _segment_deck(str(tmp_path / "mayfair.pptx"))
    period = {"data_start": dt.date(2026, 9, 23), "data_end": dt.date(2026, 10, 8)}
    up = DeckUpdater(src, oct_dataset(), period, ATC_PROFILE, {}, {"screenshots": []})
    out = str(tmp_path / "out.pptx")
    res = up.run(out)
    msg = " ".join(f["message"] for f in res["flags"])
    assert "West Bay" in msg and "Geo-target Doha" in msg and "breakdown" in msg
    assert "No slide in the deck for" not in msg  # no per-creative noise for row-table decks
    chk = {c["id"]: c for c in _deck_checks({**res, "path": out, "na": "-"}, None)}
    assert chk["deck_updated"]["status"] == "fail"
    assert "template unchanged" in chk["deck_updated"]["message"]


@pytest.mark.parametrize("text,start,end,want", [
    ("23rd September – 8th October 2026", (2026, 9, 23), (2026, 10, 8), "23rd September – 8th October 2026"),
    ("23rd September – 8th October 2026", (2026, 10, 1), (2026, 10, 31), "1st October – 31st October 2026"),
    ("1st – 30th September 2026", (2026, 10, 1), (2026, 10, 31), "1st – 31st October 2026"),
    ("Report for September 2026", (2026, 10, 1), (2026, 10, 31), "Report for October 2026"),
])
def test_cover_date_range_replaced_whole(tmp_path, text, start, end, want):
    prs = Presentation()
    _title(prs.slides.add_slide(prs.slide_layouts[6]), text, top=3)
    src = str(tmp_path / "c.pptx")
    prs.save(src)
    up = DeckUpdater(src, oct_dataset(), {"data_start": dt.date(*start), "data_end": dt.date(*end)}, ATC_PROFILE, {}, {"screenshots": []})
    out = str(tmp_path / "o.pptx")
    up.run(out)
    got = [sh.text_frame.text for sh in Presentation(out).slides[0].shapes if sh.has_text_frame][0]
    assert got == want


def test_slide_map_has_no_duplicate_entries(tmp_path):
    src = _segment_deck(str(tmp_path / "d.pptx"))
    up = DeckUpdater(src, oct_dataset(), OCT, ATC_PROFILE, {}, {"screenshots": []})
    up.match_slides()  # the pipeline matches first, run() must not match (and flag) again
    res = up.run(str(tmp_path / "o.pptx"))
    slides = [m["slide"] for m in res["slide_map"]]
    assert len(slides) == len(set(slides))
