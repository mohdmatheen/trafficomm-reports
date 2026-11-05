"""Generate SYNTHETIC test fixtures (fictional brand "Acme Dates"). Nothing here is client data.

    python -m tests.fixtures.make_fixtures [out_dir]

Kept separate from real-client samples on purpose: tests that use these files are labelled
"synthetic" and never presented as real-client validation.
"""
from __future__ import annotations

import csv
import os
import sys

import openpyxl
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

OUT = os.path.join(os.path.dirname(__file__), "synthetic")
FIX_DIR_READY = OUT

META_HDR = ["Campaign name", "Ad set name", "Ad name", "Reporting starts", "Reporting ends", "Amount spent (AED)", "Impressions", "Reach",
            "Link clicks", "Clicks (all)", "Results", "Result indicator", "3-second video plays", "CTR (link click-through rate)"]
# (campaign, adset, ad, spend, impr, reach, link, all, results, indicator, plays)
META_SEPT = [
    ("ACME_UAE_Awareness", "UAE_EN_Broad", "ACME_UAE_EN_Video_Ramadan", 4200.50, 820000, 410000, 6100, 9800, 410000, "reach", 152000),
    ("ACME_UAE_Leads", "UAE_EN_Interest", "ACME_UAE_EN_Static_Offer", 3100.00, 250000, 120000, 3900, 5200, 64, "actions:onsite_conversion.lead_grouped", 0),
    ("ACME_KSA_Leads", "KSA_AR_Interest", "ACME_KSA_AR_Static_Offer", 2650.25, 310000, 150000, 2800, 4100, 51, "actions:onsite_conversion.lead_grouped", 0),
    ("ACME_KSA_Awareness", "KSA_AR_Broad", "ACME_KSA_AR_Video_Brand", 1800.00, 400000, 260000, 1500, 2600, 260000, "reach", 98000),
    ("ACME_UAE_Leads", "UAE_EN_Interest", "ACME_UAE_EN_Static_Paused", 0, 0, 0, 0, 0, 0, "actions:onsite_conversion.lead_grouped", 0),
]
META_AUG = [
    ("ACME_UAE_Awareness", "UAE_EN_Broad", "ACME_UAE_EN_Video_Ramadan", 3900.00, 760000, 390000, 5200, 8800, 390000, "reach", 140000),
    ("ACME_UAE_Leads", "UAE_EN_Interest", "ACME_UAE_EN_Static_Offer", 2800.00, 230000, 110000, 3300, 4700, 55, "actions:onsite_conversion.lead_grouped", 0),
    ("ACME_KSA_Leads", "KSA_AR_Interest", "ACME_KSA_AR_Static_Offer", 2400.00, 290000, 140000, 2500, 3800, 47, "actions:onsite_conversion.lead_grouped", 0),
]


def _meta_rows(data, start, end):
    rows = []
    for c, a, ad, sp, imp, rch, lc, ac, res, ind, pl in data:
        ctr = f"{lc / imp * 100:.2f}" if imp else ""
        rows.append([c, a, ad, start, end, f"{sp:.2f}", imp, rch, lc, ac, res, ind, pl, ctr])
    return rows


def write_csv(path, header, rows, total=True):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        if total:  # platform total row - must be excluded, never double counted
            sums = ["Total", "", "", "", ""] + [round(sum(float(r[i] or 0) for r in rows), 2) for i in range(5, 11)] + ["", 0, ""]
            w.writerow(sums)


def tiktok(path, currency):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ad report"
    ws.append(["TikTok Ads Manager - Custom report"])
    ws.append(["Date range: 2026-09-01 ~ 2026-09-30"])
    ws.append([f"Currency: {currency}"])
    ws.append([])
    ws.append(["Campaign name", "Ad group name", "Ad name", "By Day", "Cost", "Impressions", "Reach", "Clicks (destination)", "Conversions", "Video views at 6s"])
    rows = [("ACME_UAE_TT", "UAE_EN_TT", "ACME_UAE_EN_TT_Video", d, 300.0 + i * 25, 90000 + i * 5000, 50000, 700 + i * 20, 9 + i, 21000 + i * 900)
            for i, d in enumerate(["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"])]
    rows += [("ACME_UAE_TT", "UAE_EN_TT", "ACME_UAE_EN_TT_Static", "2026-09-10", 180.0, 40000, 30000, 0, 0, 0)]
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def snapchat(path):
    write_csv(path, ["Campaign Name", "Ad Squad Name", "Ad Name", "Day", "Spend", "Paid Impressions", "Swipe Ups", "Currency"],
              [["ACME_UAE_SC", "UAE_SC_Broad", "ACME_UAE_EN_SC_Story", d, f"{250 + i * 10:.2f}", 120000 + i * 4000, 900 + i * 30, "AED"]
               for i, d in enumerate(["2026-09-03", "2026-09-12", "2026-09-21"])], total=False)


def template(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Platform", "Spend", "Impressions", "Clicks", "CTR"])
    ws["E2"] = "=IFERROR(D2/C2,0)"
    sm = wb.create_sheet("Summary")
    sm["A1"] = "Reporting period"
    sm["B1"] = ""
    sm["A2"], sm["B2"] = "Total spend", "=SUM(Data!B2:B20)"
    sm["A3"], sm["B3"] = "Total impressions", "=SUM(Data!C2:C20)"
    sm["A4"], sm["B4"] = "Total clicks", "=SUM(Data!D2:D20)"
    sm["A5"], sm["B5"] = "Blended CTR", "=IF(B3=0,0,B4/B3)"
    sm["B5"].number_format = "0.00%"
    wb.save(path)


def deck(path):
    """Synthetic client deck: cover with month label, one metric slide per creative (ATC-style
    label/value tables), a chart slide, a client-content slide, and an unsupported grouped
    shape that contains numbers."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    title_only = prs.slide_layouts[5]

    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Acme Dates – Performance Report"
    s.placeholders[1].text = "August 2026"

    def metric_slide(title, vals):
        sl = prs.slides.add_slide(title_only)
        sl.shapes.title.text = title
        labels = ["Range", "Impressions", "Reach", "Clicks", "CTR", "Spend"]
        tb = sl.shapes.add_table(2, len(labels), Inches(0.6), Inches(2.2), Inches(12), Inches(1.2)).table
        for i, l in enumerate(labels):
            tb.cell(0, i).text = l
            tb.cell(1, i).text = vals[i]
        return sl

    metric_slide("Awareness – ACME_UAE_EN_Video_Ramadan", ["1st – 31st Aug 2026", "760,000", "390,000", "5,200", "0.68%", "AED 3,900.00"])
    metric_slide("Leads – ACME_UAE_EN_Static_Offer", ["1st – 31st Aug 2026", "230,000", "110,000", "3,300", "1.43%", "AED 2,800.00"])
    metric_slide("Leads – ACME_KSA_AR_Static_Offer", ["1st – 31st Aug 2026", "290,000", "140,000", "2,500", "0.86%", "AED 2,400.00"])

    sl = prs.slides.add_slide(title_only)
    sl.shapes.title.text = "Spend by creative"
    cd = CategoryChartData()
    cd.categories = ["ACME_UAE_EN_Video_Ramadan", "ACME_UAE_EN_Static_Offer", "ACME_KSA_AR_Static_Offer"]
    cd.add_series("Spend", (3900, 2800, 2400))
    sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.6), Inches(11), Inches(5), cd)

    sl = prs.slides.add_slide(title_only)
    sl.shapes.title.text = "Organic social highlights"
    tx = sl.shapes.add_textbox(Inches(1), Inches(2), Inches(10), Inches(1)).text_frame
    tx.text = "Figures to be shared by the client (1 – 31 Aug 2026)."

    sl = prs.slides.add_slide(title_only)
    sl.shapes.title.text = "Key numbers"
    grp = sl.shapes.add_group_shape()
    for i, txt in enumerate(["1,234 leads", "AED 9,100 spend"]):
        b = grp.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1 + i * 4), Inches(2.5), Inches(3.5), Inches(1.5))
        b.text_frame.text = txt
        b.text_frame.paragraphs[0].runs[0].font.size = Pt(24)
    prs.save(path)


def main(out=OUT):
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "meta_acme_sept.csv"), META_HDR, _meta_rows(META_SEPT, "2026-09-01", "2026-09-30"))
    write_csv(os.path.join(out, "meta_acme_aug.csv"), META_HDR, _meta_rows(META_AUG, "2026-08-01", "2026-08-31"))
    # overlapping export: two identical rows + one conflicting row (same ad & dates, different spend)
    dup = _meta_rows(META_SEPT[:2], "2026-09-01", "2026-09-30")
    conflict = _meta_rows([META_SEPT[2]], "2026-09-01", "2026-09-30")[0]
    conflict[5] = "2999.99"
    write_csv(os.path.join(out, "meta_acme_sept_overlap.csv"), META_HDR, dup + [conflict], total=False)
    # missing columns: no Impressions, no Reach
    hdr = [h for h in META_HDR if h not in ("Impressions", "Reach", "CTR (link click-through rate)")]
    rows = [[v for h, v in zip(META_HDR, r) if h in hdr] for r in _meta_rows(META_SEPT[:3], "2026-09-01", "2026-09-30")]
    write_csv(os.path.join(out, "meta_acme_missing_cols.csv"), hdr, rows, total=False)
    tiktok(os.path.join(out, "tiktok_acme_sept_aed.xlsx"), "AED")
    tiktok(os.path.join(out, "tiktok_acme_sept_usd.xlsx"), "USD")
    snapchat(os.path.join(out, "snapchat_acme_sept.csv"))
    template(os.path.join(out, "acme_template.xlsx"))
    deck(os.path.join(out, "acme_client_deck.pptx"))
    with open(os.path.join(out, "README.txt"), "w") as fh:
        fh.write("SYNTHETIC fixtures for automated tests (fictional brand 'Acme Dates'). Not client data.\n")
    return out


if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else OUT))
