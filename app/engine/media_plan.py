"""Client media plans: read booked figures (budget, impressions, reach, views, clicks...) and compare
them with what was delivered.

Reading is hybrid, like the instructions step:
* a layout read before for this client (saved per layout fingerprint) is reused without Claude;
* otherwise the built-in rules look for the plan table (header row, columns, total row);
* when the rules can't read the layout confidently, Claude is asked only *where* things are (which
  sheet, which header row, which column holds which field). Claude never reads or returns figures:
  every number is extracted by this module from the cells it points to, and checked against the
  plan's own Total row where there is one.

Booked figures are prorated to the reporting period by flight dates, so a monthly report compares
the month's delivery with the month's share of the plan, and pacing compares delivery to date with
the share of the flight that has elapsed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re

from . import fields as F
from . import fmt as FM
from . import rules as R
from .parsing import is_missing, read_csv, read_xlsx

METRICS = ["budget", "impressions", "reach", "views", "clicks", "engagements"]
DELIVERED = {"budget": "spend", "impressions": "impressions", "reach": "reach", "views": "views", "clicks": "clicks", "engagements": None}
METRIC_LABEL = {"budget": "Budget", "impressions": "Impressions", "reach": "Reach", "views": "Views", "clicks": "Clicks", "engagements": "Engagements"}
DIMS = ["platform", "market", "language", "campaign", "objective", "format", "buy_type", "targeting"]
FIELDS_ALL = DIMS + ["start_date", "end_date", "month", "flight", "currency", "booked_volume", "cpm", "cpc", "cpv"] + METRICS
CURRENCIES = ["AED", "USD", "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "EUR", "GBP"]
_CUR_RE = re.compile(r"\b(" + "|".join(CURRENCIES) + r")\b|(\$)", re.I)
MARKETS = {"uae": "UAE", "united arab emirates": "UAE", "emirates": "UAE", "ksa": "KSA", "saudi": "KSA", "saudi arabia": "KSA",
           "qatar": "Qatar", "kuwait": "Kuwait", "bahrain": "Bahrain", "oman": "Oman", "egypt": "Egypt", "gcc": "GCC", "mena": "MENA",
           "jordan": "Jordan", "lebanon": "Lebanon"}
SUMMARY_SHEET_RE = re.compile(r"summary|overview|total|dashboard|recap", re.I)
TOTAL_RE = re.compile(r"^\s*(grand\s+)?(sub[- ]?)?totals?\b", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------------------
# Header recognition
# ---------------------------------------------------------------------------
def norm_header(h) -> str:
    s = str(h or "").replace("\n", " ").replace("\xa0", " ").lower()
    s = re.sub(r"[_/|]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" :.-")


def header_currency(h) -> str | None:
    m = _CUR_RE.search(str(h or ""))
    if not m:
        return None
    return "USD" if m.group(2) else m.group(1).upper()


def header_field(h) -> str | None:
    """Which plan field a column header names. Rates, shares and percentages are ignored, as are
    fees, VAT and client-gross amounts (the booked budget is net media)."""
    s = norm_header(h)
    if not s:
        return None
    if re.search(r"\bcpm\b|cost per (1,?000|thousand|mille)", s):
        return "cpm"
    if re.search(r"\bcpc\b|cost per click", s):
        return "cpc"
    if re.search(r"\bcpv\b|\bcpcv\b|cost per (completed )?view", s):
        return "cpv"
    if re.search(r"cost per|\bcp[a-z]\b|\brate\b|\bctr\b|\bvtr\b|frequency|\bfreq\b|share|%|percent|\bsov\b|audience size|\bsize\b|\bfee\b|\bvat\b|to client|\bindex\b", s):
        return None
    if re.search(r"^curr(ency)?$", s):
        return "currency"
    if re.search(r"booked volume|booked units|\bvolume\b|\bunits\b|\bqty\b|quantity", s):
        return "booked_volume"
    if re.search(r"\bimpr|\bimps?\b", s):
        return "impressions"
    if re.search(r"\breach\b|unique users|\buniques\b", s):
        return "reach"
    if re.search(r"\bviews?\b|video plays|\bvv\b|thruplays?|\bplays\b", s):
        return "views"
    if re.search(r"\bclicks?\b|swipe ?ups?|\bswipes\b|site visits|link clicks|\bsessions\b", s):
        return "clicks"
    if re.search(r"engagements?\b", s) and "rate" not in s:
        return "engagements"
    if re.search(r"budget|net (to )?media|\bnet\b|gross|\bcost\b|investment|\bspend\b|\bamount\b|media value|planned spend", s):
        return "budget"
    if re.search(r"start", s) or s in ("from", "start date", "launch"):
        return "start_date"
    if re.search(r"\bend\b|finish", s) or s in ("to", "till", "until"):
        return "end_date"
    if s in ("month", "period", "flight month"):
        return "month"
    if re.search(r"^(flight|flight dates|dates|duration|timing)$", s):
        return "flight"
    if re.search(r"platform|channel|publisher|\bsite\b|network|vendor|partner|^media$|^media channel$|^source$", s):
        return "platform"
    if re.search(r"market|countr|\bgeo\b|region|location|\bcity\b", s):
        return "market"
    if re.search(r"language|\blang\b", s):
        return "language"
    if re.search(r"campaign|line item|\bactivity\b|flight name|\bphase\b", s):
        return "campaign"
    if re.search(r"objective|\bgoal\b|\bkpi\b", s):
        return "objective"
    if re.search(r"buy(ing)? type|buy model|pricing|cost model|\bmodel\b", s):
        return "buy_type"
    if re.search(r"format|placement|ad unit|creative|ad type|inventory|\bunit\b", s):
        return "format"
    if re.search(r"target|audience", s):
        return "targeting"
    return None


def _col_priority(field: str, header: str, report_cur: str | None) -> int:
    s = norm_header(header)
    p = 0
    if re.search(r"booked|planned|estimated|\best\b|target|plan\b", s):
        p += 2
    if field == "budget":
        cur = header_currency(header)
        if cur and report_cur and cur == report_cur:
            p += 3
        elif cur and report_cur and cur != report_cur:
            p -= 1
        if re.search(r"net (to )?media", s):
            p += 2
        elif "net" in s:
            p += 1
        if "total" in s:
            p += 1
        if "gross" in s:
            p -= 1
        if "unit" in s:
            p -= 5
    return p


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------
def num(v):
    if is_missing(v) or isinstance(v, (dt.date, dt.datetime)):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).replace("\xa0", " ").strip()
    if s.endswith("%"):
        return None
    s = _CUR_RE.sub("", s).replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def text(v) -> str:
    if is_missing(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()


def parse_date(v, year_hint: int | None = None) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 30000 < float(v) < 60000:  # Excel serial date
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    s = text(v)
    if not s:
        return None
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%y", "%d-%b-%Y",
              "%d %b %y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s.split(" 00:00")[0], f).date()
        except ValueError:
            continue
    return None


def _month_end(y: int, m: int) -> dt.date:
    return (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))


def parse_month(v, year_hint: int | None = None) -> tuple[dt.date, dt.date] | None:
    d = parse_date(v)
    if d:
        return dt.date(d.year, d.month, 1), _month_end(d.year, d.month)
    s = text(v).lower()
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s'\-]*(\d{2,4})?\b", s)
    if not m:
        return None
    mo = MONTHS[m.group(1)]
    y = m.group(2)
    y = (2000 + int(y) if len(y) == 2 else int(y)) if y else year_hint
    if not y:
        return None
    return dt.date(y, mo, 1), _month_end(y, mo)


_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def parse_flight(v, year_hint: int | None = None) -> tuple[dt.date, dt.date] | None:
    """'15–25 November 2026', '8 Nov – 5 Dec 2026 — 4 weeks', 'October 2026 — 4 weeks',
    '01/10/2026 - 31/10/2026'."""
    if isinstance(v, (dt.date, dt.datetime)):
        d = parse_date(v)
        return (d, d) if d else None
    s = text(v)
    if not s:
        return None
    s = re.split(r"\s+—\s+|\(|;", s)[0].strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace(" to ", " - ")
    parts = [p.strip() for p in re.split(r"\s+-\s+|(?<=\d)-(?=\d)|(?<=[a-z])-(?=\d)", s) if p.strip()]
    if len(parts) == 2:
        a, b = parts
        db = parse_date(b)
        if not db:
            mb = re.match(rf"^(\d{{1,2}})(st|nd|rd|th)?\s+{_MON}\s*,?\s*(\d{{4}})?$", b)
            if mb:
                y = int(mb.group(4)) if mb.group(4) else year_hint
                if y:
                    db = dt.date(y, MONTHS[mb.group(3)[:3]], int(mb.group(1)))
        if db:
            da = parse_date(a)
            if not da:
                ma = re.match(rf"^(\d{{1,2}})(st|nd|rd|th)?(\s+{_MON})?\s*,?\s*(\d{{4}})?$", a)
                if ma:
                    mo = MONTHS[ma.group(4)[:3]] if ma.group(3) else db.month
                    y = int(ma.group(5)) if ma.group(5) else (db.year - (1 if mo > db.month else 0))
                    try:
                        da = dt.date(y, mo, int(ma.group(1)))
                    except ValueError:
                        da = None
            if da and da <= db:
                return da, db
    mo = parse_month(s, year_hint)
    if mo and re.fullmatch(rf"{_MON}\s*,?\s*(\d{{2,4}})?", s.strip()):
        return mo
    return None


def platform_of(t: str) -> str | None:
    s = (t or "").lower()
    if not s:
        return None
    found = F.name_platforms(re.sub(r"[/|:]+", " ", s.replace("\n", " ")))
    if "youtube" in s or "google" in s or "dv360" in s:
        found.add("google_ads")
    return sorted(found)[0] if len(found) == 1 else None


def market_of(t: str) -> str | None:
    s = (t or "").lower().strip()
    if s in MARKETS:
        return MARKETS[s]
    for k, v in MARKETS.items():
        if re.search(rf"(^|[^a-z]){re.escape(k)}([^a-z]|$)", s):
            return v
    return None


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------
def load_sheets(path: str) -> list:
    ext = os.path.splitext(path)[1].lower()
    return read_csv(path) if ext == ".csv" else read_xlsx(path)


def _row_fields(row) -> dict[int, str]:
    out = {}
    for ci, v in enumerate(row):
        if isinstance(v, str):
            f = header_field(v)
            if f:
                out[ci] = f
    return out


def find_header(rows: list[list], limit: int = 40) -> tuple[int, dict[int, str], int] | None:
    best = None
    for ri, row in enumerate(rows[:limit]):
        fm = _row_fields(row)
        fields = set(fm.values())
        metrics = fields & set(METRICS)
        if not metrics:
            continue
        if not ("platform" in fields or len(metrics) >= 2):
            continue
        score = len(fields) + (3 if "platform" in fields else 0) + (2 if "budget" in fields else 0)
        if best is None or score > best[2]:
            best = (ri, fm, score)
    return best


def _resolve_columns(header_row: list, fm: dict[int, str], data_rows: list[list], report_cur: str | None) -> dict[str, int]:
    """One column per field. Volume columns that only hold fractions (a 'Reach' column of reach %)
    are not volumes."""
    by_field: dict[str, list[int]] = {}
    for ci, f in fm.items():
        by_field.setdefault(f, []).append(ci)
    out = {}
    for f, cols in by_field.items():
        def score(ci):
            p = _col_priority(f, header_row[ci], report_cur)
            if f in METRICS or f == "booked_volume":
                vals = [num(r[ci]) for r in data_rows if ci < len(r)]
                vals = [x for x in vals if x is not None]
                if vals and max(abs(x) for x in vals) <= 1.0:
                    p -= 10
            return p
        best = max(cols, key=lambda c: (score(c), -c))
        if f in METRICS and score(best) <= -10:
            continue
        out[f] = best
    return out


def sheet_metadata(rows: list[list], header_idx: int) -> dict:
    """Label/value pairs above the plan table ('Campaign Name: ...', 'Flight: October 2026')."""
    meta = {}
    for row in rows[:header_idx]:
        cells = [(ci, v) for ci, v in enumerate(row) if not is_missing(v)]
        for k, (ci, v) in enumerate(cells):
            if not isinstance(v, str) or k + 1 >= len(cells):
                continue
            label = norm_header(v)
            val = cells[k + 1][1]
            if re.search(r"^(campaign name|campaign|activity)$", label):
                meta.setdefault("campaign", text(val))
            elif re.search(r"objective", label):
                meta.setdefault("objective", text(val))
            elif re.search(r"^market", label) or label in ("country", "countries", "geo"):
                meta.setdefault("market", text(val))
            elif re.search(r"^(flight|flight dates|dates|campaign period|period|duration|timing)", label):
                meta.setdefault("flight", val)
            elif re.search(r"^curr", label):
                meta.setdefault("currency", text(val).upper())
            elif re.search(r"^client", label):
                meta.setdefault("client", text(val))
        for v in row:
            if isinstance(v, str) and "currency" not in meta:
                m = re.search(r"(figures|amounts|budgets?|costs?|values)\s+(are\s+)?in\s+(" + "|".join(CURRENCIES) + r")\b", v, re.I)
                if m:
                    meta["currency"] = m.group(3).upper()
    return meta


def extract_sheet(name: str, rows: list[list], header_idx: int, cols: dict[str, int], *, last_row: int = 0, sheet_currency: str | None = None,
                  year_hint: int | None = None, header_row: list | None = None) -> dict:
    """Plan lines from one sheet given where the columns are. Section rows (one label, no platform,
    no dims) set the objective for the lines under them; Total rows are kept for the check."""
    header_row = header_row if header_row is not None else rows[header_idx]
    meta = sheet_metadata(rows, header_idx)
    flight = parse_flight(meta.get("flight"), year_hint) if meta.get("flight") is not None else None
    col_cur = {f: header_currency(header_row[ci]) for f, ci in cols.items() if ci < len(header_row)}
    sheet_cur = sheet_currency or meta.get("currency") or None
    if sheet_cur and sheet_cur not in CURRENCIES:
        sheet_cur = header_currency(sheet_cur)
    lines, totals, notes = [], [], []
    ctx = {"platform": "", "market": meta.get("market", ""), "campaign": meta.get("campaign", ""), "objective": meta.get("objective", "")}
    section = ""
    blank = 0
    started = False
    end = len(rows) if not last_row else min(len(rows), last_row)
    label_cols = [cols[f] for f in ("platform", "campaign", "market", "objective", "format") if f in cols]
    first_col = min([ci for ci in cols.values()] or [0])
    for ri in range(header_idx + 1, end):
        row = rows[ri]
        get = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else None  # noqa: E731
        vals = {m: num(get(m)) for m in METRICS + ["booked_volume", "cpm", "cpc", "cpv"] if m in cols}
        has_num = any(v not in (None, 0) for v in vals.values())
        texts = [text(v) for v in row if isinstance(v, str) and text(v)]
        if not texts and not has_num:
            blank += 1
            if started and blank >= 6:
                break
            continue
        blank = 0
        lead = " ".join(text(row[ci]) for ci in sorted(set(label_cols + [first_col])) if ci < len(row) and text(row[ci]))
        label = lead or (texts[0] if texts else "")
        if TOTAL_RE.match(label):
            if re.search(r"sub[- ]?total", label, re.I):
                continue  # section subtotal: the lines under it are already counted
            totals.append({"row": ri + 1, "label": label, **{m: v for m, v in vals.items() if v is not None}})
            if started:
                break  # notes, fees and VAT follow the plan's Total row
            continue
        dim = {f: text(get(f)) for f in DIMS if f in cols}
        plat_txt = dim.get("platform", "")
        plat = platform_of(plat_txt) if plat_txt else None
        other_dims = [v for k, v in dim.items() if k != "platform" and v]
        if plat_txt and not plat and not other_dims:
            section = plat_txt  # 'Engagement', 'Site Visits': objective section with its subtotal
            for k in ("format", "buy_type", "targeting"):
                ctx.pop(k, None)
            continue
        if not has_num:
            if len(texts) == 1 and not plat:
                section = texts[0]
            continue
        started = True
        for k in ("platform", "market", "campaign", "format", "buy_type", "targeting"):
            if dim.get(k):
                ctx[k] = dim[k]
            elif ctx.get(k):
                dim[k] = ctx[k]  # merged / blank cells repeat the value above
        if not plat:
            plat = platform_of(dim.get("platform", "")) or platform_of(dim.get("campaign", "")) or platform_of(dim.get("format", "")) or platform_of(name)
        if not dim.get("objective"):
            dim["objective"] = section or ctx.get("objective", "")
        if not dim.get("market"):
            dim["market"] = market_of(section) or market_of(name) or ""
        start = parse_date(get("start_date"), year_hint) if "start_date" in cols else None
        endd = parse_date(get("end_date"), year_hint) if "end_date" in cols else None
        if not (start and endd) and "month" in cols:
            mo = parse_month(get("month"), year_hint)
            if mo:
                start, endd = mo
        if not (start and endd) and "flight" in cols:
            fl = parse_flight(get("flight"), year_hint)
            if fl:
                start, endd = fl
        if not (start and endd) and flight:
            start, endd = flight
        cur = (text(get("currency")).upper() or None) if "currency" in cols else None
        cur = header_currency(cur) if cur else None
        line = {"sheet": name, "row": ri + 1, "platform": plat, "platform_label": dim.get("platform") or (F.PLATFORM_LABELS.get(plat) if plat else ""),
                "market": market_of(dim.get("market", "")) or dim.get("market", ""), "language": dim.get("language", ""),
                "campaign": dim.get("campaign", ""), "objective": dim.get("objective", ""), "format": dim.get("format", ""),
                "buy_type": dim.get("buy_type", ""), "targeting": dim.get("targeting", "")[:160],
                "start": start, "end": endd, "currency": cur or col_cur.get("budget") or sheet_cur, "derived": []}
        for m in METRICS + ["booked_volume", "cpm", "cpc", "cpv"]:
            line[m] = vals.get(m)
        _derive(line)
        if not any((line.get(m) or 0) > 0 for m in METRICS):
            continue  # a zero line (e.g. an unfunded trial) books nothing
        lines.append(line)
    return {"lines": lines, "totals": totals, "meta": meta, "notes": notes,
            "flight": flight, "currency_from": "column header" if col_cur.get("budget") else ("sheet" if sheet_cur else None)}


def _derive(line: dict):
    b = line.get("budget")
    if line.get("impressions") is None and b and line.get("cpm"):
        line["impressions"] = b / line["cpm"] * 1000
        line["derived"].append("impressions = budget / CPM x 1,000")
    if line.get("clicks") is None and b and line.get("cpc"):
        line["clicks"] = b / line["cpc"]
        line["derived"].append("clicks = budget / CPC")
    if line.get("views") is None and b and line.get("cpv"):
        line["views"] = b / line["cpv"]
        line["derived"].append("views = budget / CPV")
    if b is None and line.get("impressions") and line.get("cpm"):
        line["budget"] = line["impressions"] * line["cpm"] / 1000
        line["derived"].append("budget = impressions x CPM / 1,000")
    bt = (line.get("buy_type") or "").lower()
    if line.get("booked_volume") is not None:
        unit = "impressions" if "cpm" in bt else "clicks" if "cpc" in bt or "cpl" in bt else "views" if "cpv" in bt or "cpcv" in bt else "engagements" if "cpe" in bt else None
        line["booked_unit"] = unit
        if unit and line.get(unit) is None:
            line[unit] = line["booked_volume"]
            line["derived"].append(f"{unit} = booked volume ({line.get('buy_type')})")


# ---------------------------------------------------------------------------
# Built-in rules: find the plan sheets and columns
# ---------------------------------------------------------------------------
def rule_mapping(sheets: list, report_cur: str | None) -> dict | None:
    cands = []
    for si, sh in enumerate(sheets):
        h = find_header(sh.rows)
        if h:
            ri, fm, score = h
            cols = _resolve_columns(sh.rows[ri], fm, sh.rows[ri + 1:ri + 200], report_cur)
            if set(cols) & set(METRICS):
                cands.append({"sheet": sh.name, "index": si, "header_row": ri + 1, "columns": cols, "score": score})
    if not cands:
        return None
    notes = []
    if len(cands) > 1:
        detail = [c for c in cands if not SUMMARY_SHEET_RE.search(c["sheet"])]
        if detail and len(detail) < len(cands):
            notes.append("Summary tab(s) " + ", ".join(f"'{c['sheet']}'" for c in cands if c not in detail) + " not counted: they repeat the detailed tabs")
            cands = detail
    return {"sheets": [{k: c[k] for k in ("sheet", "index", "header_row", "columns")} for c in cands], "notes": notes}


def header_signature(sheets: list, mapping: dict | None) -> list:
    sig = []
    if mapping:
        for m in mapping["sheets"]:
            sh = _sheet(sheets, m)
            if sh is not None and m["header_row"] - 1 < len(sh.rows):
                sig.append(tuple(norm_header(v) for v in sh.rows[m["header_row"] - 1] if text(v)))
    else:
        for sh in sheets:
            best = max(sh.rows[:30] or [[]], key=lambda r: sum(1 for v in r if isinstance(v, str) and v.strip() and not re.search(r"\d", v)))
            sig.append(tuple(norm_header(v) for v in best if isinstance(v, str) and v.strip() and not re.search(r"\d", v)))
    return sorted(set(sig))


def layout_fingerprint(sheets: list, rule_map: dict | None) -> str:
    """Same layout month to month: the plan table's header row(s). Sheet names, campaign names,
    dates and figures are ignored, so next month's plan (or a V7 with an extra campaign tab) is the
    same layout."""
    return hashlib.sha256(json.dumps(header_signature(sheets, rule_map)).encode()).hexdigest()[:20]


def _sheet(sheets, m):
    by_name = {s.name: s for s in sheets}
    if m.get("sheet") in by_name:
        return by_name[m["sheet"]]
    idx = m.get("index")
    return sheets[idx] if isinstance(idx, int) and 0 <= idx < len(sheets) else None


def apply_mapping(sheets: list, mapping: dict, year_hint: int | None, report_cur: str | None) -> dict:
    """Extract lines with a mapping (from the rules, Claude or a saved layout). A saved layout is
    applied to every sheet whose header row matches it, so a plan with a new campaign tab is read."""
    out = {"lines": [], "totals": [], "sheets": [], "notes": list(mapping.get("notes") or []), "flights": {}}
    targets = []
    if mapping.get("saved"):
        sigs = {tuple(s) for s in mapping.get("signature") or []}
        for si, sh in enumerate(sheets):
            if SUMMARY_SHEET_RE.search(sh.name) and len(sheets) > 1 and mapping.get("skip_summary", True):
                continue
            for ri, row in enumerate(sh.rows[:40]):
                if tuple(norm_header(v) for v in row if text(v)) in sigs:
                    fm = _row_fields(row)
                    cols = {f: ci for f, ci in (mapping.get("columns_by_header") or {}).items()}
                    hdr = [norm_header(v) for v in row]
                    resolved = {f: hdr.index(h) for f, h in cols.items() if h in hdr}
                    targets.append((sh, ri, resolved or _resolve_columns(row, fm, sh.rows[ri + 1:ri + 200], report_cur), 0, None))
                    break
    else:
        for m in mapping["sheets"]:
            sh = _sheet(sheets, m)
            if sh is None:
                out["notes"].append(f"Sheet '{m.get('sheet')}' not found in the media plan")
                continue
            targets.append((sh, m["header_row"] - 1, {f: int(c) for f, c in m["columns"].items()}, int(m.get("last_row") or 0), m.get("currency") or None))
    for sh, ri, cols, last, cur in targets:
        if ri >= len(sh.rows):
            continue
        res = extract_sheet(sh.name, sh.rows, ri, cols, last_row=last, sheet_currency=cur, year_hint=year_hint)
        out["lines"] += res["lines"]
        out["totals"] += [{**t, "sheet": sh.name} for t in res["totals"]]
        out["sheets"].append({"sheet": sh.name, "header_row": ri + 1, "columns": {f: norm_header(sh.rows[ri][c]) for f, c in cols.items() if c < len(sh.rows[ri])},
                              "lines": len(res["lines"]), "meta": {k: text(v) for k, v in res["meta"].items()}})
        if res["flight"]:
            out["flights"][sh.name] = res["flight"]
    return out


def total_check(read: dict) -> list[dict]:
    """The plan's own Total row per sheet vs the sum of the lines read."""
    out = []
    for sh in {t["sheet"] for t in read["totals"]}:
        tot = [t for t in read["totals"] if t["sheet"] == sh]
        t = tot[-1]
        lines = [ln for ln in read["lines"] if ln["sheet"] == sh]
        for m in ("budget", "impressions", "clicks", "views"):
            if t.get(m) is None:
                continue
            got = sum((ln.get(m) or 0) for ln in lines if f"{m} = " not in " ".join(ln["derived"]))
            if any(f"{m} = " in " ".join(ln["derived"]) for ln in lines):
                continue
            ok = abs(got - t[m]) <= max(1.0, 0.005 * abs(t[m]))
            out.append({"sheet": sh, "metric": m, "plan_total": t[m], "lines_total": got, "ok": ok})
    return out


def _confident(read: dict, checks: list[dict]) -> bool:
    if not read["lines"]:
        return False
    if any(not c["ok"] for c in checks):
        return False
    if any(not ln["platform"] for ln in read["lines"]):
        return False
    return True


def grid_for_model(sheets: list, max_rows: int = 60, max_cols: int = 30) -> str:
    """The first rows of each sheet as a compact text grid (cell refs kept so Claude can point at
    rows and columns). Long cells are cut; this is the only plan content sent to Claude."""
    parts = []
    for si, sh in enumerate(sheets[:12]):
        parts.append(f"### sheet {si} '{sh.name}'")
        for ri, row in enumerate(sh.rows[:max_rows]):
            cells = [f"c{ci + 1}={text(v)[:40]}" for ci, v in enumerate(row[:max_cols]) if text(v)]
            if cells:
                parts.append(f"r{ri + 1}: " + " | ".join(cells))
    return "\n".join(parts)[:60000]


def mapping_from_model(out, sheets: list) -> dict:
    """Claude's answer (MediaPlanMapOut) -> internal mapping (0-based columns)."""
    m = {"sheets": [], "notes": list(getattr(out, "notes", []) or [])}
    for s in out.sheets:
        idx = s.sheet_index if 0 <= s.sheet_index < len(sheets) else None
        name = sheets[idx].name if idx is not None else s.sheet_name
        cols = {}
        for c in s.columns:
            if c.field in FIELDS_ALL and c.column >= 1:
                cols.setdefault(c.field, c.column - 1)
        if cols and s.header_row >= 1:
            m["sheets"].append({"sheet": name, "index": idx, "header_row": s.header_row, "columns": cols, "last_row": s.last_row,
                                "currency": (s.currency or "").upper() or None})
    return m


def saved_form(sheets: list, mapping: dict) -> dict:
    """Mapping as saved for the client: by header text, so it applies to next month's file."""
    sig, by_header = [], {}
    for m in mapping["sheets"]:
        sh = _sheet(sheets, m)
        if sh is None:
            continue
        row = sh.rows[m["header_row"] - 1]
        sig.append([norm_header(v) for v in row if text(v)])
        for f, ci in m["columns"].items():
            if ci < len(row) and text(row[ci]):
                by_header.setdefault(f, norm_header(row[ci]))
    return {"saved": True, "signature": sig, "columns_by_header": by_header, "notes": mapping.get("notes", [])[:5]}


# ---------------------------------------------------------------------------
# Entry point used by the pipeline
# ---------------------------------------------------------------------------
def read_media_plans(files: list[dict], provider, saved_layouts: dict, task: dict, report_cur: str | None) -> dict:
    ps = task.get("period_start")
    year_hint = ps.year if isinstance(ps, dt.date) else dt.date.today().year
    rate = R.usd_aed_rate(task)
    res = {"files": [], "lines": [], "warnings": [], "notes": [], "assumptions": [], "save_layouts": {}, "total_checks": [], "engines": []}
    for f in files:
        try:
            sheets = load_sheets(f["path"])
        except Exception as e:  # noqa: BLE001
            res["warnings"].append(f"Media plan '{f['name']}' could not be opened ({type(e).__name__}: {e})")
            continue
        rmap = rule_mapping(sheets, report_cur)
        fp = layout_fingerprint(sheets, rmap)
        engine, read, checks = None, None, []
        saved = (saved_layouts or {}).get(fp)
        if saved:
            read = apply_mapping(sheets, {**saved, "saved": True}, year_hint, report_cur)
            checks = total_check(read)
            if read["lines"]:
                engine = ("saved", "Layout read as in an earlier report for this client (no Claude call)")
        if engine is None and rmap:
            read = apply_mapping(sheets, rmap, year_hint, report_cur)
            checks = total_check(read)
            if _confident(read, checks):
                engine = ("rules", "Plan table found by the built-in rules and its totals match the plan's own Total row" if checks else "Plan table found by the built-in rules")
        if engine is None and hasattr(provider, "map_media_plan"):
            out = provider.map_media_plan(grid_for_model(sheets), f["name"])
            if out is not None:
                cmap = mapping_from_model(out, sheets)
                if cmap["sheets"]:
                    cread = apply_mapping(sheets, cmap, year_hint, report_cur)
                    cchecks = total_check(cread)
                    if cread["lines"]:
                        read, checks = cread, cchecks
                        engine = ("claude", "New media plan layout - Claude located the plan table; figures were read from the cells and checked")
                        if all(c["ok"] for c in checks):
                            res["save_layouts"][fp] = saved_form(sheets, cmap)
        if engine is None and read and read["lines"]:
            engine = ("rules", "Built-in rules (low confidence - check the lines read)")
            res["warnings"].append(f"Media plan '{f['name']}': the layout was not fully recognised. Check the 'Media Plan' tab; lines without a platform are not compared.")
        if engine is None:
            res["warnings"].append(f"Media plan '{f['name']}': no table of booked figures (platform with budget, impressions, views or clicks) was found. Booked vs delivered was not produced for this file.")
            res["files"].append({"name": f["name"], "engine": None, "lines": 0, "layout": fp})
            continue
        lines = read["lines"]
        for ln in lines:
            ln["file"] = f["name"]
            _apply_currency(ln, report_cur, rate, res)
        for c in checks:
            if not c["ok"]:
                res["warnings"].append(f"Media plan '{f['name']}' tab '{c['sheet']}': {METRIC_LABEL[c['metric']].lower()} of the lines read ({FM.fmt_num(c['lines_total'])}) "
                                       f"differs from the plan's Total row ({FM.fmt_num(c['plan_total'])}). Check the 'Media Plan' tab.")
        res["total_checks"] += [{**c, "file": f["name"]} for c in checks]
        res["notes"] += read["notes"]
        res["lines"] += lines
        res["engines"].append({"file": f["name"], "engine": engine[0], "reason": engine[1]})
        res["files"].append({"name": f["name"], "engine": engine[0], "reason": engine[1], "lines": len(lines), "layout": fp, "sheets": read["sheets"]})
    if not any(ln.get("currency") for ln in res["lines"]) and res["lines"] and report_cur:
        res["assumptions"].append(f"The media plan does not state its currency; budgets are read as {report_cur}.")
    return res


def _apply_currency(ln: dict, report_cur: str | None, rate: float, res: dict):
    cur = ln.get("currency")
    ln["budget_original"] = ln.get("budget")
    ln["currency_original"] = cur or report_cur
    ln["fx_rate"] = None
    if not cur or not report_cur or cur == report_cur:
        return
    if cur == "USD" and report_cur == "AED":
        for m in ("budget", "cpm", "cpc", "cpv"):
            if ln.get(m) is not None:
                ln[m] = ln[m] * rate
        ln["fx_rate"] = rate
        msg = f"Media plan budgets in USD converted to AED at {rate:g} (agency rule)."
        if msg not in res["assumptions"]:
            res["assumptions"].append(msg)
        return
    ln["budget"] = None
    msg = f"Media plan budget in {cur} not compared: the report is in {report_cur} and no {cur}→{report_cur} rate is set. Volumes are still compared."
    if msg not in res["warnings"]:
        res["warnings"].append(msg)


# ---------------------------------------------------------------------------
# Booked vs delivered
# ---------------------------------------------------------------------------
def _days(a: dt.date, b: dt.date) -> int:
    return (b - a).days + 1 if a and b and b >= a else 0


def _overlap(a1, b1, a2, b2) -> int:
    if not (a1 and b1 and a2 and b2):
        return 0
    return _days(max(a1, a2), min(b1, b2))


def _codes(s: str) -> set[str]:
    return {m.lower() for m in re.findall(r"\b(?:C|P|Phase\s?|Wave\s?|Flight\s?)\d{1,2}\b", s or "", re.I)}


_STOP = set("the and for with campaign launch growth challenge on x x2 media plan digital uae ksa 2026 2025 2027 meta social".split())


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in _STOP}


def match_campaign(delivered_name: str, plan_campaigns: list[str]) -> str | None:
    """Delivered campaign -> plan campaign by its code (C1, Phase 2) or two distinctive words."""
    dn = delivered_name or ""
    dcodes = _codes(re.sub(r"[_\-|]", " ", dn))
    hits = [p for p in plan_campaigns if _codes(p) & dcodes]
    if len(hits) == 1:
        return hits[0]
    dw = _words(re.sub(r"[_\-|]", " ", dn))
    scored = sorted(((len(_words(p) & dw), p) for p in plan_campaigns), reverse=True)
    if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


def compare(plan: dict, calc, period: dict, task: dict) -> dict | None:
    """Booked (prorated to the reporting period) vs delivered, per platform (and per plan campaign
    when the delivered campaign names can be matched to the plan's), with pacing to date."""
    lines = [ln for ln in plan.get("lines", []) if ln.get("platform")]
    if not lines or calc is None or calc.dataset is None:
        return None
    from .calc import aggregate, safe_div
    ps = task.get("period_start") or period.get("data_start")
    pe = task.get("period_end") or period.get("data_end")
    de = period.get("data_end") or pe
    asof = min(pe, de) if pe and de else (pe or de)
    notes, used = [], []
    for ln in lines:
        s, e = ln.get("start"), ln.get("end")
        if s and e and ps and pe:
            ov = _overlap(s, e, ps, pe)
            if ov == 0:
                ln["in_period"] = 0.0
                continue
            ln["in_period"] = ov / _days(s, e)
            ln["elapsed"] = _overlap(s, e, ps, asof) / _days(s, e)
            ln["flight_left"] = e > asof
        else:
            ln["in_period"] = 1.0
            ln["elapsed"] = (_days(ps, asof) / _days(ps, pe)) if (ps and pe and asof) else 1.0
            ln["flight_left"] = bool(asof and pe and asof < pe)
        used.append(ln)
    out_of = len(lines) - len(used)
    if out_of:
        notes.append(f"{out_of} plan line(s) have flights outside the reporting period and are not compared.")
    if not any(ln.get("start") for ln in used):
        notes.append("The media plan has no flight dates; booked figures are compared as planned for this reporting period.")
    elif any(0 < ln["in_period"] < 1 for ln in used):
        notes.append("Lines whose flight runs beyond the reporting period are prorated by days in the period.")
    if not used:
        return {"rows": [], "total": None, "notes": notes, "lines_used": 0, "metrics": [], "asof": asof}
    d = calc.dataset
    cur_rows = d[d["_period"] == "current"]
    # campaign-level comparison when every delivered campaign of a planned platform maps to a plan campaign
    plan_camps = sorted({ln["campaign"] for ln in used if ln.get("campaign")})
    camp_map = {}
    if len(plan_camps) > 1 and "campaign_name" in cur_rows.columns:
        for p in sorted({ln["platform"] for ln in used}):
            for cn in cur_rows[cur_rows["platform"] == p]["campaign_name"].dropna().unique():
                camp_map[(p, cn)] = match_campaign(str(cn), plan_camps)
        if camp_map and not all(camp_map.values()):
            unmatched = [cn for (p, cn), v in camp_map.items() if not v]
            notes.append(f"Compared per platform: {len(unmatched)} delivered campaign(s) could not be matched to a plan campaign (e.g. '{unmatched[0]}').")
            camp_map = {}
    by_campaign = bool(camp_map)
    metrics = [m for m in METRICS if any(ln.get(m) for ln in used)]
    keyf = (lambda ln: (ln["platform"], ln.get("campaign") or "")) if by_campaign else (lambda ln: (ln["platform"], ""))
    groups: dict[tuple, list] = {}
    for ln in used:
        groups.setdefault(keyf(ln), []).append(ln)
    rows = []
    for (plat, camp), lns in sorted(groups.items(), key=lambda kv: -sum((x.get("budget") or 0) * x["in_period"] for x in kv[1])):
        sub = cur_rows[cur_rows["platform"] == plat]
        if by_campaign:
            names = [cn for (p, cn), v in camp_map.items() if p == plat and v == camp]
            sub = sub[sub["campaign_name"].isin(names)]
        agg = aggregate(sub, []) if len(sub) else []
        dl = agg[0] if agg else {}
        row = {"platform": plat, "label": F.PLATFORM_LABELS.get(plat, plat), "short": SHORT.get(plat, plat), "campaign": camp, "campaign_short": short_campaign(camp), "lines": len(lns), "has_delivery": bool(len(sub)),
               "flight_left": any(x.get("flight_left") for x in lns),
               "start": min((x["start"] for x in lns if x.get("start")), default=None), "end": max((x["end"] for x in lns if x.get("end")), default=None)}
        for m in metrics:
            booked_full = sum(x[m] for x in lns if x.get(m) is not None) if any(x.get(m) is not None for x in lns) else None
            booked = sum(x[m] * x["in_period"] for x in lns if x.get(m) is not None) if booked_full is not None else None
            expected = sum(x[m] * x["elapsed"] for x in lns if x.get(m) is not None) if booked_full is not None else None
            dkey = DELIVERED[m]
            delivered = dl.get(dkey) if dkey and len(sub) else None
            row[m] = {"booked_flight": booked_full, "booked": booked, "expected": expected, "delivered": delivered,
                      "delivery_pct": safe_div(delivered, booked, 100.0), "pacing_pct": safe_div(delivered, expected, 100.0)}
        _rates(row)
        row["status"] = _status(row)
        rows.append(row)
    total = {"label": "Total (planned platforms)", "short": "Total", "flight_left": any(r["flight_left"] for r in rows)}
    for m in metrics:
        parts = [r[m] for r in rows]
        def s(k):
            vals = [p[k] for p in parts if p[k] is not None]
            return sum(vals) if vals else None
        booked, expected = s("booked"), s("expected")
        delivered = sum(p["delivered"] for p in parts if p["delivered"] is not None and p["booked"] is not None) if any(p["delivered"] is not None for p in parts) else None
        total[m] = {"booked_flight": s("booked_flight"), "booked": booked, "expected": expected, "delivered": delivered,
                    "delivery_pct": safe_div(delivered, booked, 100.0), "pacing_pct": safe_div(delivered, expected, 100.0)}
    _rates(total)
    total["status"] = _status(total)
    planned = {r["platform"] for r in rows}
    extra = sorted({p for p in cur_rows["platform"].dropna().unique()} - planned)
    if extra:
        notes.append("Delivered without a media plan line: " + ", ".join(F.PLATFORM_LABELS.get(p, p) for p in extra) + " (not in the comparison).")
    no_data = [r["label"] + (f" ({r['campaign']})" if r["campaign"] else "") for r in rows if not r["has_delivery"]]
    if no_data:
        notes.append("Booked but no delivery in the uploaded exports: " + ", ".join(no_data) + ".")
    if "views" in metrics:
        notes.append("Delivered views follow the agency definition: Meta 3-second plays, Snapchat and TikTok 2-second plays.")
    if "clicks" in metrics:
        notes.append("Delivered clicks are link clicks (Snapchat swipe ups).")
    if "reach" in metrics:
        notes.append("Delivered reach is summed across campaigns (agency rule), so it is not deduplicated like a planned reach estimate.")
    return {"rows": rows, "total": total, "metrics": metrics, "notes": notes, "lines_used": len(used), "by_campaign": by_campaign,
            "asof": asof, "period_start": ps, "period_end": pe, "currency": calc.currency}


SHORT = {"meta": "Meta", "tiktok": "TikTok", "snapchat": "Snapchat", "google_ads": "Google", "linkedin": "LinkedIn", "x": "X"}


def short_campaign(c: str) -> str:
    """'McLaren x ON — C2 Strava Race Challenge' -> 'C2 Strava Race Challenge'."""
    parts = [p.strip() for p in re.split(r"\s[—–]\s|\s-\s", c or "") if p.strip()]
    return parts[-1] if parts else (c or "")


def display(row: dict) -> str:
    return (row.get("short") or row.get("label") or "") + (f" – {row['campaign_short']}" if row.get("campaign_short") else "")


def _rates(row: dict):
    from .calc import safe_div
    d = row

    def g(m, k):
        return (d.get(m) or {}).get(k)
    row["rates"] = {
        "cpm": {"planned": safe_div(g("budget", "booked_flight"), g("impressions", "booked_flight"), 1000.0), "actual": safe_div(g("budget", "delivered"), g("impressions", "delivered"), 1000.0)},
        "cpc": {"planned": safe_div(g("budget", "booked_flight"), g("clicks", "booked_flight")), "actual": safe_div(g("budget", "delivered"), g("clicks", "delivered"))},
        "cpv": {"planned": safe_div(g("budget", "booked_flight"), g("views", "booked_flight")), "actual": safe_div(g("budget", "delivered"), g("views", "delivered"))},
    }


def primary_metric(row: dict) -> str | None:
    for m in ("budget", "impressions", "clicks", "views"):
        if (row.get(m) or {}).get("pacing_pct") is not None:
            return m
    return None


def _status(row: dict) -> str:
    m = primary_metric(row)
    if not m:
        return "No delivery data"
    p = row[m]["pacing_pct"]
    if not row.get("flight_left", True):  # flight over: delivered vs booked, not pacing
        return "Under-delivered" if p < 90 else "Over-delivered" if p > 110 else "Delivered"
    if p < 90:
        return "Behind plan"
    if p > 110:
        return "Ahead of plan"
    return "On track"


# ---------------------------------------------------------------------------
# Facts and commentary (numbers only from here; commentary is checked against these facts)
# ---------------------------------------------------------------------------
def plan_facts(cmp: dict, currency: str | None) -> dict:
    facts = {}

    def put(fid, v, metric, scope, unit):
        if v is not None:
            facts[fid] = {"value": v, "metric": metric, "scope": scope, "unit": unit, "currency": currency}
    for r in (cmp.get("rows") or []) + ([cmp["total"]] if cmp.get("total") else []):
        lab = r["label"] + (f" / {r['campaign']}" if r.get("campaign") else "")
        base = "plan.total" if r is cmp.get("total") else f"plan[{lab}]"
        for m in cmp["metrics"]:
            x = r.get(m) or {}
            unit = "money" if m == "budget" else "count"
            put(f"{base}.{m}_booked", x.get("booked"), f"booked_{m}", f"Media plan: {lab}", unit)
            put(f"{base}.{m}_booked_flight", x.get("booked_flight"), f"booked_{m}", f"Media plan (full flight): {lab}", unit)
            put(f"{base}.{m}_delivered", x.get("delivered"), m if m != "budget" else "spend", f"Delivered: {lab}", unit)
            put(f"{base}.{m}_delivery_pct", x.get("delivery_pct"), "delivery_pct", f"Delivered vs booked: {lab}", "percent")
            put(f"{base}.{m}_pacing_pct", x.get("pacing_pct"), "pacing_pct", f"Pacing vs plan to date: {lab}", "percent")
        for k, v in (r.get("rates") or {}).items():
            put(f"{base}.{k}_planned", v.get("planned"), k, f"Planned {k.upper()}: {lab}", "money")
            put(f"{base}.{k}_actual", v.get("actual"), k, f"Actual {k.upper()}: {lab}", "money")
    return facts


def plan_commentary(cmp: dict, currency: str | None) -> list[dict]:
    out = []
    cur = currency or ""
    t = cmp.get("total") or {}
    asof = cmp.get("asof")
    if (t.get("budget") or {}).get("delivery_pct") is not None:
        b = t["budget"]
        out.append({"type": "observation", "evidence": ["plan.total.budget_delivered", "plan.total.budget_booked", "plan.total.budget_delivery_pct"],
                    "text": f"Against the media plan, spend was {FM.fmt_money(b['delivered'], cur)} of the {FM.fmt_money(b['booked'], cur)} booked for the period "
                            f"({FM.fmt_pct(b['delivery_pct'], 1)} of booked budget)."})
    for r in cmp.get("rows") or []:
        lab = display(r)
        fid = "plan[" + r["label"] + (f" / {r['campaign']}" if r.get("campaign") else "") + "]"
        for m in ("impressions", "clicks", "views"):
            x = r.get(m) or {}
            if x.get("delivery_pct") is None:
                continue
            out.append({"type": "observation", "evidence": [f"{fid}.{m}_delivered", f"{fid}.{m}_booked", f"{fid}.{m}_delivery_pct"],
                        "text": f"{lab} delivered {FM.fmt_int(x['delivered'])} {m}, {FM.fmt_pct(x['delivery_pct'], 1)} of the {FM.fmt_int(x['booked'])} booked for the period."})
            break
        pm = primary_metric(r)
        if pm and r.get("flight_left") and r["status"] != "On track":
            p = r[pm]["pacing_pct"]
            word = "ahead of" if p > 110 else "behind"
            out.append({"type": "observation", "evidence": [f"{fid}.{pm}_pacing_pct"],
                        "text": f"{lab} is pacing {word} plan on {METRIC_LABEL[pm].lower()}: {FM.fmt_pct(p, 1)} of the delivery expected by {(str(asof.day) + ' ' + asof.strftime('%b %Y')) if asof else 'the data end date'}."})
            if p < 90:
                out.append({"type": "recommendation", "evidence": [],
                            "text": f"Review budget allocation, bids and audience size for {lab} to close the gap to the booked {METRIC_LABEL[pm].lower()} before the flight ends."})
        if (r.get("budget") or {}).get("delivery_pct") is not None and r["budget"]["delivery_pct"] > 105 and not r.get("flight_left"):
            out.append({"type": "recommendation", "evidence": [f"{fid}.budget_delivery_pct"],
                        "text": f"Spend on {lab} is above the booked budget for the period; confirm the variance with the client team."})
        rt = (r.get("rates") or {}).get("cpm") or {}
        if rt.get("planned") and rt.get("actual"):
            word = "below" if rt["actual"] < rt["planned"] else "above"
            out.append({"type": "observation", "evidence": [f"{fid}.cpm_planned"],
                        "text": f"{lab} ran at a CPM of {FM.fmt_money(rt['actual'], cur)}, {word} the planned {FM.fmt_money(rt['planned'], cur)}."})
            out[-1]["evidence"].append(f"{fid}.cpm_actual")
    return out


# ---------------------------------------------------------------------------
# Deck columns that belong to a booked-vs-delivered table
# ---------------------------------------------------------------------------
_METRIC_WORDS = [("budget", r"budget|spend|spent|cost|investment|media value"), ("impressions", r"impr|\bimps?\b"), ("reach", r"reach"),
                 ("views", r"views?|plays"), ("clicks", r"clicks?|swipe"), ("engagements", r"engagement")]


def deck_plan_column(label: str) -> tuple[str, str | None] | None:
    """('booked'|'delivered'|'pct'|'pacing'|'status', metric) for a deck table header that is part of
    a booked-vs-delivered table, else None. A plain 'Impressions' or 'Spend' is not a plan column."""
    s = norm_header(label)
    if not s:
        return None
    metric = next((m for m, rx in _METRIC_WORDS if re.search(rx, s)), None)
    if "pacing" in s:
        return ("status", metric) if "status" in s else ("pacing", metric)
    if re.search(r"\bstatus\b|on track", s):
        return ("status", metric)
    if re.search(r"%|percent|delivery rate|achiev|utili[sz]|\bused\b|vs\.? (booked|plan)|of (booked|plan)", s):
        return ("pct", metric)
    if re.search(r"booked|planned|\bplan\b|target|estimated|\best\.?\b|allocated|committed|contracted", s) or s in ("budget", "total budget", "media budget", "net budget"):
        return ("booked", metric or "budget")
    if re.search(r"delivered|actual|achieved", s):
        return ("delivered", metric)
    return None


def is_plan_header(header: list[str]) -> bool:
    kinds = [deck_plan_column(h) for h in header]
    return any(k and k[0] in ("booked", "pct", "pacing") for k in kinds) and sum(1 for k in kinds if k) >= 2


def file_layout(path: str, report_cur: str | None = None) -> str:
    """Layout fingerprint of one plan file (part of the report-structure fingerprint)."""
    try:
        sheets = load_sheets(path)
    except Exception:  # noqa: BLE001
        return "unreadable"
    return layout_fingerprint(sheets, rule_mapping(sheets, report_cur))


def summary_text(insp_sheets: list, path: str, report_cur: str | None = None) -> dict:
    """Quick rules-only reading shown under the file right after upload."""
    try:
        sheets = load_sheets(path)
        rmap = rule_mapping(sheets, report_cur)
        if not rmap:
            return {"read": False, "text": "Plan table not recognised by the built-in rules. Claude will locate it when the report runs."}
        read = apply_mapping(sheets, rmap, None, report_cur)
        checks = total_check(read)
        lines = read["lines"]
        plats = sorted({F.PLATFORM_LABELS.get(ln["platform"], ln["platform"]) for ln in lines if ln["platform"]})
        budget = sum(ln["budget"] or 0 for ln in lines)
        cur = next((ln["currency"] for ln in lines if ln.get("currency")), report_cur or "")
        txt = f"{len(lines)} plan line(s) in {len(read['sheets'])} tab(s)" + (f" · {', '.join(plats)}" if plats else "") + (f" · booked budget {FM.fmt_money(budget, cur, 0)}" if budget else "")
        if checks and all(c["ok"] for c in checks):
            txt += " · matches the plan's Total row"
        elif any(not c["ok"] for c in checks) or not _confident(read, checks):
            txt += " · layout not fully recognised; Claude will check it when the report runs"
        return {"read": bool(lines), "text": txt}
    except Exception as e:  # noqa: BLE001
        return {"read": False, "text": f"Could not read the plan ({type(e).__name__})."}
