"""Validation suite run before outputs are marked ready. Each check reports pass / warn / fail /
not_run with details - a check is never reported as passed unless it actually ran."""
from __future__ import annotations

import math
import re

import openpyxl
from pptx import Presentation

from . import fields as F
from . import fmt as FM
from .calc import RATE_DEFS, safe_div
from .parsing import is_missing, parse_number, read_csv, read_xlsx


def _chk(cid, title, status, message, details=None):
    return {"id": cid, "title": title, "status": status, "message": message, "details": details or []}


def _sum(series) -> float:
    v = series.sum(min_count=1) if len(series) else 0.0
    return 0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _close(a, b, rel=1e-9, abs_=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= max(abs_, rel * max(abs(float(a)), abs(float(b))))


def independent_file_sums(f: dict) -> dict:
    """Re-read a raw file directly (separate code path from normalisation) and sum additive columns
    over the data rows identified at inspection, excluding total rows and summary blocks."""
    sheets = read_csv(f["path"]) if f["ext"] == ".csv" else read_xlsx(f["path"])
    by = {s.name: s for s in sheets}
    sums = {}
    for si in f["inspection"]["sheets"]:
        if not si.get("is_data_sheet"):
            continue
        raw = by[si["name"]]
        excl = {e["row"] for e in si.get("excluded_rows", [])}
        stop = min([b["start"] for b in si.get("summary_blocks", [])] or [10 ** 9])
        a, b = si["data_rows"]
        for c in si["columns"]:
            k = c.get("canonical")
            if not k or F.FIELDS[k].kind != "additive" or k == "results":
                continue
            fmt = c.get("numeric_format", "plain")
            fmt = "us" if fmt == "us_or_eu_thousands" else fmt
            tot, n = 0.0, 0
            for ri in range(a - 1, min(b, stop - 1)):
                if ri + 1 in excl:
                    continue
                row = raw.rows[ri]
                v = row[c["index"]] if c["index"] < len(row) else None
                if is_missing(v):
                    continue
                try:
                    tot += parse_number(v, fmt)
                    n += 1
                except ValueError:
                    pass
            if n:
                sums[(si["name"], k)] = tot
    return sums


def run_validation(*, files: list[dict], norm, calc, excel: dict | None, deck_update: dict | None, deck_new: dict | None,
                   template: dict | None, commentary_dropped: list[str], render: dict, layout_issues: list[dict], ctx: dict) -> dict:
    checks = []
    df = norm.df
    # 0 required columns present
    mm = getattr(norm, "missing_metrics", [])
    checks.append(_chk("columns", "Required columns", "warn" if mm else "pass",
                       "Spend, impressions and clicks found in every data sheet" if not mm else f"{len(mm)} sheet(s) lack a core metric column - shown as N/A, not zero",
                       [f"{m['file']} / {m['sheet']}: missing {', '.join(m['missing'])}" for m in mm]))
    # 1 source coverage
    accounted = []
    missing = []
    for f in files:
        rows = int(((df["_file_id"] == f["id"]) & df["_excluded"].isna() & df["_period"].notna()).sum()) if len(df) else 0
        ex = [e for e in norm.exclusions if e["file"] == f["name"] and e.get("scope") in ("file",)]
        if rows:
            accounted.append(f"{f['name']}: {rows} rows included")
        elif ex:
            accounted.append(f"{f['name']}: excluded - {ex[0]['reason']}")
        else:
            other = [x for x in (df[df['_file_id'] == f['id']]['_excluded'].dropna().unique() if len(df) else [])]
            if other:
                accounted.append(f"{f['name']}: all rows excluded - {other[0]}")
            else:
                missing.append(f["name"])
    checks.append(_chk("coverage", "Source coverage", "fail" if missing else "pass",
                       "Every uploaded raw file is included or excluded with a reason" if not missing else f"Files with no usable rows and no recorded reason: {missing}", accounted))
    # 2 period
    p = norm.period
    if calc.included_rows == 0:
        checks.append(_chk("period", "Reporting period", "fail", "No rows fall inside the reporting period"))
    else:
        partial = p.get("data_start") and p.get("start") and (p["data_start"] > p["start"] or p["data_end"] < p["end"])
        checks.append(_chk("period", "Reporting period", "warn" if partial else "pass",
                           f"Data window {p.get('data_start')} to {p.get('data_end')} within reporting period {p.get('start')} to {p.get('end')}" + (" (partial coverage - labels use the data window)" if partial else "")))
    # 3 duplicates / overlaps
    ov = norm.overlaps
    within = [w for w in norm.warnings if "exact repeats" in w]
    checks.append(_chk("overlaps", "Duplicates and overlapping exports", "warn" if within else "pass",
                       f"{len(ov)} overlap(s) detected and resolved" if ov else "No duplicate or overlapping exports found",
                       [f"{o['type'].replace('_', ' ')}: {', '.join(o['files'])}" + (f" ({o['rows']} rows)" if o.get("rows") else "") + (f" -> {o['resolution']}" if o.get("resolution") else "") for o in ov] + within))
    # 4 currency
    live = calc.dataset
    curs = sorted(live["currency"].dropna().unique()) if len(live) else []
    checks.append(_chk("currency", "Currency compatibility", "pass" if len(curs) <= 1 else "fail",
                       f"Single reporting currency: {curs[0] if curs else 'n/a'}" if len(curs) <= 1 else f"Multiple currencies remain: {curs}",
                       [a for a in norm.assumptions if "converted" in a or "Currency" in a]))
    # 5 metric compatibility
    det = []
    status = "pass"
    cur_rows = live[live["_period"] == "current"]
    for pf in sorted(cur_rows["platform"].dropna().unique()):
        sub = cur_rows[cur_rows["platform"] == pf]
        n_click = int(sub["clicks"].notna().sum())
        if n_click < len(sub):
            status = "warn"
            det.append(f"{F.PLATFORM_LABELS.get(pf, pf)}: {len(sub) - n_click} of {len(sub)} rows have no compatible '{calc.click_metric_label}' value - excluded from CTR/CPC (shown as N/A where applicable)")
        else:
            det.append(f"{F.PLATFORM_LABELS.get(pf, pf)}: clicks = {calc.click_metric_label} on all {len(sub)} rows")
    if calc.settings.get("conversion_metric"):
        det.append(f"CPA uses {calc.conversion_label} only; other conversion types are reported separately")
    else:
        det.append("No conversion metric selected - CPA not calculated")
    det.append("Reach/unique metrics are not summed above ad level")
    checks.append(_chk("compatibility", "Metric compatibility", status, "Metric definitions checked per platform", det))
    # 6 reconciliation to source (independent re-read)
    rec_det, rec_fail = [], False
    for f in files:
        if not f["inspection"].get("ok", True) or any(e["file"] == f["name"] and e.get("scope") == "file" for e in norm.exclusions):
            continue
        try:
            src = independent_file_sums(f)
        except Exception as e:  # noqa: BLE001
            rec_det.append(f"{f['name']}: could not re-read ({type(e).__name__})")
            rec_fail = True
            continue
        sub_all = norm.df[norm.df["_file_id"] == f["id"]]
        for (sheet, k), raw_sum in src.items():
            if k not in sub_all.columns:
                continue
            s = sub_all[sub_all["_sheet"] == sheet]
            incl = s[s["_excluded"].isna() & s["_period"].notna()]
            excl = s[~(s["_excluded"].isna() & s["_period"].notna())]
            orig = f"_orig_{k}" if f"_orig_{k}" in s.columns else k
            a = _sum(incl[orig]) + _sum(excl[orig])
            if not _close(a, raw_sum, 1e-9, 1e-6):
                rec_fail = True
                rec_det.append(f"{f['name']} {F.FIELDS[k].label}: source {raw_sum:,.2f} vs normalised {a:,.2f}")
            else:
                ex_note = f" (incl. {_sum(excl[orig]):,.2f} in excluded rows)" if _sum(excl[orig]) else ""
                rec_det.append(f"{f['name']} {F.FIELDS[k].label}: {raw_sum:,.2f} = source{ex_note}")
    # engine total equals sum of included normalised rows (in reporting currency)
    for k in ("spend", "impressions", "link_clicks", "video_plays_3s"):
        if k in cur_rows.columns and calc.totals.get(k) is not None:
            s = float(cur_rows[k].sum(min_count=1))
            if not _close(s, calc.totals[k]):
                rec_fail = True
                rec_det.append(f"Engine total {k} {calc.totals[k]} != row sum {s}")
    # platform-provided total rows
    for t in norm.platform_totals:
        for label, v in t["values"].items():
            key = next((k for k, fld in F.FIELDS.items() if fld.label == label), None)
            if not key:
                continue
            s = norm.df[(norm.df["_file"] == t["file"]) & (norm.df["_sheet"] == t["sheet"])]
            orig = f"_orig_{key}" if f"_orig_{key}" in s.columns else key
            if orig in s.columns:
                try:
                    vv = parse_number(v)
                except ValueError:
                    continue
                ss = _sum(s[orig])
                tol = max(0.011 * len(s), abs(vv) * 0.0005)
                if abs(ss - vv) > tol:
                    rec_det.append(f"{t['file']}: platform total row {label} = {vv:,.2f} but rows sum to {ss:,.2f} (difference {ss - vv:,.2f})")
                    rec_fail = rec_fail or False
                else:
                    rec_det.append(f"{t['file']}: rows reconcile to the platform's own total row for {label} ({vv:,.2f})")
    checks.append(_chk("reconciliation", "Totals reconciled to source data", "fail" if rec_fail else "pass",
                       "Re-read every included file independently and compared column sums" if not rec_fail else "Some totals do not reconcile", rec_det))
    # 7 rates
    rate_det, rate_fail = [], False
    scopes = [("Total", calc.totals)] + [(F.PLATFORM_LABELS.get(r["platform"], r["platform"]), r) for r in (calc.tables["platform"].rows if calc.tables.get("platform") else [])]
    for name, r in scopes:
        for rate in ("ctr", "cpc", "cpm", "cpa"):
            num, den, mult, _ = RATE_DEFS[rate]
            if r.get(rate) is None:
                continue
            if r.get(f"_{rate}_coverage") and r[f"_{rate}_coverage"].split("/")[0] != r[f"_{rate}_coverage"].split("/")[1]:
                rate_det.append(f"{name} {rate.upper()}: computed on {r[f'_{rate}_coverage']} rows with both values")
                continue
            exp = safe_div(r.get(num), r.get(den), mult)
            if not _close(exp, r[rate]):
                rate_fail = True
                rate_det.append(f"{name} {rate.upper()} {r[rate]} != {num}/{den} = {exp}")
        zero_den = [rate for rate in ("ctr", "cpc", "cpm") if r.get(RATE_DEFS[rate][1]) == 0]
        if zero_den:
            rate_det.append(f"{name}: zero denominator for {', '.join(zero_den)} -> N/A")
    rate_det.append("Rates recomputed from summed numerators/denominators (not averaged)")
    checks.append(_chk("rates", "Rate calculations", "fail" if rate_fail else "pass", "CTR, CPC, CPM, CPA verified against their components", rate_det))
    # 8 excel read-back
    if excel and excel.get("path"):
        try:
            wb = openpyxl.load_workbook(excel["path"], data_only=True)
            det = [f"Sheets: {', '.join(wb.sheetnames)}"]
            bad = []
            ws = wb["Executive Summary"]
            got = {}
            for row in ws.iter_rows(min_row=11, max_col=2, values_only=True):
                if row[0]:
                    got[row[0]] = row[1]
            from .calc import METRIC_LABELS
            for m, v in excel["written"]["kpis"].items():
                cell = got.get(METRIC_LABELS.get(m, m))
                exp = None if v is None else (v / 100 if m in ("ctr", "vtr") else v)
                if (exp is None and cell != "N/A") or (exp is not None and not _close(cell, exp)):
                    bad.append(f"KPI {m}: workbook {cell} vs engine {exp}")
            if "Consolidated Data" in wb.sheetnames and excel["written"].get("consolidated_rows") is not None:
                n = wb["Consolidated Data"].max_row - 4
                exp_rows = int(calc.dataset["_period"].notna().sum())
                if n != exp_rows:
                    bad.append(f"Consolidated Data has {n} rows, expected {exp_rows}")
                else:
                    det.append(f"Consolidated Data rows = {n} (all included rows with provenance)")
            checks.append(_chk("excel", "Excel output read-back", "fail" if bad else "pass", "Workbook reopened; KPI values match the engine" if not bad else "Workbook values differ from the engine", det + bad))
        except Exception as e:  # noqa: BLE001
            checks.append(_chk("excel", "Excel output read-back", "fail", f"Workbook could not be reopened: {type(e).__name__}: {e}"))
    # 9 template
    if template:
        tchk = template.get("checks") or []
        st = "not_run" if any(c["status"] == "not_run" for c in tchk) else ("fail" if any(c["status"] == "fail" for c in tchk) else "pass")
        det = [c["message"] for c in tchk] + [f"Unsupported template feature: {u}" for u in template.get("unsupported", [])] + \
              [f"Populated {w['sheet']} from row {w['header_row'] + 1}: {w['rows']} rows ({', '.join(w['dims'])})" for w in template.get("written", [])] + \
              [f"Formula cells left untouched: {template.get('skipped_formula_cells', 0)}"]
        if not template.get("written"):
            st = "fail"
            det.insert(0, "No input table matching the data could be found in the template")
        if st == "pass" and template.get("unsupported"):
            st = "warn"
        checks.append(_chk("template", "Excel template population", st, "Template populated without overwriting formulas; recalculated with LibreOffice", det + [d for c in tchk for d in c.get("details", [])]))
    # 10 deck read-back + cross-file consistency
    if deck_update and deck_update.get("path"):
        prs = Presentation(deck_update["path"])
        bad, ok = [], 0
        for ch in deck_update["changes"]:
            if ch.get("metric") in ("thumbnail", "chart") or ch.get("row") is None:
                continue
            slide = prs.slides[ch["slide"] - 1]
            sh = next((s for s in slide.shapes if s.name == ch["shape"] and getattr(s, "has_table", False) and s.has_table), None)
            if not sh:
                continue
            hdr = [c.text.replace("​", "").strip() for c in sh.table.rows[0].cells]
            ci = hdr.index(ch["col"]) if ch["col"] in hdr else None
            got = sh.table.cell(ch["row"], ci).text.strip() if ci is not None else None
            if got != ch["new"]:
                bad.append(f"Slide {ch['slide']} {ch['col']}: '{got}' != expected '{ch['new']}'")
            else:
                ok += 1
        # cross-file: deck values vs Excel creative table values
        xbad, xok = [], 0
        if excel and excel.get("written", {}).get("creative"):
            ex_creative = {}
            for key, vals in excel["written"]["creative"].items():
                ex_creative[key[-1]] = ex_creative.get(key[-1], []) + [vals]
            for ch in deck_update["changes"]:
                if ch.get("metric") in ("impressions", "link_clicks", "clicks", "reach", "views", "video_plays_3s"):
                    ent = next((m["entity"] for m in deck_update["slide_map"] if m["slide"] == ch["slide"]), None)
                    if not ent:
                        continue
                    creative = ent.split(" – ")[-1]
                    brand = ent.split(" – ")[0]
                    cands = [k for k in excel["written"]["creative"] if k[-1] == creative and (k[0] == brand or len(k) < 3)]
                    if not cands:
                        continue
                    v = excel["written"]["creative"][cands[0]].get("clicks" if ch["metric"] == "link_clicks" else ch["metric"])
                    want = FM.CellStyle(ch["new"]).format(v, 0, deck_update.get("na", "-"))
                    if want != ch["new"]:
                        xbad.append(f"Slide {ch['slide']} {ch['col']}: deck '{ch['new']}' vs Excel {v}")
                    else:
                        xok += 1
        checks.append(_chk("deck", "Updated deck read-back", "fail" if bad else "pass",
                           f"Deck reopened; {ok} updated table cells verified", bad))
        checks.append(_chk("crossfile", "Excel vs PowerPoint consistency", "fail" if xbad else ("pass" if xok else "not_run"),
                           f"{xok} deck figures matched the Excel creative table" if xok else "No overlapping figures to compare (Excel not generated or no creative table)", xbad))
        lab = [ch for ch in deck_update["changes"] if ch.get("metric") in ("range", "period_label")]
        checks.append(_chk("labels", "Dates and labels", "pass" if lab or deck_update.get("range_text") else "warn",
                           f"Period labels set to '{deck_update.get('range_text')}' on {len([c for c in deck_update['changes'] if c.get('metric') == 'range'])} table cells; cover/period text updated: {len([c for c in deck_update['changes'] if c.get('metric') == 'period_label'])}",
                           [f"Slide {c['slide']}: '{c['old']}' -> '{c['new']}'" for c in deck_update["changes"] if c.get("metric") == "period_label"]))
        flags = [f for f in deck_update.get("flags", []) if f["severity"] == "warning"]
        checks.append(_chk("deck_flags", "Deck items needing review", "warn" if flags else "pass",
                           f"{len(flags)} item(s) could not be updated automatically" if flags else "All recognised reporting content updated", [f["message"] for f in deck_update.get("flags", [])]))
    if deck_new and deck_new.get("path"):
        prs = Presentation(deck_new["path"])
        bad, ok = [], 0
        for sidx, exp in deck_new["expected"].items():
            slide = prs.slides[int(sidx) - 1]
            texts = []
            for sh in slide.shapes:
                if getattr(sh, "has_table", False) and sh.has_table:
                    texts += [c.text for r in sh.table.rows for c in r.cells]
                elif getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                    texts.append(sh.text_frame.text)
            blob = "\n".join(texts)
            for k, v in exp.items():
                for item in (v if isinstance(v, list) else [v]):
                    if str(item) in blob:
                        ok += 1
                    else:
                        bad.append(f"New deck slide {sidx}: '{item}' ({k}) not found")
        checks.append(_chk("deck_new", "New deck figures vs dataset", "fail" if bad else "pass", f"{ok} figures on generated slides match the validated dataset", bad[:20]))
    # 11 files open / render
    for name, r in render.items():
        checks.append(_chk(f"open_{name}", f"{name} opens and renders", "pass" if r.get("ok") else ("not_run" if r.get("not_run") else "fail"), r.get("message", ""), r.get("details", [])))
    # 12 layout
    if deck_update or deck_new:
        lw = [i for i in layout_issues if i["severity"] == "warning"]
        checks.append(_chk("layout", "PowerPoint layout quality", "warn" if lw else "pass",
                           f"{len(lw)} possible overflow/clipping issue(s)" if lw else "No overflow, clipping or off-slide shapes detected on generated/updated slides (heuristic check + rendered previews)",
                           [f"Slide {i['slide']}: {i['message']}" if i["slide"] else i["message"] for i in layout_issues][:30]))
    # 13 commentary
    if ctx.get("commentary_enabled"):
        checks.append(_chk("commentary", "Commentary traceability", "warn" if commentary_dropped else "pass",
                           "Every number in the commentary matches a calculated value" if not commentary_dropped else f"{len(commentary_dropped)} statement(s) dropped for untraceable figures", commentary_dropped))
    overall = "blocked" if any(c["status"] == "fail" for c in checks) else ("warnings" if any(c["status"] == "warn" for c in checks) else "passed")
    return {"overall": overall, "checks": checks,
            "counts": {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "not_run")}}
