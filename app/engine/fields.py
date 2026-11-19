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
    Field("link_clicks", "Link clicks", "additive", "count", "Clicks to the advertiser's destination (Meta 'Link clicks', TikTok 'Clicks (destination)', LinkedIn 'Clicks to Landing Page', X 'Link clicks')"),
    Field("clicks_all", "Clicks (all)", "additive", "count", "All clicks including profile, likes, expands etc."),
    Field("clicks_unspecified", "Clicks (unspecified)", "additive", "count", "Export column named only 'Clicks' - definition must be confirmed"),
    Field("swipe_ups", "Swipe ups", "additive", "count", "Snapchat swipe ups to attachment"),
    Field("landing_page_views", "Landing page views", "additive", "count"),
    Field("video_plays_3s", "3-second video plays", "additive", "count", "Meta: plays of at least 3 seconds"),
    Field("video_plays_2s", "2-second video plays", "additive", "count", "Meta: 2-second continuous plays; TikTok: 2-second video views; LinkedIn and X: video views (2 seconds, 50% in view)"),
    Field("video_views_6s", "6-second video views", "additive", "count", "TikTok: plays of at least 6 seconds (focused view)"),
    Field("video_views_tiktok", "Video views (TikTok)", "additive", "count", "TikTok 'Video views' (plays started)"),
    Field("video_views_snap", "Video views (Snapchat)", "additive", "count", "Snapchat: views of 2s+ or swipe up"),
    Field("video_views_youtube", "YouTube views", "additive", "count", "Google Ads 'TrueView views' / 'YouTube views': 30s watched (or the whole ad if shorter) or an interaction"),
    Field("views", "Views", "additive", "count", "Agency rule: Meta = 3-second video plays, Snapchat = 2-second video views, TikTok = 2-second video plays, LinkedIn and X = video views (2s, 50% in view), Google/YouTube = YouTube views"),
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
    (r"^(day|date|by day|stat time day|start time|time|start date \(in utc\)|start date)$", "date", None),
    (r"^(campaign name|campaign)$", "campaign_name", None),
    (r"^campaign id$", "campaign_id", None),
    (r"^(ad set name|ad set|adset name)$", "adset_name", "meta"),
    (r"^(ad group name|ad group)$", "adset_name", "tiktok"),
    (r"^(ad squad name|ad squad)$", "adset_name", "snapchat"),
    (r"^(ad set id|ad group id|ad squad id)$", "adset_id", None),
    (r"^(ad name|ad)$", "ad_name", None),
    (r"^ad id$", "ad_id", None),
    (r"^(creative|creative name|tweet text|post text)$", "creative", None),
    (r"^(account name|account)$", "account", None),
    (r"^objective$", "objective", None),
    (r"^brand$", "brand", None),
    (r"^(country|country/region|country / region|country region|location)$", "country", None),
    (r"^(market|region)$", "market", None),
    (r"^language$", "language", None),
    (r"^currency$", "currency", None),
    (r"^platform$", "platform", None),
    (r"^result indicator$", "result_indicator", "meta"),
    (r"^(amount spent|spend|cost|total cost|total spent|amount spent \(.*\))$", "spend", None),
    (r"^(impressions|paid impressions|impr\.?)$", "impressions", None),
    (r"^((paid |member )?reach|reach \(estimated\))$", "reach", None),
    (r"^(frequency|average frequency|frequency \(estimated\))$", "frequency", None),
    (r"^link clicks$", "link_clicks", "meta"),
    (r"^clicks \(destination\)$", "link_clicks", "tiktok"),
    (r"^(clicks to landing page|landing page clicks)$", "link_clicks", "linkedin"),
    (r"^url clicks$", "link_clicks", "x"),
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
    "tiktok": [(r"^video views$", "video_views_tiktok"),
               (r"^(2-second video views|2-second video plays|2 ?s(ec)? video (views|plays)|video views at 2s|video plays at 2s)$", "video_plays_2s"), (r"^clicks$", "clicks_unspecified")],
    "snapchat": [(r"^(video views|2 ?sec(ond)? video views|video views \(2s\)|2s video views|2-second video views)$", "video_views_snap"), (r"^impressions$", "impressions")],
    # LinkedIn: "Clicks" is every click (likes, profile, "see more"); link clicks are "Clicks to Landing Page".
    # A LinkedIn video view is 2+ continuous seconds with at least 50% of the player in view.
    "linkedin": [(r"^clicks$", "clicks_all"), (r"^video views$", "video_plays_2s")],
    # X: a video view is 2+ continuous seconds with at least 50% of the player in view.
    "x": [(r"^video views$", "video_plays_2s"), (r"^clicks$", "clicks_all")],
    # Google Ads / YouTube: views are "TrueView views" (older exports) or "YouTube views"; reach is "Unique users".
    "google_ads": [(r"^(trueview views|youtube views|views|video views)$", "video_views_youtube"),
                   (r"^unique users$", "reach")],
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
# Column signatures: header names that only one platform's export uses. Generic headers that several
# platforms share ("Cost", "Conversions", "Clicks", "Impressions", "Reach") are deliberately NOT here:
# they are what made a LinkedIn export look like TikTok and, later, a Google export look like TikTok.
# Each entry matches a whole normalised header; entries ending in "*" also match headers that start with it.
PLATFORM_SIGNATURES = {
    "meta": ["reporting starts", "reporting ends", "amount spent", "result indicator", "ad set name", "ad set id",
             "link clicks", "3-second video plays", "2-second continuous video plays", "thruplays", "attribution setting",
             "cost per results", "cost per result", "ad set budget", "ad set delivery", "landing page views",
             "unique link clicks", "cpc (cost per link click)", "ctr (link click-through rate)"],
    "tiktok": ["ad group name", "ad group id", "clicks (destination)", "ctr (destination)", "cpc (destination)",
               "video views at 6s", "video views at 2s", "2-second video views", "6-second video views", "stat time day",
               "by day", "cost per 1,000 people reached", "average play time per video view", "average play time per user",
               "profile visits", "paid followers", "focused views (6-second)"],
    "snapchat": ["ad squad name", "ad squad id", "swipe ups", "swipe up rate", "paid impressions", "story opens",
                 "paid reach", "attachment avg view time*", "screen time*", "ecpsu", "ecpm"],
    "google_ads": ["avg. cpc", "avg. cpm", "avg. cpv", "avg. cost", "conv.", "all conv.", "conv. rate", "conv. value",
                   "cost / conv.", "cost / all conv.", "view-through conv.", "interactions", "interaction rate",
                   "impr.", "impr. (top) %", "impr. (abs. top) %", "search impr. share", "currency code",
                   "bid strategy type", "campaign state", "ad group state", "keyword", "search term",
                   "youtube views", "trueview views"],
    "linkedin": ["campaign group name", "campaign group id", "total spent", "clicks to landing page", "landing page clicks",
                 "total social actions", "total engagements", "viral impressions", "reactions", "lead forms opened",
                 "one-click leads", "post-click conversions", "view-through conversions", "average cpm", "average cpc",
                 "start date (in utc)", "clicks to linkedin page", "member follows", "reach (estimated)",
                 "frequency (estimated)", "sponsored update*"],
    "x": ["billed engagements", "url clicks", "tweet id", "tweet text", "post id", "post text", "media views",
          "media engagements", "retweets", "reposts", "likes", "replies", "funding source*", "result type",
          "app clicks", "time period", "promoted tweet*"],
}
PLATFORM_LABELS = {"meta": "Meta (Facebook & Instagram)", "tiktok": "TikTok", "snapchat": "Snapchat",
                   "google_ads": "Google Ads", "linkedin": "LinkedIn", "x": "X (Twitter)", "unknown": "Unknown"}

# Words in a FILE NAME that name a platform, matched as whole tokens (so "x" in "ZSP_X_Oct" names X but the
# x in "Max" does not). Words of 6+ letters also match inside glued names such as "BWAtiktokOct".
PLATFORM_NAME_TOKENS = {
    "meta": {"meta", "facebook", "fb", "instagram", "ig", "fbig", "fb+ig"},
    "tiktok": {"tiktok", "tt"},
    "snapchat": {"snapchat", "snap", "sc"},
    "google_ads": {"google", "gads", "googleads", "adwords", "youtube", "yt", "dv360"},
    "linkedin": {"linkedin", "li"},
    "x": {"x", "twitter"},
}
# Phrases in report METADATA lines (the rows above the header) that name a platform.
PLATFORM_META_WORDS = {"meta": ["facebook", "instagram", "meta ads"], "tiktok": ["tiktok"], "snapchat": ["snapchat"],
                       "google_ads": ["google ads", "adwords"], "linkedin": ["linkedin"], "x": ["twitter", "x ads"]}


def name_platforms(name: str) -> set[str]:
    """Platforms a file name says, e.g. 'BWA_Meta_UAE_Oct2026.xlsx' -> {'meta'}."""
    stem = re.sub(r"\.(csv|xlsx|xlsm|xls|txt)$", "", (name or "").strip().lower())
    toks = set(re.split(r"[\s_\-.,()\[\]]+", stem)) - {""}
    if "fb+ig" in stem:
        toks.add("fb+ig")
    found = {p for p, words in PLATFORM_NAME_TOKENS.items() if toks & words}
    for p, words in PLATFORM_NAME_TOKENS.items():
        if any(len(w) >= 6 and w in stem for w in words):
            found.add(p)
    return found


def _sig_hits(sigs: list[str], hs: set[str]) -> list[str]:
    hit = []
    for s in sigs:
        if s.endswith("*"):
            if any(h.startswith(s[:-1]) for h in hs):
                hit.append(s[:-1])
        elif s in hs:
            hit.append(s)
    return hit


def detect_platform(norm_headers: list[str], filename: str = "", meta_text: str = "") -> tuple[str, float, list[str]]:
    """Decide which platform a sheet was exported from. Returns (platform, confidence, evidence).

    1. Score each platform by its distinctive column headers.
    2. If the file name (or report metadata) names exactly one platform, trust it - unless the columns
       clearly belong to another platform (2+ distinctive headers of that platform and none of the named one).
       Then the columns win and the upload check tells the employee the name and columns disagree.
    3. Without a usable name, the platform with the most distinctive headers wins; a tie or fewer than
       2 headers gives 'unknown', and the employee is asked.
    """
    hs = {h for h in norm_headers if h}
    hits = {p: _sig_hits(sigs, hs) for p, sigs in PLATFORM_SIGNATURES.items()}
    scores = {p: len(h) for p, h in hits.items()}
    named = name_platforms(filename)
    meta = (meta_text or "").lower()
    if not named:
        named = {p for p, words in PLATFORM_META_WORDS.items() if any(w in meta for w in words)}
    total = sum(scores.values())

    def ev(p, extra=None):
        out = [f"header '{s}'" for s in hits[p]]
        return out + ([extra] if extra else [])

    if len(named) == 1:
        p = next(iter(named))
        rival = max((q for q in scores if q != p), key=lambda q: scores[q])
        if scores[p] == 0 and scores[rival] >= 2:
            return rival, round(scores[rival] / (total or 1), 2), ev(rival)
        conf = 1.0 if scores[p] >= scores[rival] else 0.7
        return p, conf, ev(p, "file name/metadata names the platform")
    ranked = sorted(scores, key=lambda q: -scores[q])
    if len(named) > 1:  # e.g. "Meta_TikTok_combined": pick between the named platforms by columns
        cand = [q for q in ranked if q in named]
        if scores[cand[0]] > scores[cand[1]]:
            return cand[0], round(scores[cand[0]] / (total or 1), 2), ev(cand[0], "file name/metadata names the platform")
    best, second = ranked[0], ranked[1]
    if scores[best] < 2 or scores[best] == scores[second]:
        return "unknown", 0.0, []
    return best, round(scores[best] / total, 2), ev(best)


# ---------------------------------------------------------------------------
# Metric compatibility for cross-platform aggregation
# ---------------------------------------------------------------------------
# For the "selected click metric" the field used per platform. Swipe ups only join
# the destination-click group when that equivalence has been approved.
CLICK_METRIC_FIELDS = {
    "link_clicks": {"meta": "link_clicks", "tiktok": "link_clicks", "snapchat": None, "google_ads": None,
                    "linkedin": "link_clicks", "x": "link_clicks", "unknown": "link_clicks"},
    "clicks_all": {"meta": "clicks_all", "tiktok": "clicks_all", "snapchat": None, "google_ads": "clicks_unspecified",
                   "linkedin": "clicks_all", "x": "clicks_all", "unknown": "clicks_all"},
}

EQUIVALENCE_CANDIDATES = {
    ("snapchat", "swipe_ups", "link_clicks"): "Snapchat reports Swipe Ups rather than link clicks. Treat Swipe Ups as compatible with link clicks for cross-platform click totals, CTR and CPC?",
}
