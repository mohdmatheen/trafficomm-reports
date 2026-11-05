"""Consolidated Excel report (no template). Values are written from the deterministic engine;
rates are recomputed from totals, missing values are written as 'N/A' (never 0)."""
from __future__ import annotations

import datetime as dt
import re

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import fields as F
from . import fmt as FM
from .calc import METRIC_LABELS, aggregate

RED = "EA3E3A"  # trafficomm.com signal red
CHARCOAL = "0C0C0D"  # trafficomm.com ink
LIGHT = "F4F4F4"
HDR_FILL = PatternFill("solid", fgColor=CHARCOAL)
HDR_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=16, color=RED)
SUB_FONT = Font(size=10, color="666666", italic=True)
BOLD = Font(bold=True)
THIN = Side(style="thin", color="DDDDDD")
TOTAL_FILL = PatternFill("solid", fgColor="EDEDED")

PCT = {"ctr", "vtr"}
MONEY = {"spend", "cpc", "cpm", "cpa", "purchase_value"}
DEC2 = {"roas", "frequency_calc"}
DIM_LABEL = {"platform": "Platform", "campaign_name": "Campaign", "creative": "Creative", "brand": "Brand / project",
             "objective": "Objective", "country": "Country", "market": "Market", "language": "Language", "ad_name": "Ad"}


def _num_format(metric, cur):
    if metric in PCT:
        return "0.00%"
    if metric in MONEY:
        return f'"{cur} "#,##0.00' if cur else "#,##0.00"
    if metric in DEC2:
        return "0.00"
    return "#,##0"


def _write_val(ws, r, c, metric, v, cur):
    cell = ws.cell(row=r, column=c)
    if v is None:
        cell.value = "N/A"
        cell.alignment = Alignment(horizontal="right")
        cell.font = Font(color="999999")
        return cell
    cell.value = v / 100.0 if metric in PCT else v
    cell.number_format = _num_format(metric, cur)
    return cell


def _header(ws, r, labels, c0=1):
    for i, l in enumerate(labels):
        cell = ws.cell(row=r, column=c0 + i, value=l)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[r].height = 30


def safe_title(name: str) -> str:
    return re.sub(r"[\\/*?:\[\]]", "-", str(name))[:31] or "Sheet"


def _title(ws, title, ctx):
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"{ctx['client']} · {ctx['brand'] or 'All brands'} · {ctx['period_label']} · Currency: {ctx['currency'] or 'n/a'}"
    ws["A2"].font = SUB_FONT
    ws.sheet_view.showGridLines = False


def _autowidth(ws, minw=10, maxw=48):
    widths = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            l = len(str(c.value)) if not isinstance(c.value, float) else 12
            widths[c.column_letter] = max(widths.get(c.column_letter, 0), l)
    for k, w in widths.items():
        ws.column_dimensions[k].width = max(minw, min(maxw, w + 2))


def write_table(ws, start_row, dims, metrics, rows, totals, cur, title=None, note=None, with_total=True):
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = Font(bold=True, size=12, color=CHARCOAL)
        r += 1
    if note:
        ws.cell(row=r, column=1, value=note).font = SUB_FONT
        r += 1
    labels = [DIM_LABEL.get(d, d) for d in dims] + [METRIC_LABELS.get(m, m) for m in metrics]
    _header(ws, r, labels)
    hdr = r
    for row in rows:
        r += 1
        for i, d in enumerate(dims):
            v = row.get(d)
            ws.cell(row=r, column=i + 1, value=F.PLATFORM_LABELS.get(v, v) if d == "platform" else v)
        for j, m in enumerate(metrics):
            _write_val(ws, r, len(dims) + j + 1, m, row.get(m), cur)
    if with_total and totals and len(rows) > 1:
        r += 1
        ws.cell(row=r, column=1, value="Total (rates recomputed from totals)").font = BOLD
        for j, m in enumerate(metrics):
            v = totals.get(m)
            if m in ("reach", "frequency_calc"):
                v = None
            c = _write_val(ws, r, len(dims) + j + 1, m, v, cur)
            c.font = BOLD
        for col in range(1, len(labels) + 1):
            ws.cell(row=r, column=col).fill = TOTAL_FILL
    return hdr, r


def _bar_chart(ws, title, cat_col, val_col, hdr_row, last_row, anchor, y_title=""):
    if last_row - hdr_row < 1:
        return
    ch = BarChart()
    ch.type = "bar"
    ch.title = title
    ch.y_axis.title = y_title
    ch.height = 7
    ch.width = 16
    data = Reference(ws, min_col=val_col, min_row=hdr_row, max_row=last_row)
    cats = Reference(ws, min_col=cat_col, min_row=hdr_row + 1, max_row=last_row)
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(cats)
    ch.legend = None
    ws.add_chart(ch, anchor)


def build_excel(path: str, calc, norm, ctx: dict, commentary: list[dict] | None, plan: dict, profile: dict) -> dict:
    """Returns dict with sheet list and the values written (for validation read-back)."""
    wb = Workbook()
    cur = calc.currency or ""
    written = {"sheets": [], "kpis": {}}
    t = calc.totals
    metrics = [m for m in calc.tables[next(iter(calc.tables))].metrics] if calc.tables else []
    agg_metrics = [m for m in metrics if m not in ("reach", "frequency_calc")]

    # ---- Executive summary
    ws = wb.active
    ws.title = "Executive Summary"
    _title(ws, ctx["title"], ctx)
    ws["A4"] = "Reporting period"
    ws["B4"] = ctx["period_label"]
    ws["A5"] = "Data window"
    ws["B5"] = ctx["data_window"]
    ws["A6"] = "Sources"
    ws["B6"] = f"{len({r['file'] for r in norm.mapping_summary})} file(s), {calc.included_rows} rows included"
    ws["A7"] = "Clicks definition"
    ws["B7"] = calc.click_metric_label
    ws["A8"] = "Conversion for CPA"
    ws["B8"] = calc.conversion_label or "Not selected - CPA not calculated"
    for rr in range(4, 9):
        ws.cell(row=rr, column=1).font = BOLD
    kpis = [m for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "video_plays_3s", "vtr", "conversions", "cpa", "leads_meta_form", "website_leads", "purchase_value", "roas"] if m in metrics or m in ("leads_meta_form", "website_leads") and t.get(m) is not None]
    _header(ws, 10, ["KPI", "Value", "Definition"])
    r = 10
    for m in kpis:
        r += 1
        ws.cell(row=r, column=1, value=METRIC_LABELS.get(m, m)).font = BOLD
        _write_val(ws, r, 2, m, t.get(m), cur)
        ws.cell(row=r, column=3, value=calc.definitions.get(m) or (F.FIELDS[m].definition if m in F.FIELDS else ""))
        written["kpis"][m] = t.get(m)
    r += 1
    ws.cell(row=r, column=1, value="Reach").font = BOLD
    ws.cell(row=r, column=2, value="N/A")
    ws.cell(row=r, column=3, value="Reach is not additive across ads/platforms; see ad-level tabs. No deduplicated reach is available from these exports.")
    if commentary:
        r += 2
        ws.cell(row=r, column=1, value="Summary commentary").font = Font(bold=True, size=12, color=RED)
        for c in commentary[: (3 if plan.get("short_summary") else 8)]:
            r += 1
            ws.cell(row=r, column=1, value={"observation": "Observed", "interpretation": "Interpretation", "recommendation": "Recommended action"}.get(c["type"], c["type"]))
            ws.cell(row=r, column=2, value=c["text"]).alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
            ws.row_dimensions[r].height = 32
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 90
    written["sheets"].append(ws.title)

    def table_sheet(name, key, dims_override=None, chart_metric="spend", note=None):
        tb = calc.tables.get(key)
        if not tb or not tb.rows:
            return None
        ws = wb.create_sheet(safe_title(name))
        _title(ws, tb.title, ctx)
        ms = metrics if (len(tb.dims) >= 2 or key in ("creative", "ad")) else agg_metrics
        if key not in ("creative", "ad") and all(r_["_rows"] > 1 for r_ in tb.rows):
            ms = agg_metrics
        hdr, last = write_table(ws, 4, tb.dims, ms, tb.rows, t, cur, note=note or ("Reach/frequency shown only where a row is a single ad (reach is not additive)." if "reach" in ms else None))
        ws.freeze_panes = ws.cell(row=hdr + 1, column=len(tb.dims) + 1)
        ws.auto_filter.ref = f"A{hdr}:{get_column_letter(len(tb.dims) + len(ms))}{last - (1 if len(tb.rows) > 1 else 0)}"
        _autowidth(ws)
        if chart_metric in ms and len(tb.rows) <= 30:
            _bar_chart(ws, f"{METRIC_LABELS[chart_metric]} by {DIM_LABEL.get(tb.dims[-1], tb.dims[-1])}", len(tb.dims), len(tb.dims) + ms.index(chart_metric) + 1,
                       hdr, hdr + len(tb.rows), f"{get_column_letter(len(tb.dims) + len(ms) + 2)}4")
        written["sheets"].append(ws.title)
        written[key] = {(tuple(r_[d] for d in tb.dims)): {m: r_.get(m) for m in ms} for r_ in tb.rows}
        return ws

    order = profile.get("excel_tab_order") or []
    makers = {
        "Platform Summary": lambda: table_sheet("Platform Summary", "platform"),
        "Brand Summary": lambda: table_sheet("Brand Summary", "brand") if calc.tables.get("brand") and len(calc.tables["brand"].rows) > 1 else None,
        "Campaign Performance": lambda: table_sheet("Campaign Performance", "campaign"),
        "Creative Performance": lambda: table_sheet("Creative Performance", "creative", chart_metric="ctr"),
        "Market & Language": lambda: _market_sheet(wb, calc, ctx, agg_metrics, cur, written),
    }
    default_order = ["Platform Summary", "Brand Summary", "Campaign Performance", "Creative Performance", "Market & Language"]
    seq = [n for n in order if n in makers] + [n for n in default_order if n not in order]
    for name in seq:
        if name == "Creative Performance" and not plan.get("include_creative", True):
            continue
        makers[name]()
    for key, tb in calc.tables.items():
        if key.startswith("x_"):
            table_sheet(tb.title[:31], key)

    # split tabs (e.g. separate UAE and KSA)
    split = plan.get("split_tabs_by")
    if split and split in calc.dataset.columns:
        cur_rows = calc.dataset[calc.dataset["_period"] == "current"]
        for val in sorted(cur_rows[split].dropna().unique()):
            sub = cur_rows[cur_rows[split] == val]
            ws = wb.create_sheet(safe_title(f"{val}"))
            _title(ws, f"{DIM_LABEL.get(split, split)}: {val}", ctx)
            tot = aggregate(sub, [])[0]
            r0 = 4
            for dims, ttl in ((["platform"], "By platform"), (["platform", "campaign_name"], "By campaign")):
                if all(d in sub.columns and sub[d].notna().any() for d in dims):
                    rows = sorted(aggregate(sub, dims), key=lambda x: -(x.get("spend") or 0))
                    _, last = write_table(ws, r0, dims, agg_metrics, rows, tot, cur, title=ttl)
                    r0 = last + 3
            _autowidth(ws)
            written["sheets"].append(ws.title)
            written.setdefault("split", {})[str(val)] = {m: tot.get(m) for m in agg_metrics}

    if calc.comparison:
        _comparison_sheet(wb, calc, ctx, cur, written)

    # consolidated data with provenance
    ws = wb.create_sheet("Consolidated Data")
    _title(ws, "Consolidated data (normalised, with source provenance)", ctx)
    d = calc.dataset
    d = d[d["_period"].notna()]
    dim_cols = [c for c in ["_period", "platform", "brand", "market", "country", "language", "campaign_name", "adset_name", "ad_name", "creative", "objective", "format", "date", "report_start", "report_end", "currency"] if c in d.columns and d[c].notna().any()]
    met_cols = [c for c in ["spend", "impressions", "reach", "frequency", "link_clicks", "clicks_all", "swipe_ups", "clicks", "video_plays_3s", "video_views_6s", "video_views_snap", "thruplays", "leads_meta_form", "website_leads", "leads", "conversions", "purchases", "purchase_value"] if c in d.columns and d[c].notna().any()]
    prov = ["_file", "_sheet", "_row", "_fx_rate"]
    labels = [{"_period": "Period"}.get(c, DIM_LABEL.get(c, F.FIELDS[c].label if c in F.FIELDS else c)) for c in dim_cols] + \
             [METRIC_LABELS.get(c, c) for c in met_cols] + ["Source file", "Sheet", "Source row", "FX rate applied"]
    _header(ws, 4, labels)
    rr = 4
    for _, row in d.iterrows():
        rr += 1
        vals = [row.get(c) for c in dim_cols] + [None if (row.get(c) is None or row.get(c) != row.get(c)) else float(row.get(c)) for c in met_cols] + [row.get(c) for c in prov]
        for ci, v in enumerate(vals, 1):
            if isinstance(v, float) and v != v:
                v = None
            if isinstance(v, (dt.date,)):
                ws.cell(row=rr, column=ci, value=v).number_format = "yyyy-mm-dd"
                continue
            if v is None and dim_cols and ci > len(dim_cols) and ci <= len(dim_cols) + len(met_cols):
                ws.cell(row=rr, column=ci, value="N/A").font = Font(color="999999")
                continue
            c = ws.cell(row=rr, column=ci, value=F.PLATFORM_LABELS.get(v, v) if dim_cols[ci - 1:ci] == ["platform"] else v)
            if ci > len(dim_cols) and ci <= len(dim_cols) + len(met_cols):
                c.number_format = _num_format(met_cols[ci - len(dim_cols) - 1], cur) if met_cols[ci - len(dim_cols) - 1] != "frequency" else "0.00"
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:{get_column_letter(len(labels))}{rr}"
    _autowidth(ws, maxw=40)
    written["sheets"].append(ws.title)
    written["consolidated_rows"] = rr - 4

    if commentary and plan.get("include_commentary", True):
        ws = wb.create_sheet("Insights")
        _title(ws, "Observations, interpretations and recommendations", ctx)
        _header(ws, 4, ["Type", "Statement", "Evidence (calculated values)"])
        rr = 4
        for c in commentary:
            rr += 1
            ws.cell(row=rr, column=1, value={"observation": "Observed performance", "interpretation": "Interpretation / hypothesis", "recommendation": "Recommended action"}.get(c["type"], c["type"]))
            ws.cell(row=rr, column=2, value=c["text"]).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(row=rr, column=3, value="; ".join(c.get("evidence", []))).alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[rr].height = 45
        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["B"].width = 80
        ws.column_dimensions["C"].width = 60
        written["sheets"].append(ws.title)

    _quality_sheet(wb, norm, calc, ctx)
    written["sheets"].append("Data Quality")
    _method_sheet(wb, calc, ctx, norm)
    written["sheets"].append("Methodology")
    wb.save(path)
    return written


def _market_sheet(wb, calc, ctx, metrics, cur, written):
    keys = [k for k in ("country", "market", "language") if calc.tables.get(k) and calc.tables[k].rows]
    if not keys:
        return None
    ws = wb.create_sheet("Market & Language")
    _title(ws, "Market / language breakdown", ctx)
    r = 4
    for k in keys:
        tb = calc.tables[k]
        _, last = write_table(ws, r, tb.dims, metrics, tb.rows, calc.totals, cur, title=tb.title)
        written[k] = {(tuple(x[d] for d in tb.dims)): {m: x.get(m) for m in metrics} for x in tb.rows}
        r = last + 3
    _autowidth(ws)
    written["sheets"].append(ws.title)
    return ws


def _comparison_sheet(wb, calc, ctx, cur, written):
    ws = wb.create_sheet("Period Comparison")
    _title(ws, "Period comparison", ctx)
    cmp = calc.comparison
    _header(ws, 4, ["Metric", "Current period", "Comparison period", "Change", "Change %"])
    r = 4
    for m, dl in cmp["deltas"].items():
        a, b = cmp["current"].get(m), cmp["previous"].get(m)
        if a is None and b is None:
            continue
        r += 1
        ws.cell(row=r, column=1, value=METRIC_LABELS.get(m, m)).font = BOLD
        _write_val(ws, r, 2, m, a, cur)
        _write_val(ws, r, 3, m, b, cur)
        if m in PCT:
            c = ws.cell(row=r, column=4, value=None if dl["abs"] is None else dl["abs"])
            c.value = "N/A" if dl["abs"] is None else dl["abs"]
            c.number_format = '0.00" pp"'
        else:
            _write_val(ws, r, 4, m, dl["abs"], cur)
        _write_val(ws, r, 5, "ctr", dl["pct"], cur)
    ws.cell(row=r + 2, column=1, value="Change % = (current - comparison) / comparison. Rate changes in percentage points (pp). N/A where either period lacks the metric or the comparison value is zero.").font = SUB_FONT
    _autowidth(ws)
    written["sheets"].append(ws.title)
    written["comparison"] = {m: cmp["current"].get(m) for m in cmp["deltas"]}


def _quality_sheet(wb, norm, calc, ctx):
    ws = wb.create_sheet("Data Quality")
    _title(ws, "Data quality: sources, exclusions, assumptions", ctx)
    r = 4
    ws.cell(row=r, column=1, value="Included sources").font = Font(bold=True, size=12, color=CHARCOAL)
    r += 1
    _header(ws, r, ["File", "Sheet", "Platform", "Rows", "Currency", "Mapped columns", "Ignored columns", "Unresolved columns"])
    for m in norm.mapping_summary:
        r += 1
        for ci, v in enumerate([m["file"], m["sheet"], F.PLATFORM_LABELS.get(m["platform"], m["platform"]), m["rows"], m["currency"],
                                ", ".join(f"{x['original']}→{x['canonical']}" for x in m["mapped"]), ", ".join(m["ignored"]), ", ".join(m["unresolved"]) or "-"], 1):
            ws.cell(row=r, column=ci, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    r += 2
    ws.cell(row=r, column=1, value="Exclusions").font = Font(bold=True, size=12, color=CHARCOAL)
    r += 1
    _header(ws, r, ["File", "Sheet", "Scope", "Reason"])
    excl = list(norm.exclusions)
    d = calc.dataset if calc.dataset is not None else None
    if norm.df is not None and len(norm.df):
        for _, x in norm.df[norm.df["_excluded"].notna()].iterrows():
            excl.append({"file": x["_file"], "sheet": x["_sheet"], "scope": f"row {x['_row']}", "reason": x["_excluded"]})
    for e in excl or [{"file": "-", "sheet": "", "scope": "", "reason": "No exclusions"}]:
        r += 1
        for ci, v in enumerate([e.get("file"), e.get("sheet", ""), e.get("scope", ""), e.get("reason")], 1):
            ws.cell(row=r, column=ci, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    r += 2
    ws.cell(row=r, column=1, value="Assumptions, warnings and overlaps").font = Font(bold=True, size=12, color=CHARCOAL)
    for kind, items in (("Assumption", norm.assumptions), ("Warning", norm.warnings),
                        ("Overlap", [f"{o['type'].replace('_', ' ')}: {', '.join(o['files'])}" + (f" ({o.get('rows')} rows)" if o.get("rows") else "") for o in norm.overlaps])):
        for it in items:
            r += 1
            ws.cell(row=r, column=1, value=kind)
            ws.cell(row=r, column=2, value=it).alignment = Alignment(wrap_text=True)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=8)
    for col, w in zip("ABCDEFGH", (34, 28, 16, 8, 10, 60, 40, 30)):
        ws.column_dimensions[col].width = w


def _method_sheet(wb, calc, ctx, norm):
    ws = wb.create_sheet("Methodology")
    _title(ws, "Methodology and definitions", ctx)
    lines = [
        ("Calculation engine", "All figures are computed by deterministic code from the uploaded exports. The language model never calculates figures."),
        ("Additivity", "Spend, impressions, clicks, video plays and conversions are summed only across compatible scopes. Reach, unique clicks and frequency are never summed."),
        ("Rates", "Aggregate rates are recomputed from summed numerators and denominators over rows where both exist; never averaged."),
        ("Missing vs zero", "Missing values are shown as N/A and are never treated as zero. A zero denominator gives N/A."),
        ("Precision", "Full precision is kept in calculations; rounding is for display only."),
        ("Currency", f"Reporting currency: {calc.currency or 'n/a'}. Currencies are only combined with an employee-approved, recorded exchange rate."),
        ("Attribution", "Conversions/leads are platform-attributed and are not deduplicated across platforms; they do not represent unique customers."),
        ("Raw files", "Original uploads are stored unchanged; this workbook is generated from a normalised copy."),
    ]
    lines += [(f"Definition: {METRIC_LABELS.get(k, k)}", v) for k, v in calc.definitions.items()]
    if norm.derivations:
        lines.append(("Derived dimensions", "; ".join(f"{k}: {v} rows from naming convention" for k, v in norm.derivations.items())))
    _header(ws, 4, ["Topic", "Detail"])
    for i, (a, b) in enumerate(lines, 5):
        ws.cell(row=i, column=1, value=a).font = BOLD
        ws.cell(row=i, column=2, value=b).alignment = Alignment(wrap_text=True)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
