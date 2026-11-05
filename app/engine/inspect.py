"""File inspection: worksheets, header rows, platform, dates, currency, column mapping,
total/summary rows and metadata. Produces a JSON-serialisable inspection record per file."""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter

from . import fields as F
from .parsing import (FileRejected, RawSheet, check_ooxml, check_zip_of_images, classify_numeric_strings,
                      detect_date_format, is_missing, parse_date, parse_number, read_csv, read_xlsx)

SUMMARY_TOKENS = ("values", "grand total", "row labels", "column labels")
SUMMARY_PREFIX = ("sum of ", "count of ", "average of ", "max of ", "min of ")
# A total row label is the whole cell ("Total", "Grand total", "Total of 12 results") - NOT a name that
# merely starts with "Total" (real ATC creative: "Total-School-Later-Start").
TOTAL_RE = re.compile(r"^\s*(grand total|totals?|total of \d+ (results?|rows?|ads?)|summary|all)\s*[:.]?\s*$", re.I)
KEY_DIMS = ("campaign_name", "adset_name", "ad_name", "creative", "date", "campaign_id", "ad_id", "adset_id")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cell_str(v) -> str:
    return "" if v is None else str(v).replace("​", "").replace("\xa0", " ").strip()


def _row_blank(r) -> bool:
    return all(is_missing(v) for v in r)


def _score_header(row, platform=None) -> int:
    score = 0
    strs = 0
    for v in row:
        s = _cell_str(v)
        if not s:
            continue
        if isinstance(v, str):
            strs += 1
        m = F.match_header(s, platform)
        if m["status"] in ("mapped", "ignored"):
            score += 2 if m["status"] == "mapped" else 1
    return score * 10 + strs


def find_header_row(rows: list[list], max_scan: int = 30) -> int | None:
    best, best_i = 0, None
    for i, r in enumerate(rows[:max_scan]):
        s = _score_header(r)
        if s > best:
            best, best_i = s, i
    if best < 30:  # need at least ~2-3 recognised columns
        return None
    return best_i


def _looks_summary(row) -> bool:
    for v in row:
        s = _cell_str(v).lower()
        if s in SUMMARY_TOKENS or s.startswith(SUMMARY_PREFIX):
            return True
    return False


def inspect_sheet(sheet: RawSheet, filename: str, profile: dict | None = None) -> dict:
    profile = profile or {}
    rows = sheet.rows
    out: dict = {"name": sheet.name, "rows_total": len(rows), "truncated": sheet.truncated, "is_data_sheet": False,
                 "warnings": [], "excluded_rows": [], "total_rows": [], "summary_blocks": [], "metadata_lines": []}
    hi = find_header_row(rows)
    if hi is None:
        out["reason"] = "No recognisable header row in the first 30 rows"
        return out
    header = rows[hi]
    out["header_row"] = hi + 1
    out["metadata_lines"] = [" | ".join(_cell_str(v) for v in r if _cell_str(v)) for r in rows[:hi] if not _row_blank(r)][:20]
    meta_text = " ".join(out["metadata_lines"])
    norm_headers = [F.normalise_header(h)[0] for h in header]
    platform, conf, evidence = F.detect_platform(norm_headers, filename, meta_text)
    out.update(platform=platform, platform_label=F.PLATFORM_LABELS.get(platform, platform),
               platform_confidence=conf, platform_evidence=evidence)

    overrides = (profile.get("column_overrides") or {})
    cols = []
    for ci, h in enumerate(header):
        m = F.match_header(h, platform, overrides)
        m["index"] = ci
        cols.append(m)
    # duplicate canonical targets -> keep first, mark later as unresolved conflicts
    seen = {}
    for c in cols:
        if c["canonical"]:
            if c["canonical"] in seen:
                c["status"] = "unresolved"
                c["definition"] = f"Second column mapping to {c['canonical']} (first: '{seen[c['canonical']]}')"
                c["canonical"] = None
            else:
                seen[c["canonical"]] = c["original"]
    canon_idx = {c["canonical"]: c["index"] for c in cols if c["canonical"]}

    # ----- locate data block, total rows and trailing summary/pivot blocks -----
    data_idx: list[int] = []
    gap = False
    key_cols = [canon_idx[k] for k in KEY_DIMS if k in canon_idx]
    metric_cols = [canon_idx[k] for k in canon_idx if F.FIELDS[k].kind in ("additive", "nonadditive")]
    i = hi + 1
    while i < len(rows):
        r = rows[i] + [None] * (len(header) - len(rows[i]))
        if _row_blank(r):
            gap = True
            i += 1
            continue
        first_texts = [_cell_str(r[c]) for c in key_cols] or [_cell_str(r[0])]
        if _looks_summary(r) or (gap and key_cols and all(not t for t in first_texts)):
            out["summary_blocks"].append({"start": i + 1, "end": len(rows),
                                          "reason": "Manually added pivot/summary block below the export (e.g. 'Sum of ...', 'Grand Total') - excluded; it is not source data and may sum non-additive metrics"})
            break
        if any(TOTAL_RE.match(t) for t in first_texts if t) or TOTAL_RE.match(_cell_str(r[0]) or "x_"):
            out["total_rows"].append({"row": i + 1, "values": {F.FIELDS[k].label: r[canon_idx[k]] for k in canon_idx
                                                                if F.FIELDS[k].kind == "additive" and not is_missing(r[canon_idx[k]])}})
            out["excluded_rows"].append({"row": i + 1, "reason": "Platform total row"})
            i += 1
            continue
        if key_cols and all(not t for t in first_texts) and any(not is_missing(r[c]) for c in metric_cols):
            out["total_rows"].append({"row": i + 1, "values": {F.FIELDS[k].label: r[canon_idx[k]] for k in canon_idx
                                                                if F.FIELDS[k].kind == "additive" and not is_missing(r[canon_idx[k]])}})
            out["excluded_rows"].append({"row": i + 1, "reason": "Row has metrics but no campaign/ad identifiers (treated as a total row)"})
            i += 1
            continue
        if gap:
            out["warnings"].append(f"Data continues after a blank row at row {i + 1}")
            gap = False
        data_idx.append(i)
        i += 1

    out["data_rows"] = [data_idx[0] + 1, data_idx[-1] + 1] if data_idx else None
    out["row_count"] = len(data_idx)
    data = [rows[j] + [None] * (len(header) - len(rows[j])) for j in data_idx]

    # ----- per-column profiling -----
    for c in cols:
        vals = [r[c["index"]] for r in data]
        nonnull = [v for v in vals if not is_missing(v)]
        c["sample"] = [_cell_str(v)[:60] for v in nonnull[:3]]
        c["null_count"] = len(vals) - len(nonnull)
        key = c["canonical"]
        if key and F.FIELDS[key].kind in ("additive", "nonadditive", "rate"):
            fmt = classify_numeric_strings(nonnull)
            c["numeric_format"] = fmt
            bad = 0
            zeros = 0
            for v in nonnull:
                try:
                    x = parse_number(v, "us" if fmt == "us_or_eu_thousands" else fmt)
                    zeros += (x == 0)
                except ValueError:
                    bad += 1
            c["zero_count"] = zeros
            c["unparseable"] = bad
            if bad:
                out["warnings"].append(f"Column '{c['original']}' has {bad} non-numeric values")
        elif not key and c["status"] == "unresolved":
            # unknown column - numeric?
            nums = 0
            for v in nonnull:
                try:
                    parse_number(v)
                    nums += 1
                except ValueError:
                    pass
            c["looks_numeric"] = bool(nonnull) and nums == len(nonnull)
    out["columns"] = cols

    # ----- currency -----
    cur = None
    src = None
    hdr_curs = {c["currency"] for c in cols if c.get("currency")}
    if "currency" in canon_idx:
        vals = {_cell_str(r[canon_idx["currency"]]).upper() for r in data if _cell_str(r[canon_idx["currency"]])}
        if len(vals) == 1:
            cur, src = vals.pop(), "currency column"
        elif len(vals) > 1:
            cur, src = "MIXED:" + ",".join(sorted(vals)), "currency column (multiple values)"
    if not cur and hdr_curs:
        if len(hdr_curs) == 1:
            cur, src = next(iter(hdr_curs)), "column header"
        else:
            cur, src = "MIXED:" + ",".join(sorted(hdr_curs)), "column headers"
    if not cur:
        m = re.search(r"currency[:\s]+([A-Z]{3})\b", meta_text, re.I)
        if m:
            cur, src = m.group(1).upper(), "file metadata"
    out["currency"] = {"value": cur, "source": src}

    # ----- timezone -----
    tz = None
    m = re.search(r"(time ?zone[:\s]+[^|]+|\(?GMT[+-]\d{1,2}(:\d{2})?\)?[^|]*|UTC[+-]\d{1,2}(:\d{2})?)", meta_text, re.I)
    if m:
        tz = m.group(0).strip()
    out["timezone"] = tz

    # ----- dates -----
    date_info = {"granularity": None, "min": None, "max": None, "format": None, "ambiguous": False}
    for key in ("date", "report_start", "report_end"):
        if key in canon_idx:
            vals = [r[canon_idx[key]] for r in data]
            d = detect_date_format(vals)
            date_info[f"{key}_format"] = d
            if d["ambiguous"]:
                date_info["ambiguous"] = True
            parsed = []
            for v in vals:
                try:
                    p = parse_date(v, d["format"])
                    if p:
                        parsed.append(p)
                except ValueError:
                    pass
            if parsed:
                lo, hi_ = min(parsed), max(parsed)
                if key == "date":
                    date_info["granularity"] = "daily" if len(set(parsed)) > 1 else "single day"
                    date_info["min"], date_info["max"] = str(lo), str(hi_)
                elif key == "report_start":
                    date_info["min"] = str(lo) if not date_info["min"] else date_info["min"]
                    date_info.setdefault("granularity", None)
                    if not date_info["granularity"]:
                        date_info["granularity"] = "aggregated over reporting window"
                else:
                    if not date_info["max"] or "date" not in canon_idx:
                        date_info["max"] = str(hi_)
    if not date_info["min"]:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:-|to|–)\s*(\d{4}-\d{2}-\d{2})", meta_text)
        if m:
            date_info.update(min=m.group(1), max=m.group(2), granularity="aggregated (from file metadata)")
    out["dates"] = date_info

    out["dimensions_found"] = [k for k in canon_idx if F.FIELDS[k].kind == "dimension"]
    out["metrics_found"] = [k for k in canon_idx if F.FIELDS[k].kind != "dimension"]
    out["unresolved"] = [c["original"] for c in cols if c["status"] == "unresolved" and c.get("looks_numeric", True)]
    out["is_data_sheet"] = bool(data_idx) and any(F.FIELDS[k].kind == "additive" for k in canon_idx)
    if "results" in canon_idx:
        if "result_indicator" in canon_idx:
            inds = Counter(_cell_str(r[canon_idx["result_indicator"]]) for r in data if _cell_str(r[canon_idx["result_indicator"]]))
            out["result_indicators"] = dict(inds)
        else:
            out["warnings"].append("'Results' column has no Result indicator - its meaning cannot be determined; it will not be aggregated")
    return out


def suggest_role(name: str, ext: str, sheets_info: list[dict] | None, formula_cells: int = 0) -> str:
    lname = name.lower()
    if ext == ".pptx":
        return "client_deck"
    if ext == ".zip":
        return "creatives"
    if ext == ".xlsx" and ("template" in lname or (formula_cells > 0 and not any(s.get("row_count", 0) > 0 for s in sheets_info or []))):
        return "template"
    return "raw"


def inspect_file(path: str, original_name: str, profile: dict | None = None) -> dict:
    ext = os.path.splitext(original_name)[1].lower()
    info: dict = {"name": original_name, "ext": ext, "size": os.path.getsize(path), "sha256": sha256_file(path),
                  "sheets": [], "warnings": [], "ok": True}
    try:
        if ext == ".csv":
            sheets = read_csv(path)
            info["kind"] = "csv"
        elif ext == ".xlsx":
            info["container"] = check_ooxml(path, "xlsx")
            sheets = read_xlsx(path, include_formulas=True)
            info["kind"] = "xlsx"
            if info["container"]["has_macros"]:
                info["warnings"].append("Workbook contains a VBA project; macros are never executed and are ignored")
        elif ext == ".pptx":
            info["container"] = check_ooxml(path, "pptx")
            info["kind"] = "pptx"
            from .pptx_inspect import inspect_deck
            info["deck"] = inspect_deck(path)
            info["role_suggested"] = "client_deck"
            return info
        elif ext == ".zip":
            info["kind"] = "zip"
            info["images"] = check_zip_of_images(path)
            info["role_suggested"] = "creatives"
            return info
        elif ext in (".xlsm", ".xls", ".pptm", ".ppt"):
            raise FileRejected(f"{ext} files are not accepted. Save as .xlsx/.pptx (macro-free) and re-upload.")
        else:
            raise FileRejected(f"Unsupported file type '{ext}'. Accepted: CSV, XLSX, PPTX, ZIP of images.")
    except FileRejected as e:
        info.update(ok=False, error=str(e))
        return info
    formula_cells = sum(s.formula_cells for s in sheets)
    info["formula_cells"] = formula_cells
    for s in sheets:
        si = inspect_sheet(s, original_name, profile)
        si["formula_cells"] = s.formula_cells
        info["sheets"].append(si)
    info["role_suggested"] = suggest_role(original_name, ext, info["sheets"], formula_cells)
    data_sheets = [s for s in info["sheets"] if s["is_data_sheet"]]
    if info["role_suggested"] == "raw" and not data_sheets:
        info["warnings"].append("No worksheet with recognisable reporting data was found")
    return info
