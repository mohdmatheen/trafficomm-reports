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
from openpyxl.formula.translate import Translator

from . import fields as F
from . import fmt as FM
from .calc import aggregate
from .parsing import check_ooxml
from .render import RenderError, recalc_xlsx

ERRORS = ("#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A", "#NUM!", "#NULL!")
DERIVED_HEADERS = {"ctr": "ctr", "cpc": "cpc", "cpm": "cpm", "cpa": "cpa", "cpl": "cpa", "vtr": "vtr", "roas": "roas", "clicks": "clicks", "conversions": "conversions", "leads": "conversions"}


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
        for r in range(1, min(ws.max_row, 30) + 1):
            keys = {}
            for cidx in range(1, min(ws.max_column, 60) + 1):
                k = _hdr_key(ws.cell(row=r, column=cidx).value)
                if k:
                    keys[cidx] = k
            dims = [k for k in keys.values() if k in F.FIELDS and F.FIELDS[k].kind == "dimension"]
            mets = [k for k in keys.values() if not (k in F.FIELDS and F.FIELDS[k].kind == "dimension")]
            if len(keys) >= 3 and dims and mets:
                below = [ws.cell(row=r + 1, column=c).value for c in keys]
                formula_cols = [c for c in keys if isinstance(ws.cell(row=r + 1, column=c).value, str) and str(ws.cell(row=r + 1, column=c).value).startswith("=")]
                s["input_tables"].append({"header_row": r, "columns": {str(c): k for c, k in keys.items()}, "formula_cols": formula_cols,
                                          "prefilled_rows": sum(1 for rr in range(r + 1, ws.max_row + 1) if any(ws.cell(row=rr, column=c).value not in (None, "") for c in keys if c not in formula_cols))})
                break
        info["sheets"].append(s)
    return info


def fill_template(template_path: str, out_path: str, calc, ctx: dict, workdir: str) -> dict:
    info = inspect_template(template_path)
    shutil.copy(template_path, out_path)  # never touch the original
    wb = openpyxl.load_workbook(out_path)
    report = {"inspection": info, "written": [], "skipped_formula_cells": 0, "labels": [], "unsupported": list(info["unsupported"]), "checks": []}
    cur_rows = calc.dataset[calc.dataset["_period"] == "current"]
    for s in info["sheets"]:
        ws = wb[s["name"]]
        for c in [c for c in s["label_cells"]]:
            cell = ws[c]
            v = str(cell.value)
            if "{{" in v:
                cell.value = v.replace("{{period}}", ctx["period_label"]).replace("{{client}}", ctx["client"]).replace("{{currency}}", ctx.get("currency") or "").replace("{{brand}}", ctx.get("brand") or "")
                report["labels"].append(c)
            else:
                nxt = ws.cell(row=cell.row, column=cell.column + 1)
                if not (isinstance(nxt.value, str) and nxt.value.startswith("=")):
                    nxt.value = ctx["period_label"]
                    report["labels"].append(nxt.coordinate)
        for t in s["input_tables"]:
            cols = {int(k): v for k, v in t["columns"].items()}
            dims = [v for v in cols.values() if v in F.FIELDS and F.FIELDS[v].kind == "dimension"]
            rows = aggregate(cur_rows, dims)
            rows.sort(key=lambda r: tuple(str(r.get(d)) for d in dims))
            hr = t["header_row"]
            formula_templates = {c: ws.cell(row=hr + 1, column=c).value for c in t["formula_cols"]}
            # clear previous input values (not formulas) in the input range
            for rr in range(hr + 1, ws.max_row + 1):
                for c in cols:
                    v = ws.cell(row=rr, column=c).value
                    if c in t["formula_cols"] or (isinstance(v, str) and v.startswith("=")):
                        continue
                    ws.cell(row=rr, column=c).value = None
            for i, r in enumerate(rows):
                rr = hr + 1 + i
                for c, k in cols.items():
                    cell = ws.cell(row=rr, column=c)
                    if c in t["formula_cols"]:
                        if i > 0 and formula_templates.get(c):
                            cell.value = Translator(formula_templates[c], origin=ws.cell(row=hr + 1, column=c).coordinate).translate_formula(cell.coordinate)
                            cell.number_format = ws.cell(row=hr + 1, column=c).number_format
                        report["skipped_formula_cells"] += 1
                        continue
                    if isinstance(cell.value, str) and str(cell.value).startswith("="):
                        report["skipped_formula_cells"] += 1
                        continue
                    if k in F.FIELDS and F.FIELDS[k].kind == "dimension":
                        v = r.get(k)
                        cell.value = F.PLATFORM_LABELS.get(v, v) if k == "platform" else v
                    else:
                        v = r.get(k)
                        if k in ("ctr", "vtr"):
                            v = None if v is None else v / 100.0
                        cell.value = "N/A" if v is None else v
                    if i > 0:
                        cell._style = ws.cell(row=hr + 1, column=c)._style
            report["written"].append({"sheet": s["name"], "header_row": hr, "rows": len(rows), "dims": dims,
                                      "metrics": [v for v in cols.values() if v not in dims]})
    wb.calculation.fullCalcOnLoad = True
    wb.save(out_path)
    # recalc + verify formulas
    try:
        rec = recalc_xlsx(out_path, workdir)
        report["recalculated_copy"] = rec
        report["checks"] = verify_formulas(rec, out_path, calc)
    except RenderError as e:
        report["checks"] = [{"status": "not_run", "message": f"Formula recalculation could not run: {e}"}]
    return report


def verify_formulas(recalc_path: str, formula_path: str, calc) -> list[dict]:
    wf = openpyxl.load_workbook(formula_path)
    wv = openpyxl.load_workbook(recalc_path, data_only=True)
    checks = []
    errors = []
    matched = mismatched = unverified = 0
    plat_rows = {F.PLATFORM_LABELS.get(r["platform"], r["platform"]).lower(): r for r in (calc.tables.get("platform").rows if calc.tables.get("platform") else [])}
    plat_rows.update({r["platform"].lower(): r for r in (calc.tables.get("platform").rows if calc.tables.get("platform") else [])})
    for ws in wf.worksheets:
        if ws.title not in wv.sheetnames:
            continue
        wsv = wv[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                val = wsv[cell.coordinate].value
                if isinstance(val, str) and val.strip() in ERRORS:
                    errors.append(f"{ws.title}!{cell.coordinate} = {val}")
                    continue
                label = next((ws.cell(row=cell.row, column=c).value for c in range(1, cell.column) if isinstance(ws.cell(row=cell.row, column=c).value, str)), None)
                header = next((ws.cell(row=r, column=cell.column).value for r in range(cell.row - 1, 0, -1) if isinstance(ws.cell(row=r, column=cell.column).value, str) and not str(ws.cell(row=r, column=cell.column).value).startswith("=")), None)
                metric = _hdr_key(header) if header else None
                ent = None
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
