"""New presentation generator: clean 16:9 deck with editable native tables and charts, containing
only sections supported by the data."""
from __future__ import annotations

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

from . import fields as F
from . import fmt as FM
from .calc import METRIC_LABELS

RED = RGBColor(0xEA, 0x3E, 0x3A)  # trafficomm.com signal red
CHAR = RGBColor(0x0C, 0x0C, 0x0D)  # trafficomm.com ink
GREY = RGBColor(0x6B, 0x6B, 0x6B)
LIGHT = RGBColor(0xF6, 0xF6, 0xF3)  # trafficomm.com paper
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "Calibri"
MAX_ROWS = 10

SECTIONS = ["cover", "summary", "platform", "brand", "creative", "market", "comparison", "insights", "methodology"]


def _tb(slide, x, y, w, h, text, size=14, bold=False, color=CHAR, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    lines = text if isinstance(text, list) else [text]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = color
        r.font.name = FONT
    return box


def _header(slide, title, subtitle):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18), Inches(7.5))
    bar.fill.solid()
    bar.fill.fore_color.rgb = RED
    bar.line.fill.background()
    _tb(slide, 0.5, 0.3, 12.3, 0.7, title, 26, True, CHAR)
    _tb(slide, 0.5, 0.95, 12.3, 0.4, subtitle, 12, False, GREY)


def _footer(slide, text, n):
    _tb(slide, 0.5, 7.05, 10, 0.3, text, 9, False, GREY)
    _tb(slide, 12.2, 7.05, 0.8, 0.3, str(n), 9, False, GREY, PP_ALIGN.RIGHT)


def _table(slide, x, y, w, rows, header, col_w=None, font=11):
    n_r, n_c = len(rows) + 1, len(header)
    h = Inches(0.36) * n_r
    gt = slide.shapes.add_table(n_r, n_c, Inches(x), Inches(y), Inches(w), h).table
    for ci, label in enumerate(header):
        c = gt.cell(0, ci)
        c.text = label
        c.fill.solid()
        c.fill.fore_color.rgb = CHAR
        p = c.text_frame.paragraphs[0]
        p.runs[0].font.size = Pt(font)
        p.runs[0].font.bold = True
        p.runs[0].font.color.rgb = WHITE
        p.runs[0].font.name = FONT
        p.alignment = PP_ALIGN.CENTER if ci else PP_ALIGN.LEFT
    for ri, row in enumerate(rows, 1):
        for ci, v in enumerate(row):
            c = gt.cell(ri, ci)
            c.text = str(v)
            c.fill.solid()
            c.fill.fore_color.rgb = LIGHT if ri % 2 else WHITE
            p = c.text_frame.paragraphs[0]
            p.runs[0].font.size = Pt(font)
            p.runs[0].font.color.rgb = CHAR
            p.runs[0].font.name = FONT
            p.alignment = PP_ALIGN.RIGHT if ci else PP_ALIGN.LEFT
    if col_w:
        for i, cw in enumerate(col_w):
            gt.columns[i].width = Inches(cw)
    return gt


def _bar(slide, x, y, w, h, cats, series: dict, number_format="#,##0"):
    cd = CategoryChartData()
    cd.categories = cats
    for name, vals in series.items():
        cd.add_series(name, vals)
    gf = slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(x), Inches(y), Inches(w), Inches(h), cd)
    ch = gf.chart
    ch.has_legend = len(series) > 1
    if ch.has_legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
    plot = ch.plots[0]
    plot.has_data_labels = True
    plot.data_labels.number_format = number_format
    plot.data_labels.number_format_is_linked = False
    plot.data_labels.font.size = Pt(10)
    plot.series[0].format.fill.solid()
    plot.series[0].format.fill.fore_color.rgb = RED
    ch.category_axis.tick_labels.font.size = Pt(10)
    ch.value_axis.tick_labels.font.size = Pt(9)
    ch.value_axis.has_major_gridlines = False
    return gf


def _m(metric, v, cur):
    return FM.fmt_metric(metric, v, cur)


def build_new_deck(path: str, calc, ctx: dict, commentary: list[dict] | None, plan: dict) -> dict:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    cur = calc.currency or ""
    t = calc.totals
    remove = set(plan.get("remove_sections", []))
    sub = f"{ctx['client']} · {ctx['brand'] or 'All brands'} · {ctx['period_label']}"
    foot = f"Source: uploaded platform exports ({ctx['data_window']}). Figures calculated by Trafficomm Reporting Workspace."
    made = []
    expected = {}  # slide -> {label: text} for validation read-back
    n = 0

    def new_slide(key):
        nonlocal n
        n += 1
        made.append(key)
        return prs.slides.add_slide(blank)

    # cover
    s = new_slide("cover")
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = CHAR
    bg.line.fill.background()
    acc = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(3.55), Inches(1.4), Inches(0.08))
    acc.fill.solid()
    acc.fill.fore_color.rgb = RED
    acc.line.fill.background()
    _tb(s, 0.8, 2.0, 11.5, 1.4, ctx["title"], 40, True, WHITE)
    _tb(s, 0.8, 3.8, 11.5, 0.6, f"{ctx['brand'] or ctx['client']}", 20, False, WHITE)
    _tb(s, 0.8, 4.4, 11.5, 0.6, ctx["period_label"], 20, False, RGBColor(0xDD, 0xDD, 0xDD))
    expected[n] = {"period": ctx["period_label"]}

    # executive summary - KPI tiles
    if "summary" not in remove:
        s = new_slide("summary")
        _header(s, "Executive summary", sub)
        kpis = [m for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "video_plays_3s", "vtr", "conversions", "cpa"]
                if t.get(m) is not None][:8]
        expected[n] = {}
        for i, m in enumerate(kpis):
            col, row = i % 4, i // 4
            x, y = 0.5 + col * 3.1, 1.6 + row * 1.45
            tile = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(2.9), Inches(1.25))
            tile.fill.solid()
            tile.fill.fore_color.rgb = LIGHT
            tile.line.fill.background()
            val = _m(m, t.get(m), cur)
            _tb(s, x + 0.15, y + 0.12, 2.6, 0.35, METRIC_LABELS.get(m, m).upper(), 10, True, GREY)
            _tb(s, x + 0.15, y + 0.45, 2.6, 0.6, val, 24, True, RED if i == 0 else CHAR)
            expected[n][m] = val
        if commentary:
            obs = [c for c in commentary if c["type"] == "observation"][: (2 if plan.get("short_summary") else 3)]
            _tb(s, 0.5, 4.65, 12.3, 0.35, "Key observations", 14, True, CHAR)
            _tb(s, 0.5, 5.05, 12.3, 1.9, ["• " + c["text"] for c in obs], 13, False, CHAR)
        _tb(s, 0.5, 6.7, 12.3, 0.3, f"Clicks = {calc.click_metric_label}. Reach is not summed across ads or platforms.", 9, False, GREY)
        _footer(s, foot, n)

    def table_slides(key, title, tb, metrics, note=None):
        rows = tb.rows
        pages = [rows[i:i + MAX_ROWS] for i in range(0, len(rows), MAX_ROWS)] or [[]]
        for pi, page in enumerate(pages):
            s = new_slide(key)
            _header(s, title + (f" ({pi + 1}/{len(pages)})" if len(pages) > 1 else ""), sub)
            dlab = {"platform": "Platform", "brand": "Brand", "objective": "Objective", "creative": "Creative",
                    "campaign_name": "Campaign", "country": "Country", "market": "Market", "language": "Language"}
            dims = list(reversed(tb.dims)) if "creative" in tb.dims else tb.dims  # creative first, then brand/objective
            hdr = [dlab.get(d, d) for d in dims] + [METRIC_LABELS.get(m, m) for m in metrics]
            body = []
            expected[n] = {}
            for r in page:
                dv = [F.PLATFORM_LABELS.get(str(r[d]), str(r[d])) for d in dims]
                vals = [_m(m, r.get(m), cur) for m in metrics]
                body.append(dv + vals)
                expected[n][" / ".join(dv)] = vals
            dw = {"creative": 2.4, "brand": 2.2, "objective": 1.1, "campaign_name": 3.2, "platform": 2.2}
            wd = [dw.get(d, 2.0) for d in dims]
            rest = (12.3 - sum(wd)) / max(1, len(metrics))
            _table(s, 0.5, 1.55, 12.3, body, hdr, wd + [rest] * len(metrics), font=10 if len(metrics) + len(dims) > 8 else 11)
            if note:
                _tb(s, 0.5, 6.55, 12.3, 0.4, note, 9, False, GREY)
            _footer(s, foot, n)

    agg = [m for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa"] if t.get(m) is not None]
    pt = calc.tables.get("platform")
    if pt and len(pt.rows) > 1 and "platform" not in remove:
        s = new_slide("platform")
        _header(s, "Performance by platform", sub)
        cats = [F.PLATFORM_LABELS.get(r["platform"], r["platform"]) for r in pt.rows]
        _bar(s, 0.5, 1.5, 5.6, 3.6, cats, {"Spend": [r.get("spend") or 0 for r in pt.rows]}, '#,##0')
        ms = [m for m in agg if m != "spend"][:5]
        body = [[F.PLATFORM_LABELS.get(r["platform"], r["platform"])] + [_m(m, r.get(m), cur) for m in ["spend"] + ms] for r in pt.rows]
        expected[n] = {b[0]: b[1:] for b in body}
        _table(s, 6.4, 1.6, 6.4, body, ["Platform"] + [METRIC_LABELS[m] for m in ["spend"] + ms], [1.6] + [4.8 / (len(ms) + 1)] * (len(ms) + 1), font=10)
        _tb(s, 0.5, 5.4, 12.3, 1.2, [f"Clicks = {calc.click_metric_label}. " + ("Platforms without a compatible click metric show N/A." if any(r.get('clicks') is None for r in pt.rows) else ""),
                                     "Conversions are platform-attributed and not deduplicated across platforms."], 10, False, GREY)
        _footer(s, foot, n)
    if calc.tables.get("brand") and len(calc.tables["brand"].rows) > 1 and "brand" not in remove:
        table_slides("brand", "Performance by brand / project", calc.tables["brand"], agg)
    if calc.tables.get("creative") and "creative" not in remove:
        tb = calc.tables["creative"]
        ms = [m for m in ["spend", "impressions", "reach", "clicks", "ctr", "video_plays_3s", "vtr"] if any(r.get(m) is not None for r in tb.rows)]
        table_slides("creative", "Creative highlights", tb, ms, note="Reach shown per creative where it ran as a single ad; it is not additive. Sorted by spend.")
    elif calc.tables.get("campaign") and "creative" not in remove:
        table_slides("creative", "Campaign highlights", calc.tables["campaign"], agg)
    for k in ("country", "market", "language"):
        tb = calc.tables.get(k)
        if tb and len(tb.rows) > 1 and "market" not in remove:
            table_slides("market", tb.title, tb, agg)
    if calc.comparison and "comparison" not in remove:
        s = new_slide("comparison")
        _header(s, "Period comparison", sub)
        cmpd = calc.comparison
        body = []
        for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa"]:
            a, b = cmpd["current"].get(m), cmpd["previous"].get(m)
            if a is None and b is None:
                continue
            body.append([METRIC_LABELS[m], _m(m, a, cur), _m(m, b, cur), FM.fmt_pct(cmpd["deltas"][m]["pct"])])
        expected[n] = {b[0]: b[1:] for b in body}
        _table(s, 0.5, 1.6, 9, body, ["Metric", "Current", "Comparison", "Change %"], [3, 2, 2, 2])
        _footer(s, foot, n)
    if commentary and "insights" not in remove:
        s = new_slide("insights")
        _header(s, "Observations and recommendations", sub)
        groups = [("Observed performance", "observation"), ("Interpretation (hypotheses to verify)", "interpretation")]
        if "recommendations" not in remove:
            groups.append(("Recommended actions", "recommendation"))
        y = 1.5
        lim = 2 if plan.get("short_summary") else 4
        for label, typ in groups:
            items = [c["text"] for c in commentary if c["type"] == typ][:lim]
            if not items:
                continue
            _tb(s, 0.5, y, 12.3, 0.35, label, 14, True, RED)
            box_h = 0.33 * len(items) + 0.25
            _tb(s, 0.5, y + 0.38, 12.3, box_h, ["• " + i for i in items], 12, False, CHAR)
            y += 0.38 + box_h + 0.15
        _footer(s, foot, n)
    if "methodology" not in remove:
        s = new_slide("methodology")
        _header(s, "Methodology and caveats", sub)
        lines = ["All figures are calculated deterministically from the uploaded exports; no figures are generated by AI.",
                 f"Clicks = {calc.click_metric_label}. CTR = clicks / impressions × 100; CPC = spend / clicks; CPM = spend / impressions × 1,000.",
                 f"CPA uses: {calc.conversion_label or 'not calculated (no conversion metric selected)'}.",
                 "Reach and frequency are not additive and are only shown for single ads.",
                 "Platform-attributed conversions are not deduplicated across platforms.",
                 f"Currency: {cur or 'n/a'}. Data window: {ctx['data_window']}."]
        _tb(s, 0.5, 1.6, 12.3, 4.8, ["• " + l for l in lines], 13, False, CHAR)
        _footer(s, foot, n)
    prs.save(path)
    return {"sections": made, "expected": expected, "slides": n}
