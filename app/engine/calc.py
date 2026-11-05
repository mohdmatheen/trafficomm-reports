"""Deterministic calculations. All reporting figures come from here - never from the language model.

Rules implemented:
* additive metrics are summed only across compatible scopes (selected click metric per platform,
  explicit conversion metric, approved equivalences, single currency);
* aggregate rates are recomputed from summed numerators/denominators over rows where BOTH are present;
* zero denominators -> None (N/A), missing stays missing (None) and is distinct from 0;
* reach / unique metrics are never summed: they are only reported at single-row (ad-level) scope;
* full precision is retained; rounding happens only in presentation layers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from . import fields as F

RATE_DEFS = {
    "ctr": ("clicks", "impressions", 100.0, "CTR = clicks / impressions x 100 (clicks = selected click metric)"),
    "cpc": ("spend", "clicks", 1.0, "CPC = spend / clicks"),
    "cpm": ("spend", "impressions", 1000.0, "CPM = spend / impressions x 1,000"),
    "cpa": ("spend", "conversions", 1.0, "CPA = spend / selected conversion metric, over rows that report that conversion (rows where it is missing are excluded, not treated as zero)"),
    "roas": ("purchase_value", "spend", 1.0, "ROAS = attributed purchase value / spend (same currency)"),
    "vtr": ("video_plays_3s", "impressions", 100.0, "VTR = 3-second video plays / impressions x 100"),
}
SUM_METRICS = ["spend", "impressions", "clicks", "link_clicks", "clicks_all", "swipe_ups", "landing_page_views",
               "video_plays_3s", "video_plays_2s", "video_views_6s", "video_views_tiktok", "video_views_snap", "thruplays",
               "video_completions", "conversions", "leads_meta_form", "website_leads", "leads", "purchases", "purchase_value", "installs"]

METRIC_LABELS = {**{k: f.label for k, f in F.FIELDS.items()}, "clicks": "Clicks", "conversions": "Conversions",
                 "ctr": "CTR", "cpc": "CPC", "cpm": "CPM", "cpa": "CPA", "roas": "ROAS", "vtr": "VTR (3s)",
                 "frequency_calc": "Frequency", "reach": "Reach"}


def safe_div(n, d, mult=1.0):
    if n is None or d is None:
        return None
    try:
        if (isinstance(n, float) and math.isnan(n)) or (isinstance(d, float) and math.isnan(d)):
            return None
    except TypeError:
        return None
    if d == 0:
        return None
    return float(n) / float(d) * mult


def _nn(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


@dataclass
class Table:
    key: str
    title: str
    dims: list[str]
    rows: list[dict]
    metrics: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass
class CalcResult:
    totals: dict
    tables: dict[str, Table]
    facts: dict
    definitions: dict
    settings: dict
    comparison: dict | None
    included_rows: int
    currency: str | None
    click_metric_label: str
    conversion_label: str | None
    notes: list[str] = field(default_factory=list)
    dataset: pd.DataFrame | None = None

    def table(self, key):
        return self.tables.get(key)


def prepare(df: pd.DataFrame, plan: dict, dynamic_fields: dict) -> pd.DataFrame:
    """Add selected 'clicks' and 'conversions' columns per row according to plan/equivalences."""
    d = df[(df["_excluded"].isna()) & df["_period"].notna()].copy()
    cm = plan.get("_click_metric") or "link_clicks"
    eq = set(tuple(e) for e in plan.get("_equivalences", []))
    clicks = []
    for _, r in d.iterrows():
        p = r.get("platform") or "unknown"
        fld = F.CLICK_METRIC_FIELDS.get(cm, {}).get(p, cm)
        v = r.get(fld) if fld and fld in d.columns else None
        if (v is None or (isinstance(v, float) and math.isnan(v))) and (p, "swipe_ups", cm) in eq and "swipe_ups" in d.columns:
            v = r.get("swipe_ups")
        clicks.append(_nn(v))
    d["clicks"] = clicks
    conv = plan.get("_conversion_metric")
    d["conversions"] = d[conv] if conv and conv in d.columns else None
    for m in SUM_METRICS + ["reach", "unique_link_clicks", "frequency"] + list(dynamic_fields):
        if m not in d.columns:
            d[m] = None
        d[m] = pd.to_numeric(d[m], errors="coerce")
    return d


def aggregate(d: pd.DataFrame, dims: list[str], dynamic_fields: dict | None = None) -> list[dict]:
    """Group and compute metrics. Returns list of row dicts (full precision)."""
    dyn = list(dynamic_fields or {})
    if d.empty:
        return []
    work = d.copy()
    for dim in dims:
        work[dim] = work[dim].fillna("(not set)") if dim in work.columns else "(not set)"
    groups = work.groupby(dims, dropna=False, sort=False) if dims else [((), work)]
    out = []
    for key, g in groups:
        if not isinstance(key, tuple):
            key = (key,)
        row = {dim: (str(k) if not hasattr(k, "isoformat") else k.isoformat()) for dim, k in zip(dims, key)}
        row["_rows"] = int(len(g))
        row["_files"] = sorted(g["_file"].unique().tolist())
        for m in SUM_METRICS + dyn:
            s = g[m].sum(min_count=1) if m in g.columns else float("nan")
            row[m] = None if pd.isna(s) else float(s)
        # rates over compatible rows only
        for rate, (num, den, mult, _defn) in RATE_DEFS.items():
            if num not in g.columns or den not in g.columns:
                row[rate] = None
                continue
            both = g[num].notna() & g[den].notna()
            n = g.loc[both, num].sum(min_count=1)
            dd = g.loc[both, den].sum(min_count=1)
            row[rate] = safe_div(None if pd.isna(n) else n, None if pd.isna(dd) else dd, mult)
            row[f"_{rate}_coverage"] = f"{int(both.sum())}/{len(g)}"
        # non-additive: only at single-row scope
        if len(g) == 1:
            row["reach"] = _nn(g["reach"].iloc[0])
            row["unique_link_clicks"] = _nn(g["unique_link_clicks"].iloc[0]) if "unique_link_clicks" in g else None
            row["frequency_calc"] = safe_div(row["impressions"], row["reach"])
            row["_reach_note"] = None
        else:
            row["reach"] = None
            row["unique_link_clicks"] = None
            row["frequency_calc"] = None
            row["_reach_note"] = "Not additive - unique reach cannot be summed across rows"
        out.append(row)
    return out


def available_metrics(d: pd.DataFrame, dyn: dict) -> list[str]:
    base = ["spend", "impressions", "reach", "frequency_calc", "clicks", "ctr", "cpc", "cpm"]
    extra = []
    if d["video_plays_3s"].notna().any():
        extra += ["video_plays_3s", "vtr"]
    for m in ["video_views_6s", "video_views_tiktok", "video_views_snap", "thruplays", "swipe_ups", "landing_page_views",
              "leads_meta_form", "website_leads", "leads", "purchases", "purchase_value", "installs"]:
        if d[m].notna().any():
            extra.append(m)
    if d["conversions"].notna().any():
        extra += ["conversions", "cpa"]
    if d["purchase_value"].notna().any() and d["spend"].notna().any():
        extra.append("roas")
    return base + [e for e in extra if e not in base]


def calculate(norm_df: pd.DataFrame, plan: dict, dynamic_fields: dict, currency: str | None) -> CalcResult:
    d_all = prepare(norm_df, plan, dynamic_fields)
    cur = d_all[d_all["_period"] == "current"]
    comp = d_all[d_all["_period"] == "comparison"]
    metrics = available_metrics(cur, dynamic_fields) if len(cur) else []
    tables: dict[str, Table] = {}
    totals = aggregate(cur, [], dynamic_fields)
    totals = totals[0] if totals else {}
    notes = []

    def add(key, title, dims, extra_notes=None):
        dims_ok = [x for x in dims if x in cur.columns and cur[x].notna().any()]
        if len(dims_ok) != len(dims):
            return
        rows = aggregate(cur, dims, dynamic_fields)
        rows.sort(key=lambda r: -(r.get("spend") or 0))
        tables[key] = Table(key, title, dims, rows, metrics, extra_notes or [])

    add("platform", "Platform summary", ["platform"])
    add("brand", "Brand / project summary", ["brand"])
    add("campaign", "Campaign performance", ["platform", "campaign_name"])
    add("objective", "Objective summary", ["objective"])
    add("creative", "Creative performance", ["brand", "objective", "creative"] if "brand" in cur.columns and cur["brand"].notna().any() else ["objective", "creative"])
    add("ad", "Ad-level performance", ["platform", "ad_name"])
    add("country", "Country breakdown", ["country"])
    add("market", "Market breakdown", ["market"])
    add("language", "Language breakdown", ["language"])
    for b in plan.get("breakdowns", []):
        dims = [x for x in b if x]
        key = "x_" + "_".join(dims)
        if key not in tables and not any(tb.dims == dims or tb.dims[-len(dims):] == dims and len(dims) == 1 for tb in tables.values()):
            add(key, " x ".join(METRIC_LABELS.get(x, F.FIELDS[x].label if x in F.FIELDS else x) for x in dims) + " breakdown", dims)

    comparison = None
    if len(comp):
        ct = aggregate(comp, [], dynamic_fields)
        ct = ct[0] if ct else {}
        comparison = {"current": totals, "previous": ct, "deltas": {}}
        for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa", "video_plays_3s", "vtr"]:
            a, b = totals.get(m), ct.get(m)
            comparison["deltas"][m] = {"abs": (a - b) if a is not None and b is not None else None,
                                       "pct": safe_div((a - b) if a is not None and b is not None else None, b, 100.0)}
        if "platform" in cur.columns:
            prev_rows = {r["platform"]: r for r in aggregate(comp, ["platform"], dynamic_fields)}
            comparison["by_platform"] = [{"platform": r["platform"], "current": r, "previous": prev_rows.get(r["platform"])}
                                         for r in aggregate(cur, ["platform"], dynamic_fields)]

    cm = plan.get("_click_metric") or "link_clicks"
    conv = plan.get("_conversion_metric")
    definitions = {r: RATE_DEFS[r][3] for r in RATE_DEFS}
    definitions["clicks"] = f"Clicks = {F.FIELDS[cm].label}" + (" (+ Snapchat Swipe Ups, approved equivalence)" if any(e[0] == "snapchat" for e in plan.get("_equivalences", [])) else "")
    definitions["conversions"] = (F.FIELDS[conv].label if conv in F.FIELDS else dynamic_fields.get(conv, conv)) if conv else "No conversion metric selected - CPA not calculated"
    definitions["reach"] = "Reach is shown only at ad level. It is not additive and is never summed across ads, platforms or periods."
    definitions["frequency"] = "Frequency = impressions / reach, at ad level only"
    facts = build_facts(totals, tables, comparison, currency)
    facts["total.included_rows"] = {"value": int(len(cur)), "metric": "rows", "scope": "Included rows", "unit": "count", "currency": None}
    for rate in ("cpa", "ctr", "vtr"):
        cov = totals.get(f"_{rate}_coverage")
        if cov:
            facts[f"total.{rate}_rows"] = {"value": int(cov.split("/")[0]), "metric": "rows", "scope": f"Rows used for {rate.upper()}", "unit": "count", "currency": None}
    return CalcResult(totals=totals, tables=tables, facts=facts, definitions=definitions,
                      settings={"click_metric": cm, "conversion_metric": conv, "equivalences": plan.get("_equivalences", [])},
                      comparison=comparison, included_rows=int(len(cur)), currency=currency,
                      click_metric_label=F.FIELDS[cm].label,
                      conversion_label=definitions["conversions"] if conv else None, notes=notes, dataset=d_all)


def build_facts(totals, tables, comparison, currency) -> dict:
    """Facts registry: every number the commentary may cite, with provenance."""
    facts = {}

    def put(fid, value, metric, scope, unit=None):
        if value is None:
            return
        facts[fid] = {"value": value, "metric": metric, "scope": scope, "unit": unit or _unit(metric), "currency": currency}

    for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa", "video_plays_3s", "vtr", "roas", "leads_meta_form", "website_leads"]:
        put(f"total.{m}", totals.get(m), m, "All included data")
    for tkey in ("platform", "brand", "objective", "creative", "country", "language", "market", "campaign"):
        t = tables.get(tkey)
        if not t:
            continue
        for r in t.rows:
            label = " / ".join(str(r[d]) for d in t.dims)
            for m in ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa", "vtr", "video_plays_3s"]:
                put(f"{tkey}[{label}].{m}", r.get(m), m, f"{t.title}: {label}")
            if totals.get("spend"):
                put(f"{tkey}[{label}].spend_share", safe_div(r.get("spend"), totals.get("spend"), 100.0), "share", f"{t.title}: {label}", "percent")
    if comparison:
        for m, dlt in comparison["deltas"].items():
            put(f"comparison.previous.{m}", comparison["previous"].get(m), m, "Comparison period")
            put(f"comparison.delta_pct.{m}", dlt["pct"], "delta_pct", "Change vs comparison period", "percent")
    return facts


def _unit(metric: str) -> str:
    if metric in ("ctr", "vtr", "share", "delta_pct"):
        return "percent"
    if metric in ("spend", "cpc", "cpm", "cpa", "purchase_value"):
        return "money"
    if metric == "roas":
        return "ratio"
    return "count"
