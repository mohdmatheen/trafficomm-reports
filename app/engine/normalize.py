"""Normalisation: turn inspected raw files into one canonical dataset with row-level provenance.

Also detects material ambiguities (currency, numeric formats, click/conversion definitions,
overlapping exports, out-of-period data) and turns them into clarification questions instead
of guessing. Safe partial progress (inspection + mapping) is preserved while questions are open.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from . import fields as F
from . import rules as R
from .parsing import is_missing, parse_date, parse_number, read_csv, read_xlsx

DEFAULT_LANG_TOKENS = {"EN": "English", "ENG": "English", "ENGLISH": "English", "AR": "Arabic", "ARB": "Arabic", "ARABIC": "Arabic"}
DEFAULT_COUNTRY_TOKENS = {"UAE": "UAE", "KSA": "KSA", "SAUDI": "KSA", "QATAR": "Qatar", "QA": "Qatar", "KUWAIT": "Kuwait",
                          "KW": "Kuwait", "BAHRAIN": "Bahrain", "OMAN": "Oman", "EGYPT": "Egypt", "AE": "UAE", "SA": "KSA"}
COUNTRY_NAMES = {"united arab emirates": "UAE", "saudi arabia": "KSA", "ae": "UAE", "sa": "KSA", "uae": "UAE", "ksa": "KSA",
                 "qatar": "Qatar", "kuwait": "Kuwait", "bahrain": "Bahrain", "oman": "Oman", "egypt": "Egypt"}

ID_DIMS = ["platform", "campaign_name", "campaign_id", "adset_name", "adset_id", "ad_name", "ad_id", "creative",
           "country", "language", "date", "report_start", "report_end", "objective"]


META_LEAD_INDICATORS = {"leadgen.other", "onsite_conversion.lead_grouped", "lead", "leadgen_grouped"}
META_NON_CONVERSION_INDICATORS = {"link_click", "reach", "impressions", "video_view", "thruplay", "video_thruplay_watched", "post_engagement",
                                  "page_engagement", "landing_page_view", "estimated_ad_recallers", "profile_visit_view"}


def _non_conversion(field_key: str) -> bool:
    """Meta 'Results' for awareness/traffic objectives are not conversions and are never offered for CPA."""
    return field_key.startswith("results__") and field_key[len("results__"):] in {_slug(x) for x in META_NON_CONVERSION_INDICATORS}


@dataclass
class Question:
    key: str
    text: str
    options: list[dict]
    default: str | None = None
    blocking: bool = True
    context: str = ""
    allow_free_text: bool = False

    def as_dict(self):
        return self.__dict__.copy()


@dataclass
class NormResult:
    df: pd.DataFrame
    exclusions: list[dict] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mapping_summary: list[dict] = field(default_factory=list)
    missing_metrics: list[dict] = field(default_factory=list)
    definition_fallback: dict | None = None
    overlaps: list[dict] = field(default_factory=list)
    file_totals: dict = field(default_factory=dict)  # file -> sheet -> {metric: raw sum of included rows}
    platform_totals: list[dict] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)
    dynamic_fields: dict = field(default_factory=dict)
    derivations: dict = field(default_factory=dict)


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    return str(v).replace("​", "").replace("\xa0", " ").strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9#]+", "", (s or "").lower())


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[_\s()\[\]|]+", name or "") if t]


def derive_from_names(row: dict, profile: dict) -> list[str]:
    """Fill brand/market/country/language/format from naming conventions. Returns derivation notes."""
    notes = []
    naming = profile.get("naming") or {}
    names = " ".join(_s(row.get(k)) for k in ("ad_name", "adset_name", "campaign_name"))
    toks = _tokens(names)
    up = [t.upper() for t in toks]
    brand_codes = {k.upper(): v for k, v in (naming.get("brand_codes") or {}).items()}
    if not row.get("brand") and brand_codes:
        for t in up:
            if t in brand_codes:
                row["brand"] = brand_codes[t]
                notes.append("brand")
                break
        else:
            low = names.lower()
            for code, b in (naming.get("brand_phrases") or {}).items():
                if code.lower() in low:
                    row["brand"] = b
                    notes.append("brand")
                    break
    market_tokens = {k.upper(): v for k, v in (naming.get("market_tokens") or {}).items()}
    if not row.get("market"):
        for t in up:
            if t in market_tokens:
                row["market"] = market_tokens[t]
                notes.append("market")
                break
        if not row.get("market") and row.get("brand") and naming.get("brand_market"):
            m = naming["brand_market"].get(row["brand"])
            if m:
                row["market"] = m
                notes.append("market (from brand)")
    lang_tokens = {**DEFAULT_LANG_TOKENS, **{k.upper(): v for k, v in (naming.get("language_tokens") or {}).items()}}
    if not row.get("language"):
        for t in up:
            if t in lang_tokens:
                row["language"] = lang_tokens[t]
                notes.append("language")
                break
    country_tokens = {**DEFAULT_COUNTRY_TOKENS, **{k.upper(): v for k, v in (naming.get("country_tokens") or {}).items()}}
    if not row.get("country"):
        for t in up:
            if t in country_tokens and len(t) > 2:
                row["country"] = country_tokens[t]
                notes.append("country")
                break
        if not row.get("country") and row.get("market") and naming.get("market_country"):
            c = naming["market_country"].get(row["market"])
            if c:
                row["country"] = c
                notes.append("country (from market)")
    fmt_tokens = {k.upper(): v for k, v in (naming.get("format_tokens") or {"VID": "Video", "VIDEO": "Video", "STATIC": "Static", "CAROUSEL": "Carousel"}).items()}
    if not row.get("format"):
        for t in up:
            if t in fmt_tokens:
                row["format"] = fmt_tokens[t]
                notes.append("format")
                break
    if row.get("country"):
        row["country"] = COUNTRY_NAMES.get(row["country"].lower(), row["country"])
    if not row.get("creative"):
        m = re.search(r"\(([^()]*[A-Za-z][^()]*)\)\s*$", _s(row.get("ad_name")))
        if m and not re.match(r"^\d", m.group(1)):
            row["creative"] = m.group(1)
            notes.append("creative (from ad name)")
        elif row.get("ad_name"):
            row["creative"] = row["ad_name"]
    return notes


def _sheet_rows(path: str, ext: str):
    return read_csv(path) if ext == ".csv" else read_xlsx(path)


def build_dataset(files: list[dict], task: dict, profile: dict, answers: dict, plan: dict) -> NormResult:
    """files: [{id, name, path, ext, inspection, uploaded_order}] - raw files only."""
    res = NormResult(df=pd.DataFrame())
    rows: list[dict] = []
    dyn_fields: dict[str, str] = {}
    seen_hash: dict[str, str] = {}
    for f in sorted(files, key=lambda x: x.get("uploaded_order", 0)):
        insp = f["inspection"]
        if not insp.get("ok", True):
            res.exclusions.append({"file": f["name"], "scope": "file", "reason": f"Rejected: {insp.get('error')}"})
            continue
        if insp["sha256"] in seen_hash:
            res.exclusions.append({"file": f["name"], "scope": "file",
                                   "reason": f"Byte-identical duplicate of '{seen_hash[insp['sha256']]}' - excluded to avoid double counting"})
            res.overlaps.append({"type": "identical_file", "files": [seen_hash[insp["sha256"]], f["name"]]})
            continue
        if answers.get(f"exclude_file:{f['id']}") == "yes":
            res.exclusions.append({"file": f["name"], "scope": "file", "reason": "Excluded by employee"})
            continue
        seen_hash[insp["sha256"]] = f["name"]
        raw_sheets = {s.name: s for s in _sheet_rows(f["path"], f["ext"])}
        for si in insp["sheets"]:
            if not si.get("is_data_sheet"):
                if si.get("rows_total"):
                    res.exclusions.append({"file": f["name"], "sheet": si["name"], "scope": "sheet",
                                           "reason": si.get("reason") or "No reporting metrics found in this sheet"})
                continue
            for b in si.get("summary_blocks", []):
                res.exclusions.append({"file": f["name"], "sheet": si["name"], "scope": f"rows {b['start']}-{b['end']}", "reason": b["reason"]})
            for ex in si.get("excluded_rows", []):
                res.exclusions.append({"file": f["name"], "sheet": si["name"], "scope": f"row {ex['row']}", "reason": ex["reason"]})
            for t in si.get("total_rows", []):
                res.platform_totals.append({"file": f["name"], "sheet": si["name"], **t})
            rows.extend(_normalise_sheet(f, si, raw_sheets[si["name"]], task, profile, answers, res, dyn_fields))
    res.dynamic_fields = dyn_fields
    if not rows:
        res.df = pd.DataFrame()
        return res
    df = pd.DataFrame(rows)
    df = _currency_step(df, task, answers, res)
    df = _overlap_step(df, files, answers, res)
    df = _period_step(df, task, answers, res)
    _definition_questions(df, plan, profile, answers, res)
    res.df = df
    return res


def _normalise_sheet(f, si, raw, task, profile, answers, res: NormResult, dyn_fields) -> list[dict]:
    header_i = si["header_row"] - 1
    cols = si["columns"]
    platform = si.get("platform") or "unknown"
    if platform == "unknown":
        ans = answers.get(f"platform:{f['id']}:{si['name']}")
        if ans:
            platform = ans
        else:
            res.questions.append(Question(
                key=f"platform:{f['id']}:{si['name']}",
                text=f"Which platform is '{f['name']}' ({si['name']}) exported from? The headers don't match a known export.",
                options=[{"value": k, "label": v} for k, v in F.PLATFORM_LABELS.items() if k != "unknown"],
                context="Platform decides which click/video definitions apply."))
    # numeric format decisions
    col_fmt = {}
    for c in cols:
        if not c.get("canonical"):
            continue
        fmtc = c.get("numeric_format", "plain")
        fld = F.FIELDS[c["canonical"]]
        if fmtc in ("ambiguous", "us_or_eu_thousands"):
            key = f"numfmt:{f['id']}:{si['name']}:{c['index']}"
            if fmtc == "us_or_eu_thousands" and fld.unit == "count":
                col_fmt[c["index"]] = "us"
                res.assumptions.append(f"'{c['original']}' in {f['name']}: values like '1,234' read as thousands separators (fractional counts are impossible)")
            elif answers.get(key):
                col_fmt[c["index"]] = answers[key]
            else:
                col_fmt[c["index"]] = "us"
                res.questions.append(Question(key=key, text=f"Column '{c['original']}' in {f['name']} has values like '{(c.get('sample') or ['?'])[0]}'. Is the comma a thousands separator or a decimal separator?",
                                              options=[{"value": "us", "label": "Thousands separator (1,234 = one thousand two hundred thirty-four)"},
                                                       {"value": "eu", "label": "Decimal separator (1,234 = 1.234)"}],
                                              context="Ambiguous numeric format would change monetary totals."))
        else:
            col_fmt[c["index"]] = fmtc
    # date format decisions
    date_fmt = {}
    for key in ("date", "report_start", "report_end"):
        d = si.get("dates", {}).get(f"{key}_format")
        if not d:
            continue
        fmt = d.get("format")
        if d.get("ambiguous"):
            qk = f"datefmt:{f['id']}:{si['name']}"
            if answers.get(qk):
                fmt = answers[qk]
            else:
                res.questions.append(Question(key=qk, text=f"Dates in {f['name']} could be day/month or month/day (e.g. '{_first_sample(cols, key)}'). Which is it?",
                                              options=[{"value": "%d/%m/%Y", "label": "Day/Month/Year"}, {"value": "%m/%d/%Y", "label": "Month/Day/Year"}],
                                              context="Wrong date order would put data in the wrong period."))
        date_fmt[key] = fmt
    sheet_currency = (si.get("currency") or {}).get("value")
    out = []
    data = raw.rows
    excluded_rows = {e["row"] for e in si.get("excluded_rows", [])}
    start, end = si["data_rows"] if si.get("data_rows") else (0, -1)
    summary_start = min([b["start"] for b in si.get("summary_blocks", [])] or [10**9])
    for ri in range(start - 1, min(end, summary_start - 1)):
        rown = ri + 1
        if rown in excluded_rows:
            continue
        r = list(data[ri]) + [None] * (len(cols) - len(data[ri]))
        if all(is_missing(v) for v in r):
            continue
        row: dict = {"_file_id": f["id"], "_file": f["name"], "_sheet": si["name"], "_row": rown, "platform": platform}
        for c in cols:
            k = c.get("canonical")
            if not k:
                continue
            v = r[c["index"]]
            fld = F.FIELDS[k]
            if fld.kind == "dimension":
                if k in ("date", "report_start", "report_end"):
                    try:
                        row[k] = parse_date(v, date_fmt.get(k))
                    except ValueError:
                        row[k] = None
                        res.warnings.append(f"{f['name']} row {rown}: unparseable {fld.label} '{v}'")
                else:
                    sv = _s(v)
                    if k == "platform" and sv:
                        sv = _platform_from_value(sv) or platform
                    row[k] = sv or None
            else:
                try:
                    row[k] = parse_number(v, col_fmt.get(c["index"], "plain"))
                except ValueError:
                    row[k] = None
                    res.warnings.append(f"{f['name']} row {rown}: non-numeric '{v}' in {c['original']} treated as missing")
        # Results split by indicator (objective-dependent)
        if "results" in row:
            ind = _s(row.get("result_indicator"))
            val = row.pop("results")
            if ind:
                short = ind.split(":")[-1]
                if short in META_LEAD_INDICATORS:
                    row["leads_meta_form"] = val
                elif short == "link_click":
                    row["results__link_click"] = val
                    dyn_fields["results__link_click"] = "Meta Results counted as link clicks (duplicates 'Link clicks'; used only for checks)"
                elif short in META_NON_CONVERSION_INDICATORS:
                    # e.g. Results = Reach for awareness ads: duplicates an existing column and reach is
                    # not additive, so it is not carried as a separate metric
                    pass
                else:
                    fk = "results__" + _slug(short)
                    row[fk] = val
                    dyn_fields[fk] = f"Meta Results with indicator '{ind}' (platform-attributed)" + (" - not a conversion" if short in META_NON_CONVERSION_INDICATORS else "")
        if not row.get("currency"):
            row["currency"] = sheet_currency if sheet_currency and not sheet_currency.startswith("MIXED") else None
        notes = derive_from_names(row, profile)
        for n in notes:
            res.derivations[n] = res.derivations.get(n, 0) + 1
        row["_row_start"] = row.get("date") or row.get("report_start")
        row["_row_end"] = row.get("date") or row.get("report_end") or row.get("report_start")
        out.append(row)
    # core metrics absent from this sheet are reported as missing (N/A), never as zero
    have = {c["canonical"] for c in cols if c.get("canonical")}
    absent = [lbl for keys, lbl in ((("spend",), "spend"), (("impressions",), "impressions"),
                                    (("link_clicks", "clicks_all", "clicks_unspecified", "swipe_ups"), "clicks")) if not have.intersection(keys)]
    if absent and out:
        res.missing_metrics.append({"file": f["name"], "sheet": si["name"], "missing": absent})
        res.warnings.append(f"{f['name']} ({si['name']}): no {', '.join(absent)} column - "
                            "these metrics and rates that depend on them are shown as N/A for its rows, not zero")
    # mapping summary
    res.mapping_summary.append({
        "file": f["name"], "file_id": f["id"], "sheet": si["name"], "platform": platform, "rows": len(out),
        "currency": sheet_currency, "mapped": [{"original": c["original"], "canonical": c["canonical"], "definition": c.get("definition", ""), "source": c.get("source")} for c in cols if c.get("canonical")],
        "ignored": [c["original"] for c in cols if c["status"] == "ignored" and c["original"]],
        "unresolved": [c["original"] for c in cols if c["status"] == "unresolved" and c["original"]],
    })
    return out


def _first_sample(cols, key):
    for c in cols:
        if c.get("canonical") == key and c.get("sample"):
            return c["sample"][0]
    return "?"


def _platform_from_value(v: str) -> str | None:
    s = v.lower()
    if any(w in s for w in ("facebook", "instagram", "meta")):
        return "meta"
    if "tiktok" in s:
        return "tiktok"
    if "snap" in s:
        return "snapchat"
    if "google" in s:
        return "google_ads"
    return None


# ---------------------------------------------------------------------------
def _currency_step(df: pd.DataFrame, task, answers, res: NormResult) -> pd.DataFrame:
    target = (task.get("currency") or "").upper() or None
    money = [m for m in F.MONEY if m in df.columns]
    df["_fx_rate"] = 1.0
    df["_orig_currency"] = df["currency"]
    for m in money:
        df[f"_orig_{m}"] = df[m]
    # agency rule: Snapchat and TikTok raw reports are in USD
    for plat, src in R.PLATFORM_SOURCE_CURRENCY.items():
        sel = (df["platform"] == plat)
        if not sel.any():
            continue
        miss = sel & df["currency"].isna()
        if miss.any():
            df.loc[miss, "currency"] = src
            df.loc[miss, "_orig_currency"] = src
            res.assumptions.append(f"{F.PLATFORM_LABELS.get(plat, plat)} values read as {src} (agency rule: {F.PLATFORM_LABELS.get(plat, plat)} raw reports are in {src})")
        other = sorted({c for c in df.loc[sel & df["currency"].notna(), "currency"].unique() if c != src})
        if other:
            res.warnings.append(f"{F.PLATFORM_LABELS.get(plat, plat)} export states {', '.join(other)}, but the agency rule says {src} - kept the currency stated in the file. Check the export.")
    missing_cur = df[df["currency"].isna()]
    for fname in sorted(missing_cur["_file"].unique()):
        fid = missing_cur[missing_cur["_file"] == fname]["_file_id"].iloc[0]
        key = f"currency:{fid}"
        if answers.get(key):
            df.loc[(df["_file"] == fname) & df["currency"].isna(), "currency"] = answers[key]
            df.loc[(df["_file"] == fname), "_orig_currency"] = answers[key]
            res.assumptions.append(f"Currency of {fname} confirmed by employee as {answers[key]}")
        else:
            res.questions.append(Question(key=key, text=f"'{fname}' has no currency indicator in its headers or metadata. Which currency are its monetary values in?",
                                          options=[{"value": c, "label": c} for c in ([target] if target else []) + [c for c in ("AED", "USD", "SAR", "EUR", "GBP") if c != target]],
                                          default=target, context="Currency is never assumed."))
    curs = sorted({c for c in df["currency"].dropna().unique()})
    res.currencies = curs
    if target is None and len(curs) == 1:
        target = curs[0]
    if not money:
        return df
    for c in curs:
        if c == target:
            continue
        key = f"fx:{c}->{target}"
        rate = answers.get(key)
        if not rate and c == "USD" and target == "AED":
            rate = R.usd_aed_rate(task)
        if rate:
            try:
                r = float(rate)
            except ValueError:
                r = None
            if r and r > 0:
                sel = df["currency"] == c
                for m in money:
                    df.loc[sel, m] = df.loc[sel, m] * r
                df.loc[sel, "_fx_rate"] = r
                df.loc[sel, "currency"] = target
                res.assumptions.append(f"{c} converted to {target} at {r} (" + ("rate set on the task" if c == "USD" and target == "AED" and not answers.get(key) else "employee-approved rate") + ", recorded on each row)")
                continue
        res.questions.append(Question(
            key=key, text=f"Spend is reported in {c} for {', '.join(sorted(df[df['currency']==c]['_file'].unique()))}, but the reporting currency is {target}. Enter the approved exchange rate (1 {c} = ? {target}).",
            options=[], allow_free_text=True, context="Currencies are never combined without an approved, recorded exchange rate."))
    return df


def _row_key(r) -> tuple:
    return tuple((k, str(r.get(k))) for k in ID_DIMS if k in r and r.get(k) is not None and str(r.get(k)) != "nan")


def _overlap_step(df: pd.DataFrame, files, answers, res: NormResult) -> pd.DataFrame:
    metrics = [m for m in F.ADDITIVE + F.NONADDITIVE if m in df.columns]
    df["_excluded"] = None
    order = {f["id"]: f.get("uploaded_order", 0) for f in files}
    df["_key"] = [_row_key(r) for r in df.to_dict("records")]
    groups = defaultdict(list)
    for idx, k, fid in zip(df.index, df["_key"], df["_file_id"]):
        groups[k].append((idx, fid))
    conflicts = defaultdict(lambda: {"rows": 0, "idx": []})
    dup_pairs = defaultdict(int)
    for k, items in groups.items():
        fids = {fid for _, fid in items}
        if len(fids) < 2:
            continue
        items.sort(key=lambda x: order.get(x[1], 0))
        keep_idx, keep_f = items[0]
        for idx, fid in items[1:]:
            if fid == keep_f:
                continue  # repeated observation within one file is legitimate; keep
            a = df.loc[keep_idx, metrics]
            b = df.loc[idx, metrics]
            same = all((pd.isna(x) and pd.isna(y)) or (not pd.isna(x) and not pd.isna(y) and abs(float(x) - float(y)) < 1e-9) for x, y in zip(a, b))
            fa, fb = df.loc[keep_idx, "_file"], df.loc[idx, "_file"]
            if same:
                df.loc[idx, "_excluded"] = f"Duplicate of {fa} row {df.loc[keep_idx, '_row']} (same identifiers, dates and figures)"
                dup_pairs[(fa, fb)] += 1
            else:
                conflicts[(fa, fb)]["rows"] += 1
                conflicts[(fa, fb)]["idx"].append((keep_idx, idx))
    for (fa, fb), n in dup_pairs.items():
        res.overlaps.append({"type": "duplicate_rows", "files": [fa, fb], "rows": n})
    for (fa, fb), c in conflicts.items():
        key = f"overlap:{_slug(fa)}|{_slug(fb)}"
        ans = answers.get(key)
        res.overlaps.append({"type": "conflicting_rows", "files": [fa, fb], "rows": c["rows"], "resolution": ans})
        if ans in ("a", "b"):
            for ia, ib in c["idx"]:
                drop, keepf = (ib, fa) if ans == "a" else (ia, fb)
                df.loc[drop, "_excluded"] = f"Overlaps {keepf}; employee chose to use {keepf}"
        elif ans == "both":
            res.assumptions.append(f"Employee confirmed overlapping rows in {fa} and {fb} are separate observations")
        else:
            res.questions.append(Question(key=key, text=f"'{fa}' and '{fb}' contain {c['rows']} rows for the same ads and dates with different figures (likely re-exports). Which should be used?",
                                          options=[{"value": "a", "label": f"Use {fa}"}, {"value": "b", "label": f"Use {fb}"},
                                                   {"value": "both", "label": "They are separate observations - keep both"}],
                                          default="b", context="Overlapping exports would double count."))
    # granularity overlap: daily rows vs aggregated rows for the same platform/campaign over overlapping dates
    if "date" in df.columns and ("report_start" in df.columns):
        daily = df[df["date"].notna() & df["_excluded"].isna()]
        agg = df[df["date"].isna() & df["report_start"].notna() & df["_excluded"].isna()]
        if len(daily) and len(agg):
            for fa in daily["_file"].unique():
                for fb in agg["_file"].unique():
                    if fa == fb:
                        continue
                    da, db = daily[daily["_file"] == fa], agg[agg["_file"] == fb]
                    camp_a = set(da.get("campaign_name", pd.Series(dtype=str)).dropna())
                    camp_b = set(db.get("campaign_name", pd.Series(dtype=str)).dropna())
                    if not (camp_a & camp_b) or set(da["platform"]) != set(db["platform"]):
                        continue
                    if da["date"].min() <= db["_row_end"].max() and da["date"].max() >= db["_row_start"].min():
                        key = f"overlap:{_slug(fa)}|{_slug(fb)}"
                        ans = answers.get(key)
                        if ans == "a":
                            df.loc[df["_file"] == fb, "_excluded"] = f"Covers the same campaigns/dates as {fa} at a different granularity; employee chose {fa}"
                        elif ans == "b":
                            df.loc[df["_file"] == fa, "_excluded"] = f"Covers the same campaigns/dates as {fb} at a different granularity; employee chose {fb}"
                        else:
                            res.questions.append(Question(key=key, text=f"'{fa}' (daily rows) and '{fb}' (one row per ad for the whole window) cover the same campaigns and dates. Which should be used?",
                                                          options=[{"value": "a", "label": f"Use {fa} (daily)"}, {"value": "b", "label": f"Use {fb} (aggregated)"}],
                                                          context="Same data at two granularities would double count."))
                        res.overlaps.append({"type": "granularity_overlap", "files": [fa, fb], "resolution": ans})
    within = df[df["_excluded"].isna()].duplicated(subset=["_file_id", "_key"] + metrics, keep=False)
    if within.any():
        res.warnings.append(f"{int(within.sum())} rows are exact repeats within the same file; kept as legitimate repeated observations - please review")
    return df


def _period_step(df: pd.DataFrame, task, answers, res: NormResult) -> pd.DataFrame:
    ps, pe = task.get("period_start"), task.get("period_end")
    cs, ce = task.get("comparison_start"), task.get("comparison_end")
    live = df[df["_excluded"].isna()]
    dmin = live["_row_start"].dropna().min() if len(live) else None
    dmax = live["_row_end"].dropna().max() if len(live) else None
    if not ps or not pe:
        ps, pe = dmin, dmax
        res.assumptions.append(f"No reporting period given - using the data's date span {dmin} to {dmax}")
    df["_period"] = None
    for idx, r in df.iterrows():
        if r["_excluded"]:
            continue
        a, b = r["_row_start"], r["_row_end"]
        if a is None or (isinstance(a, float)):
            df.loc[idx, "_period"] = "current"
            continue
        if ps and pe and a >= ps and b <= pe:
            df.loc[idx, "_period"] = "current"
        elif cs and ce and a >= cs and b <= ce:
            df.loc[idx, "_period"] = "comparison"
        elif (ps and pe and b >= ps and a <= pe):
            key = f"partial_period:{r['_file_id']}"
            ans = answers.get(key)
            if ans == "include":
                df.loc[idx, "_period"] = "current"
            elif ans == "exclude":
                df.loc[idx, "_excluded"] = f"Window {a}–{b} extends beyond the reporting period; excluded by employee"
            else:
                df.loc[idx, "_period"] = "current"
                if not any(q.key == key for q in res.questions):
                    res.questions.append(Question(key=key, text=f"'{r['_file']}' covers {a} to {b}, which extends beyond the reporting period {ps} to {pe}. Aggregated rows can't be split by day. Include them as-is or exclude them?",
                                                  options=[{"value": "include", "label": "Include as-is (label the actual window)"}, {"value": "exclude", "label": "Exclude these rows"}],
                                                  context="Reporting scope is never assumed."))
        else:
            df.loc[idx, "_excluded"] = f"Outside the reporting period ({a} to {b})"
    cur = df[(df["_period"] == "current") & df["_excluded"].isna()]
    cov_min = cur["_row_start"].dropna().min() if len(cur) else None
    cov_max = cur["_row_end"].dropna().max() if len(cur) else None
    res.period = {"start": ps, "end": pe, "comparison_start": cs, "comparison_end": ce,
                  "data_start": cov_min, "data_end": cov_max}
    if cov_min and ps and (cov_min > ps or cov_max < pe):
        res.warnings.append(f"Data covers {cov_min} to {cov_max}, not the full reporting period {ps} to {pe}. Labels use the actual data window.")
    if cs and ce:
        comp = df[(df["_period"] == "comparison") & df["_excluded"].isna()]
        res.coverage["comparison_rows"] = int(len(comp))
        if comp.empty:
            res.warnings.append("A comparison period was set but no uploaded rows fall inside it - comparison will be N/A")
    return df


def _definition_questions(df: pd.DataFrame, plan: dict, profile: dict, answers: dict, res: NormResult):
    live = df[df["_excluded"].isna()]
    platforms = sorted(live["platform"].dropna().unique())
    approved = set(map(tuple, profile.get("approved_equivalences", []))) | set(map(tuple, plan.get("approved_equivalences", [])))
    # agency rule: clicks are always the raw report's link-click metric
    requested = plan.get("click_metric")
    click_metric = R.CLICK_METRIC
    if requested and requested != click_metric:
        res.warnings.append("The instructions asked for all clicks, but the agency rule is to always report link clicks - kept link clicks.")
    # unspecified "Clicks" columns
    if "clicks_unspecified" in live.columns and live["clicks_unspecified"].notna().any():
        for p in sorted(live[live["clicks_unspecified"].notna()]["platform"].unique()):
            key = f"clicks_def:{p}"
            ans = answers.get(key) or (profile.get("clicks_definitions") or {}).get(p)
            if ans in ("link_clicks", "clicks_all"):
                sel = (df["platform"] == p) & df["clicks_unspecified"].notna()
                if ans not in df.columns:
                    df[ans] = None
                df.loc[sel & df[ans].isna(), ans] = df.loc[sel, "clicks_unspecified"]
                res.assumptions.append(f"{F.PLATFORM_LABELS.get(p, p)} 'Clicks' treated as {F.FIELDS[ans].label} (confirmed)")
            elif ans != "separate":
                res.questions.append(Question(key=key, text=f"The {F.PLATFORM_LABELS.get(p, p)} export has a column named just 'Clicks'. What does it measure?",
                                              options=[{"value": "link_clicks", "label": "Link / destination clicks"}, {"value": "clicks_all", "label": "All clicks"},
                                                       {"value": "separate", "label": "Unsure - keep it separate (no CTR/CPC for this platform)"}],
                                              context="CTR and CPC depend on which clicks are used."))
    # a requested clicks definition that no uploaded file reports is not applied silently (it would
    # turn every click figure into N/A); keep the definition the data supports and say so
    def _has(col):
        return col in df.columns and df.loc[df["_excluded"].isna(), col].notna().any()
    other = "link_clicks" if click_metric == "clicks_all" else "clicks_all"
    if not _has(click_metric) and _has(other):
        label = {"link_clicks": "Link clicks", "clicks_all": "Clicks (all)"}
        res.warnings.append(f"Requested clicks definition '{label[click_metric]}' is not in any uploaded export - kept '{label[other]}'. "
                            f"Re-export with a '{label[click_metric]}' column (or upload it as a revision) to switch.")
        res.definition_fallback = {"requested": click_metric, "used": other}
        click_metric = other
    # swipe-up equivalence
    if "snapchat" in platforms and click_metric == "link_clicks" and "swipe_ups" in live.columns and live["swipe_ups"].notna().any():
        key = "equiv:snapchat:swipe_ups=link_clicks"
        ans = answers.get(key)
        if R.SNAPCHAT_SWIPE_UPS_ARE_LINK_CLICKS or ("snapchat", "swipe_ups", "link_clicks") in approved or ans == "yes":
            res.assumptions.append("Snapchat Swipe Ups reported as link clicks (agency rule)")
            plan.setdefault("_equivalences", []).append(("snapchat", "swipe_ups", "link_clicks"))
        elif ans == "no":
            res.assumptions.append("Snapchat Swipe Ups kept separate: Snapchat excluded from cross-platform click totals, CTR and CPC")
        else:
            res.questions.append(Question(key=key, text=F.EQUIVALENCE_CANDIDATES[("snapchat", "swipe_ups", "link_clicks")],
                                          options=[{"value": "yes", "label": "Yes - treat Swipe Ups as link clicks"},
                                                   {"value": "no", "label": "No - keep separate (Snapchat CTR/CPC shown on its own)"}],
                                          default="no", context="Different platforms define clicks differently."))
    # conversion metric for CPA
    conv_candidates = [k for k in ["leads_meta_form", "website_leads", "leads", "conversions", "purchases", "installs"] + sorted(res.dynamic_fields)
                       if k in live.columns and live[k].notna().any() and not _non_conversion(k)]
    chosen = plan.get("conversion_metric") or answers.get("conversion_metric") or profile.get("conversion_metric")
    if chosen and chosen != "none" and chosen not in conv_candidates and conv_candidates:
        chosen = None
    if conv_candidates and not chosen and plan.get("needs_cpa", True):
        res.questions.append(Question(
            key="conversion_metric",
            text="Which conversion should CPA (cost per acquisition) use? The files contain: " + ", ".join(_conv_label(k, res) for k in conv_candidates) + ". These are platform-attributed and are not combined.",
            options=[{"value": k, "label": _conv_label(k, res)} for k in conv_candidates] + [{"value": "none", "label": "No CPA - report conversions only"}],
            default="none", blocking=True, context="Conversion definitions are never assumed."))
    plan["_conversion_metric"] = chosen if chosen and chosen != "none" else None
    plan["_click_metric"] = click_metric


def _conv_label(k: str, res: NormResult) -> str:
    if k in F.FIELDS:
        return F.FIELDS[k].label
    return res.dynamic_fields.get(k, k)
