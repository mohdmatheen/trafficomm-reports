"""Populate a client-supplied Excel template without overwriting the original.

* inspects tabs, header rows, input ranges and formula cells first;
* writes only into input ranges (never over formula cells), extends row formulas down;
* flags features openpyxl cannot preserve (charts/images/pivots/external links);
* recalculates with LibreOffice and verifies formula results against the engine.
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl
import pandas as pd
from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator

from . import fields as F
from . import fmt as FM
from .calc import aggregate
from .parsing import check_ooxml
from .render import RenderError, recalc_xlsx

ERRORS = ("#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A", "#NUM!", "#NULL!")
DERIVED_HEADERS = {"views": "views", "ctr": "ctr", "cpc": "cpc", "cpm": "cpm", "cpa": "cpa", "cpl": "cpa", "vtr": "vtr", "roas": "roas", "clicks": "clicks", "conversions": "conversions", "leads": "conversions"}


# a template's generic "Clicks"/"CTR" columns mean the engine's selected definition (shown in Methodology)
TEMPLATE_ALIAS = {"clicks_unspecified": "clicks", "reported_ctr": "ctr", "reported_cpc": "cpc", "reported_cpm": "cpm", "reported_cpr": "cpa",
                  "leads": "conversions", "results": "conversions"}


def _hdr_key(v) -> str | None:
    if not isinstance(v, str):
        return None
    m = F.match_header(v)
    if m["canonical"]:
        return TEMPLATE_ALIAS.get(m["canonical"], m["canonical"])
    n = F.normalise_header(v)[0]
    return DERIVED_HEADERS.get(n)


MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
          "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
          "nov": 11, "november": 11, "dec": 12, "december": 12}
MARKETS = {"uae": "UAE", "ksa": "KSA", "saudi": "KSA", "saudi arabia": "KSA", "qatar": "Qatar", "kuwait": "Kuwait", "bahrain": "Bahrain",
           "oman": "Oman", "egypt": "Egypt", "united arab emirates": "UAE"}
PLATFORM_ALIASES = {"meta": "meta", "facebook": "meta", "instagram": "meta", "facebook & instagram": "meta", "meta (facebook & instagram)": "meta",
                    "fb": "meta", "tiktok": "tiktok", "tik tok": "tiktok", "snapchat": "snapchat", "snap": "snapchat",
                    "google": "google_ads", "google ads": "google_ads", "google_ads": "google_ads", "adwords": "google_ads"}
TOTAL_RE = re.compile(r"^\s*(grand\s+|sub\s*-?\s*)?totals?\b", re.I)
SCAN_ROWS = 300


def _row_merged(ws, row: int) -> bool:
    return any(rng.min_row <= row <= rng.max_row for rng in ws.merged_cells.ranges)


def _header_keys(ws, r: int) -> dict[int, str]:
    keys = {}
    for cidx in range(1, min(ws.max_column, 60) + 1):
        k = _hdr_key(ws.cell(row=r, column=cidx).value)
        if k:
            keys[cidx] = k
    return keys


def _is_header(keys: dict[int, str]) -> bool:
    dims = [k for k in keys.values() if k in F.FIELDS and F.FIELDS[k].kind == "dimension"]
    return len(keys) >= 3 and bool(dims) and len(dims) < len(keys)


def _months_in(text: str) -> set[int]:
    return {MONTHS[w] for w in re.findall(r"[a-z]+", (text or "").lower()) if w in MONTHS}


def _market_in(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"\ball\s+(markets|countries|regions)\b", t):
        return None
    for name in sorted(MARKETS, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", t):
            return MARKETS[name]
    return None


def _table(ws, r: int, keys: dict[int, str], prev_end: int) -> dict:
    """One input table: its header row, the bounded block of data rows under it (stopping at a total row,
    a merged title row, a blank gap after labelled rows or the next header) and the title just above it."""
    dims = [k for k in keys.values() if k in F.FIELDS and F.FIELDS[k].kind == "dimension"]
    label_col = next(c for c, k in keys.items() if k in dims)
    formula_cols = [c for c in keys if isinstance(ws.cell(row=r + 1, column=c).value, str) and str(ws.cell(row=r + 1, column=c).value).startswith("=")]
    labels, total_row, last = {}, None, r
    rr = r + 1
    while rr <= ws.max_row:
        lab = ws.cell(row=rr, column=label_col).value
        if _row_merged(ws, rr) or _is_header(_header_keys(ws, rr)):
            break
        if isinstance(lab, str) and TOTAL_RE.match(lab):
            total_row = rr
            break
        if lab in (None, ""):
            if labels:  # end of a labelled block
                break
        else:
            labels[rr] = str(lab).strip()
        last = rr
        rr += 1
    title = ""
    for tr in range(r - 1, max(prev_end, r - 4), -1):
        v = next((ws.cell(row=tr, column=c).value for c in range(1, min(ws.max_column, 20) + 1)
                  if isinstance(ws.cell(row=tr, column=c).value, str) and ws.cell(row=tr, column=c).value.strip()), None)
        if v:
            title = v.strip()
            break
    block_end = (total_row - 1) if total_row else (last if labels else ws.max_row)
    return {"header_row": r, "columns": {str(c): k for c, k in keys.items()}, "formula_cols": formula_cols, "label_col": label_col,
            "labels": {str(k): v for k, v in labels.items()}, "total_row": total_row, "block_end": block_end, "title": title,
            "prefilled_rows": sum(1 for x in range(r + 1, block_end + 1) if any(ws.cell(row=x, column=c).value not in (None, "") for c in keys if c not in formula_cols))}


def inspect_template(path: str) -> dict:
    info = {"container": check_ooxml(path, "xlsx"), "sheets": [], "unsupported": []}
    c = info["container"]
    if c.get("charts"):
        info["unsupported"].append(f"{c['charts']} chart(s): openpyxl does not preserve charts in existing workbooks - they will be missing from the populated file")
    if c.get("images"):
        info["unsupported"].append(f"{c['images']} image(s): not preserved by openpyxl")
    if c.get("pivots"):
        info["unsupported"].append(f"{c['pivots']} pivot table(s): pivot caches are not refreshed - refresh in Excel")
    if c.get("external_links"):
        info["unsupported"].append("External workbook links: not updated")
    if c.get("has_macros"):
        info["unsupported"].append("VBA macros present: never executed; removed from output")
    wb = openpyxl.load_workbook(path)  # formulas as strings
    for ws in wb.worksheets:
        s = {"name": ws.title, "dims": ws.dimensions, "formula_cells": 0, "input_tables": [], "label_cells": [],
             "merged": len(ws.merged_cells.ranges), "conditional_formats": len(ws.conditional_formatting), "data_validations": len(ws.data_validations.dataValidation)}
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    s["formula_cells"] += 1
                if isinstance(cell.value, str) and (re.search(r"reporting period|^period$|\{\{\w+\}\}", cell.value.strip(), re.I)):
                    s["label_cells"].append(cell.coordinate)
        r, prev_end = 1, 0
        while r <= min(ws.max_row, SCAN_ROWS):
            keys = _header_keys(ws, r)
            if _is_header(keys):
                t = _table(ws, r, keys, prev_end)
                s["input_tables"].append(t)
                prev_end = t["total_row"] or t["block_end"]
                r = max(r, prev_end) + 1
                continue
            r += 1
        info["sheets"].append(s)
    return info


def _months_between(a, b) -> set[int]:
    if not a or not b:
        return set()
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    if b < a:
        return set()
    out, cur = set(), pd.Timestamp(a.year, a.month, 1)
    while cur <= b and len(out) < 12:
        out.add(cur.month)
        cur = cur + pd.offsets.MonthBegin(1)
    return out


def _data_months(ctx: dict) -> set[int]:
    """Months the report's data actually covers (data window clipped to the reporting period)."""
    ps, pe, ds, de = (ctx.get(k) for k in ("period_start", "period_end", "data_start", "data_end"))
    if ds and de:
        a = max(pd.Timestamp(ds), pd.Timestamp(ps)) if ps else pd.Timestamp(ds)
        b = min(pd.Timestamp(de), pd.Timestamp(pe)) if pe else pd.Timestamp(de)
        m = _months_between(a, b)
        if m:
            return m
    return _months_between(ps, pe)


def _plat_key(label: str) -> str:
    l = re.sub(r"\s+", " ", str(label).strip().lower())
    return PLATFORM_ALIASES.get(l, l)


def _write_value(cell, k: str, r: dict | None):
    if k in F.FIELDS and F.FIELDS[k].kind == "dimension":
        v = None if r is None else r.get(k)
        cell.value = F.PLATFORM_LABELS.get(v, v) if k == "platform" else v
        return
    v = None if r is None else r.get(k)
    if k in ("ctr", "vtr"):
        v = None if v is None else v / 100.0
    cell.value = "N/A" if v is None else v


def _na_formulas(ws, row: int, report: dict):
    """A metric the exports do not have is written as N/A; a rate formula in the same row that divides by it
    would show #VALUE!, so it becomes N/A too - the convention these client templates already use."""
    na_cols = {ws.cell(row=row, column=c).column_letter for c in range(1, min(ws.max_column, 60) + 1)
               if ws.cell(row=row, column=c).value == "N/A"}
    if not na_cols:
        return
    for c in range(1, min(ws.max_column, 60) + 1):
        cell = ws.cell(row=row, column=c)
        if isinstance(cell, MergedCell) or not (isinstance(cell.value, str) and cell.value.startswith("=")):
            continue
        refs = set(re.findall(rf"(?<![A-Z$])\$?([A-Z]{{1,3}})\$?{row}(?!\d)", cell.value))
        if refs & na_cols:
            report.setdefault("na_formulas", []).append(f"{ws.title}!{cell.coordinate}")
            cell.value = "N/A"


def fill_template(template_path: str, out_path: str, calc, ctx: dict, workdir: str) -> dict:
    info = inspect_template(template_path)
    shutil.copy(template_path, out_path)  # never touch the original
    wb = openpyxl.load_workbook(out_path)
    report = {"inspection": info, "written": [], "skipped_formula_cells": 0, "labels": [], "unsupported": list(info["unsupported"]),
              "checks": [], "notes": [], "kept": []}
    cur_rows = calc.dataset[calc.dataset["_period"] == "current"]
    data_months = _data_months(ctx)
    row_ctx: dict[tuple[str, int], dict | None] = {}  # rows whose formulas are checked against a block subset (None = not checkable)
    month_names = {v: k.title() for k, v in MONTHS.items() if len(k) == 3}
    for s in info["sheets"]:
        ws = wb[s["name"]]
        for c in [c for c in s["label_cells"]]:
            cell = ws[c]
            if isinstance(cell, MergedCell):
                continue
            v = str(cell.value)
            if "{{" in v:
                cell.value = v.replace("{{period}}", ctx["period_label"]).replace("{{client}}", ctx["client"]).replace("{{currency}}", ctx.get("currency") or "").replace("{{brand}}", ctx.get("brand") or "")
                report["labels"].append(c)
            else:
                nxt = ws.cell(row=cell.row, column=cell.column + 1)
                if not isinstance(nxt, MergedCell) and not (isinstance(nxt.value, str) and nxt.value.startswith("=")):
                    nxt.value = ctx["period_label"]
                    report["labels"].append(nxt.coordinate)
        for t in s["input_tables"]:
            cols = {int(k): v for k, v in t["columns"].items()}
            dims = [v for v in cols.values() if v in F.FIELDS and F.FIELDS[v].kind == "dimension"]
            where = f"{s['name']}" + (f" / {t['title']}" if t.get("title") else "")
            # a multi-month workbook keeps one block per month: only the month(s) this data covers are written
            block_months = _months_in(t.get("title", "")) or _months_in(s["name"])
            span = range(t["header_row"] + 1, (t["total_row"] or t["block_end"]) + 1)
            if block_months and data_months and not (block_months & data_months):
                report["kept"].append(where)
                row_ctx.update({(s["name"], x): None for x in span})
                continue
            if block_months and data_months and not data_months <= block_months:
                report["notes"].append(f"Template block '{where}' covers {', '.join(month_names[m] for m in sorted(block_months))} but the data covers "
                                       f"{', '.join(month_names[m] for m in sorted(data_months))}; it was left as in the template because the "
                                       "exports cannot be split by month. Upload one month's exports to fill it.")
                row_ctx.update({(s["name"], x): None for x in span})
                continue
            rows_df = cur_rows
            market = _market_in(t.get("title", "")) or _market_in(s["name"])
            if market:
                if "country" not in cur_rows.columns or cur_rows["country"].isna().all():
                    report["notes"].append(f"Template block '{where}' is for {market}, but no market could be identified in the exports; it was left as in the template.")
                    row_ctx.update({(s["name"], x): None for x in span})
                    continue
                rows_df = cur_rows[cur_rows["country"] == market]
            if market or block_months:
                sub_tot = (aggregate(rows_df, []) or [None])[0]
                if t["total_row"]:
                    row_ctx[(s["name"], t["total_row"])] = sub_tot
            hr = t["header_row"]
            labels = {int(k): v for k, v in t.get("labels", {}).items()}
            if labels and len(dims) == 1:
                # labelled block (e.g. Meta / TikTok / Snapchat / Google rows above a TOTAL row): update matching rows in place
                dim = dims[0]
                agg = {(_plat_key(r.get(dim)) if dim == "platform" else str(r.get(dim)).strip().lower()): r for r in aggregate(rows_df, [dim])}
                by_label = {rr: (_plat_key(lab) if dim == "platform" else lab.strip().lower()) for rr, lab in labels.items()}
                written = 0
                for rr, key in by_label.items():
                    r = agg.get(key)
                    if market or block_months:
                        row_ctx[(s["name"], rr)] = r
                    if r is None:
                        continue
                    for c, k in cols.items():
                        if k in dims:
                            continue
                        cell = ws.cell(row=rr, column=c)
                        if isinstance(cell, MergedCell) or c in t["formula_cols"] or (isinstance(cell.value, str) and cell.value.startswith("=")):
                            report["skipped_formula_cells"] += 1
                            continue
                        _write_value(cell, k, r)
                    _na_formulas(ws, rr, report)
                    written += 1
                missing = [F.PLATFORM_LABELS.get(k, k) if dim == "platform" else k for k, r in agg.items() if k not in by_label.values() and (r.get("spend") or 0) > 0]
                if missing:
                    report["notes"].append(f"Template block '{where}' has no row for {', '.join(missing)}; that data is in the consolidated report only.")
                report["written"].append({"sheet": s["name"], "header_row": hr, "rows": written, "dims": dims, "block": t.get("title") or None,
                                          "metrics": [v for v in cols.values() if v not in dims]})
                continue
            rows = aggregate(rows_df, dims)
            rows.sort(key=lambda r: tuple(str(r.get(d)) for d in dims))
            end = t["block_end"]
            formula_templates = {c: ws.cell(row=hr + 1, column=c).value for c in t["formula_cols"]}
            # clear previous input values (not formulas) inside this table's block only
            for rr in range(hr + 1, end + 1):
                for c in cols:
                    cell = ws.cell(row=rr, column=c)
                    if isinstance(cell, MergedCell) or c in t["formula_cols"] or (isinstance(cell.value, str) and cell.value.startswith("=")):
                        continue
                    cell.value = None
            room = end - hr
            if len(rows) > room:
                report["notes"].append(f"Template block '{where}' has room for {room} rows but the data has {len(rows)}; only the first {room} were written.")
                rows = rows[:room]
            if market:
                row_ctx.update({(s["name"], x): None for x in range(hr + 1, end + 1)})
            for i, r in enumerate(rows):
                rr = hr + 1 + i
                for c, k in cols.items():
                    cell = ws.cell(row=rr, column=c)
                    if isinstance(cell, MergedCell):
                        continue
                    if c in t["formula_cols"]:
                        if i > 0 and formula_templates.get(c):
                            cell.value = Translator(formula_templates[c], origin=ws.cell(row=hr + 1, column=c).coordinate).translate_formula(cell.coordinate)
                            cell.number_format = ws.cell(row=hr + 1, column=c).number_format
                        report["skipped_formula_cells"] += 1
                        continue
                    if isinstance(cell.value, str) and str(cell.value).startswith("="):
                        report["skipped_formula_cells"] += 1
                        continue
                    _write_value(cell, k, r)
                    if i > 0:
                        cell._style = ws.cell(row=hr + 1, column=c)._style
            report["written"].append({"sheet": s["name"], "header_row": hr, "rows": len(rows), "dims": dims, "block": t.get("title") or None,
                                      "metrics": [v for v in cols.values() if v not in dims]})
    if report.get("na_formulas"):
        report["notes"].append(f"{len(report['na_formulas'])} rate formula(s) set to N/A because a figure they divide by is not in the exports: "
                               + ", ".join(report["na_formulas"][:10]))
    if report["kept"]:
        report["notes"].append(f"{len(report['kept'])} template block(s) for other months were kept exactly as supplied: " + "; ".join(report["kept"][:12])
                               + (" ..." if len(report["kept"]) > 12 else ""))
    wb.calculation.fullCalcOnLoad = True
    wb.save(out_path)
    # recalc + verify formulas
    try:
        rec = recalc_xlsx(out_path, workdir)
        report["recalculated_copy"] = rec
        report["checks"] = verify_formulas(rec, out_path, calc, row_ctx, [w["sheet"] for w in report["written"]])
    except RenderError as e:
        report["checks"] = [{"status": "not_run", "message": f"Formula recalculation could not run: {e}"}]
    return report


def verify_formulas(recalc_path: str, formula_path: str, calc, row_ctx: dict | None = None, written_sheets: list[str] | None = None) -> list[dict]:
    row_ctx = row_ctx or {}
    wf = openpyxl.load_workbook(formula_path)
    wv = openpyxl.load_workbook(recalc_path, data_only=True)
    checks = []
    errors = []
    matched = mismatched = unverified = 0
    plat_rows = {F.PLATFORM_LABELS.get(r["platform"], r["platform"]).lower(): r for r in (calc.tables.get("platform").rows if calc.tables.get("platform") else [])}
    plat_rows.update({r["platform"].lower(): r for r in (calc.tables.get("platform").rows if calc.tables.get("platform") else [])})
    written = {sh for sh, _ in row_ctx} | set(written_sheets or [])
    for ws in wf.worksheets:
        if ws.title not in wv.sheetnames:
            continue
        if written and ws.title not in written and not any(f"{w}!" in str(c.value) or f"'{w}'!" in str(c.value) for w in written
                                                          for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")):
            continue  # a sheet the fill never wrote to and that does not reference one: its formulas are the client's history
        wsv = wv[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                if row_ctx.get((ws.title, cell.row), 0) is None:  # a block kept as supplied (another month/market)
                    unverified += 1
                    continue
                val = wsv[cell.coordinate].value
                if isinstance(val, str) and val.strip() in ERRORS:
                    errors.append(f"{ws.title}!{cell.coordinate} = {val}")
                    continue
                label = next((ws.cell(row=cell.row, column=c).value for c in range(1, cell.column) if isinstance(ws.cell(row=cell.row, column=c).value, str)), None)
                header = next((ws.cell(row=r, column=cell.column).value for r in range(cell.row - 1, 0, -1) if isinstance(ws.cell(row=r, column=cell.column).value, str) and not str(ws.cell(row=r, column=cell.column).value).startswith("=")), None)
                metric = _hdr_key(header) if header else None
                ent = None
                if (ws.title, cell.row) in row_ctx:
                    ent = row_ctx[(ws.title, cell.row)]
                    label = None
                if label:
                    l = str(label).strip().lower()
                    ent = calc.totals if l in ("total", "grand total", "all platforms", "overall") else plat_rows.get(l)
                    if ent is None:  # row labels such as "Total spend" / "Blended CTR" describe an overall metric
                        mm = re.match(r"^(total|blended|overall|all[- ]platform)\s+(.+)$", l)
                        if mm and _hdr_key(mm.group(2)):
                            ent, metric = calc.totals, _hdr_key(mm.group(2))
                if ent is not None and metric and isinstance(val, (int, float)):
                    exp = ent.get(metric)
                    if exp is not None and metric in ("ctr", "vtr"):
                        exp = exp / 100.0
                    if exp is None:
                        unverified += 1
                    elif abs(float(val) - float(exp)) <= max(1e-6, abs(float(exp)) * 1e-9):
                        matched += 1
                    else:
                        mismatched += 1
                        errors.append(f"{ws.title}!{cell.coordinate} ({label} / {header}) = {val}, engine = {exp}")
                else:
                    unverified += 1
    checks.append({"status": "fail" if errors else "pass", "message": f"Formula recalculation: {matched} formula results reconciled to the engine, {mismatched} mismatched, {unverified} not mappable to an engine value; {len([e for e in errors if '=' in e and 'engine' not in e])} error values", "details": errors[:20]})
    return checks
