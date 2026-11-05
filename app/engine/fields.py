"""Canonical reporting schema and the column-mapping dictionary.

Every raw column is mapped to a canonical field *with its original definition retained*.
Similar-looking metrics are deliberately kept apart (e.g. link clicks vs all clicks vs swipe ups,
3-second plays vs 6-second focused views). Cross-definition combination only happens when an
equivalence has been explicitly approved (task answer or reporting profile).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------
# kind: dimension | additive | nonadditive | rate | meta
# unit: money | count | percent | ratio | text | date


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str
    unit: str = "count"
    definition: str = ""


FIELDS: dict[str, Field] = {f.key: f for f in [
    # dimensions
    Field("date", "Date", "dimension", "date", "Reporting day for daily-granularity exports"),
    Field("report_start", "Reporting starts", "dimension", "date", "Start of the export's reporting window"),
    Field("report_end", "Reporting ends", "dimension", "date", "End of the export's reporting window"),
    Field("platform", "Platform", "dimension", "text"),
    Field("account", "Account", "dimension", "text"),
    Field("campaign_name", "Campaign", "dimension", "text"),
    Field("campaign_id", "Campaign ID", "dimension", "text"),
    Field("adset_name", "Ad set / ad group", "dimension", "text", "Meta ad set, TikTok ad group, Snapchat ad squad"),
    Field("adset_id", "Ad set ID", "dimension", "text"),
    Field("ad_name", "Ad", "dimension", "text"),
    Field("ad_id", "Ad ID", "dimension", "text"),
    Field("creative", "Creative", "dimension", "text"),
    Field("objective", "Objective", "dimension", "text"),
    Field("brand", "Brand / project", "dimension", "text"),
    Field("market", "Market", "dimension", "text"),
    Field("country", "Country", "dimension", "text"),
    Field("language", "Language", "dimension", "text"),
    Field("format", "Format", "dimension", "text"),
    Field("currency", "Currency", "dimension", "text"),
    Field("result_indicator", "Result indicator", "dimension", "text", "Meta: which action 'Results' counts"),
    # additive metrics
    Field("spend", "Spend", "additive", "money", "Media cost as reported by the platform"),
    Field("impressions", "Impressions", "additive", "count", "Times ads were on screen (paid impressions)"),
    Field("link_clicks", "Link clicks", "additive", "count", "Clicks to the advertiser's destination (Meta 'Link clicks', TikTok 'Clicks (destination)')"),
    Field("clicks_all", "Clicks (all)", "additive", "count", "All clicks including profile, likes, expands etc."),
    Field("clicks_unspecified", "Clicks (unspecified)", "additive", "count", "Export column named only 'Clicks' - definition must be confirmed"),
    Field("swipe_ups", "Swipe ups", "additive", "count", "Snapchat swipe ups to attachment"),
    Field("landing_page_views", "Landing page views", "additive", "count"),
    Field("video_plays_3s", "3-second video plays", "additive", "count", "Meta: plays of at least 3 seconds"),
    Field("video_plays_2s", "2-second continuous plays", "additive", "count", "Meta: 2-second continuous plays"),
    Field("video_views_6s", "6-second video views", "additive", "count", "TikTok: plays of at least 6 seconds (focused view)"),
    Field("video_views_tiktok", "Video views (TikTok)", "additive", "count", "TikTok 'Video views' (plays started)"),
    Field("video_views_snap", "Video views (Snapchat)", "additive", "count", "Snapchat: views of 2s+ or swipe up"),
    Field("views", "Views", "additive", "count", "Agency rule: Meta = 3-second video plays, Snapchat = 2-second video views"),
    Field("thruplays", "ThruPlays", "additive", "count", "Meta: plays to completion or 15s"),
    Field("video_completions", "Video completions", "additive", "count", "Plays to 100%"),
    Field("results", "Results (objective-dependent)", "additive", "count", "Meta 'Results' - meaning depends on Result indicator; never summed across indicators"),
    Field("leads_meta_form", "Meta lead-form leads", "additive", "count", "Meta Results where indicator = leadgen.other (instant-form leads, platform-attributed)"),
    Field("website_leads", "Website leads", "additive", "count", "Pixel-attributed website leads (platform-attributed)"),
    Field("leads", "Leads (platform)", "additive", "count", "Platform-reported leads (platform-attributed)"),
    Field("conversions", "Conversions (platform)", "additive", "count", "Platform-specific conversion count - definition differs by platform/pixel"),
    Field("purchases", "Purchases", "additive", "count", "Platform-attributed purchases"),
    Field("purchase_value", "Purchase value", "additive", "money", "Platform-attributed revenue"),
    Field("installs", "App installs", "additive", "count"),
    # non-additive
    Field("reach", "Reach", "nonadditive", "count", "Unique accounts reached - NOT additive across ads, periods or platforms"),
    Field("unique_link_clicks", "Unique link clicks", "nonadditive", "count", "Unique people who clicked - NOT additive"),
    Field("frequency", "Frequency (reported)", "rate", "ratio", "Impressions / reach as reported"),
    # reported rates (kept for reference, never aggregated)
    Field("reported_ctr", "CTR (reported)", "rate", "percent"),
    Field("reported_cpc", "CPC (reported)", "rate", "money"),
    Field("reported_cpm", "CPM (reported)", "rate", "money"),
    Field("reported_cpr", "Cost per result (reported)", "rate", "money"),
]}

ADDITIVE = [k for k, f in FIELDS.items() if f.kind == "additive"]
NONADDITIVE = [k for k, f in FIELDS.items() if f.kind == "nonadditive"]
DIMENSIONS = [k for k, f in FIELDS.items() if f.kind == "dimension"]
MONEY = [k for k, f in FIELDS.items() if f.unit == "money" and f.kind == "additive"]

# Fields that are dropped deliberately: delivery/budget metadata that is not reporting data.
IGNORED_PATTERNS = [
    r"^ad set budget", r"^ad set budget type", r"^budget", r"^ends$", r"^attribution setting",
    r"^bid( type)?$", r"^last significant edit", r"ranking$", r"^ad delivery", r"^delivery",
    r"^results \(initial\)", r"^starts$", r"^status$", r"^primary status", r"^secondary status",
]

# ---------------------------------------------------------------------------
# Header synonyms: (regex on normalised header, canonical key, platform hint or None)
# Order matters: first match wins.
# ---------------------------------------------------------------------------
SYNONYMS: list[tuple[str, str, str | None]] = [
    (r"^reporting starts?$", "report_start", "meta"),
    (r"^reporting ends?$", "report_end", "meta"),
    (r"^(day|date|by day|stat time day|start time|time)$", "date", None),
    (r"^(campaign name|campaign)$", "campaign_name", None),
    (r"^campaign id$", "campaign_id", None),
    (r"^(ad set name|ad set|adset name)$", "adset_name", "meta"),
    (r"^(ad group name|ad group)$", "adset_name", "tiktok"),
    (r"^(ad squad name|ad squad)$", "adset_name", "snapchat"),
    (r"^(ad set id|ad group id|ad squad id)$", "adset_id", None),
    (r"^(ad name|ad)$", "ad_name", None),
    (r"^ad id$", "ad_id", None),
    (r"^(creative|creative name)$", "creative", None),
    (r"^(account name|account)$", "account", None),
    (r"^objective$", "objective", None),
    (r"^brand$", "brand", None),
    (r"^(country|country/region|country / region|country region|location)$", "country", None),
    (r"^(market|region)$", "market", None),
    (r"^language$", "language", None),
    (r"^currency$", "currency", None),
    (r"^platform$", "platform", None),
    (r"^result indicator$", "result_indicator", "meta"),
    (r"^(amount spent|spend|cost|total cost|amount spent \(.*\))$", "spend", None),
    (r"^(impressions|paid impressions|impr\.?)$", "impressions", None),
    (r"^reach$", "reach", None),
    (r"^frequency$", "frequency", None),
    (r"^link clicks$", "link_clicks", "meta"),
    (r"^clicks \(destination\)$", "link_clicks", "tiktok"),
    (r"^(clicks \(all\)|all clicks)$", "clicks_all", None),
    (r"^clicks$", "clicks_unspecified", None),
    (r"^unique link clicks$", "unique_link_clicks", "meta"),
    (r"^(swipe ups|swipe-ups|swipes)$", "swipe_ups", "snapchat"),
    (r"^landing page views$", "landing_page_views", "meta"),
    (r"^3-second video plays$", "video_plays_3s", "meta"),
    (r"^2-second continuous video plays$", "video_plays_2s", "meta"),
    (r"^(video views at 6s|6-second video views|focused views \(6-second\))$", "video_views_6s", "tiktok"),
    (r"^thruplays?$", "thruplays", "meta"),
    (r"^(video completions|video views at 100%|video completes)$", "video_completions", None),
    (r"^results$", "results", "meta"),
    (r"^website leads$", "website_leads", "meta"),
    (r"^(leads|lead form submissions|total leads)$", "leads", None),
    (r"^(conversions|total conversions|conversion)$", "conversions", None),
    (r"^(purchases|total purchases|purchase)$", "purchases", None),
    (r"^(purchases conversion value|total purchase value|purchase value|purchase revenue|revenue)$", "purchase_value", None),
    (r"^(app installs|installs)$", "installs", None),
    (r"^(ctr|ctr \(all\)|ctr \(link click-through rate\)|ctr \(destination\))$", "reported_ctr", None),
    (r"^(cpc|cpc \(all\)|cpc \(cost per link click\)|cpc \(destination\)|avg\.? cpc)$", "reported_cpc", None),
    (r"^(cpm|cpm \(cost per 1,000 impressions\)|ecpm)$", "reported_cpm", None),
    (r"^(cost per results?|cost per result|cpa|cost per conversion)$", "reported_cpr", None),
]

# platform-specific overrides where the same header means something different
PLATFORM_SYNONYMS: dict[str, list[tuple[str, str]]] = {
    "tiktok": [(r"^video views$", "video_views_tiktok"), (r"^clicks$", "clicks_unspecified")],
    "snapchat": [(r"^(video views|2 ?sec(ond)? video views|video views \(2s\)|2s video views|2-second video views)$", "video_views_snap"), (r"^impressions$", "impressions")],
}

_CURRENCY_RE = re.compile(r"\((?P<cur>[A-Z]{3})\)")


def normalise_header(h: object) -> tuple[str, str | None]:
    """Return (normalised header, currency code found in header or None)."""
    s = "" if h is None else str(h)
    s = s.replace("​", "").replace("\xa0", " ").strip()
    cur = None
    m = _CURRENCY_RE.search(s)
    if m:
        cur = m.group("cur")
        s = _CURRENCY_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s, cur


def match_header(raw_header: object, platform: str | None = None,
                 profile_overrides: dict[str, str] | None = None) -> dict:
    """Map one raw header to a canonical field.

    Returns dict(original, normalised, canonical|None, status, definition, currency, source)
    status: mapped | ignored | unresolved | profile
    """
    norm, cur = normalise_header(raw_header)
    out = {"original": "" if raw_header is None else str(raw_header), "normalised": norm,
           "canonical": None, "status": "unresolved", "definition": "", "currency": cur, "source": "dictionary"}
    if not norm:
        out["status"] = "ignored"
        out["definition"] = "Empty header"
        return out
    if profile_overrides:
        for pat, key in profile_overrides.items():
            if norm == pat.lower() or re.fullmatch(pat.lower(), norm):
                out.update(canonical=key, status="mapped", source="reporting profile",
                           definition=FIELDS[key].definition if key in FIELDS else "")
                return out
    for pat in IGNORED_PATTERNS:
        if re.search(pat, norm):
            out.update(status="ignored", definition="Delivery/budget metadata - not reporting data")
            return out
    if platform and platform in PLATFORM_SYNONYMS:
        for pat, key in PLATFORM_SYNONYMS[platform]:
            if re.fullmatch(pat, norm):
                out.update(canonical=key, status="mapped", definition=FIELDS[key].definition)
                return out
    for pat, key, _hint in SYNONYMS:
        if re.fullmatch(pat, norm):
            out.update(canonical=key, status="mapped", definition=FIELDS[key].definition)
            return out
    return out


# ---------------------------------------------------------------------------
# Platform detection signatures
# ---------------------------------------------------------------------------
PLATFORM_SIGNATURES = {
    "meta": ["reporting starts", "amount spent", "result indicator", "ad set name", "link clicks",
             "3-second video plays", "thruplays", "attribution setting", "cost per results"],
    "tiktok": ["ad group name", "clicks (destination)", "video views at 6s", "by day", "stat time day",
               "cost", "conversions"],
    "snapchat": ["ad squad name", "swipe ups", "paid impressions", "story opens"],
    "google_ads": ["avg. cpc", "conv.", "interactions", "impr."],
}
PLATFORM_LABELS = {"meta": "Meta (Facebook & Instagram)", "tiktok": "TikTok", "snapchat": "Snapchat",
                   "google_ads": "Google Ads", "unknown": "Unknown"}


def detect_platform(norm_headers: list[str], filename: str = "", meta_text: str = "") -> tuple[str, float, list[str]]:
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    hs = set(norm_headers)
    for p, sigs in PLATFORM_SIGNATURES.items():
        hit = [s for s in sigs if s in hs or any(h.startswith(s) for h in hs)]
        scores[p] = len(hit)
        evidence[p] = [f"header '{s}'" for s in hit]
    fn = (filename + " " + meta_text).lower()
    for p, words in {"meta": ["meta", "facebook", "fb+ig", "instagram"], "tiktok": ["tiktok"],
                     "snapchat": ["snapchat", "snap"], "google_ads": ["google ads", "adwords"]}.items():
        if any(w in fn for w in words):
            scores[p] = scores.get(p, 0) + 2
            evidence.setdefault(p, []).append("file name/metadata mentions platform")
    best = max(scores, key=lambda k: scores[k]) if scores else "unknown"
    total = sum(scores.values()) or 1
    if scores.get(best, 0) < 2:
        return "unknown", 0.0, []
    return best, round(scores[best] / total, 2), evidence[best]


# ---------------------------------------------------------------------------
# Metric compatibility for cross-platform aggregation
# ---------------------------------------------------------------------------
# For the "selected click metric" the field used per platform. Swipe ups only join
# the destination-click group when that equivalence has been approved.
CLICK_METRIC_FIELDS = {
    "link_clicks": {"meta": "link_clicks", "tiktok": "link_clicks", "snapchat": None, "google_ads": None, "unknown": "link_clicks"},
    "clicks_all": {"meta": "clicks_all", "tiktok": "clicks_all", "snapchat": None, "google_ads": "clicks_unspecified", "unknown": "clicks_all"},
}

EQUIVALENCE_CANDIDATES = {
    ("snapchat", "swipe_ups", "link_clicks"): "Snapchat reports Swipe Ups rather than link clicks. Treat Swipe Ups as compatible with link clicks for cross-platform click totals, CTR and CPC?",
}
