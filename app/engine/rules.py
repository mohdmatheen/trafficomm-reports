"""Agency-wide reporting rules (Trafficomm standing instructions, Nov 2026).

These apply to every task and every client, on top of the client's own reporting rules. They are
code, not a per-client profile, so they also reach clients added later.
"""
import re

# Snapchat and TikTok raw exports are billed in USD; reports are in AED.
PLATFORM_SOURCE_CURRENCY = {"snapchat": "USD", "tiktok": "USD"}
DEFAULT_USD_AED = 3.6725  # official peg; employees can change it on each task

# "Views" means a specific play definition per platform.
VIEWS_FIELD = {"meta": "video_plays_3s", "snapchat": "video_views_snap", "tiktok": "video_plays_2s"}
VIEWS_LABEL = {"meta": "3-second video plays", "snapchat": "2-second video views", "tiktok": "2-second video plays"}

# Clicks are always the raw report's link-click metric (Snapchat reports it as Swipe Ups).
CLICK_METRIC = "link_clicks"
SNAPCHAT_SWIPE_UPS_ARE_LINK_CLICKS = True

# Reach above ad level is the sum of the rows' reach (as in the agency's own sheets). Platforms do not
# export deduplicated reach, so people reached by several campaigns are counted more than once.
REACH_SUMMED = True
REACH_NOTE = "Reach is summed across campaigns/ads (agency rule); people reached by more than one campaign are counted more than once."

# Slides that the client fills in themselves: never touched.
_CLIENT_PATTERNS = [
    r"update\s+to\s+kyra", r"kyra\s+to\s+update",
    r"to\s+be\s+shared\s+by\s+(the\s+)?client",
]
CLIENT_SLIDE_RE = re.compile("|".join(_CLIENT_PATTERNS), re.I)

# Screenshots placed into decks get a black border.
SCREENSHOT_BORDER_PT = 1.5
SCREENSHOT_BORDER_RGB = (0, 0, 0)


def is_client_slide_text(text: str) -> bool:
    t = re.sub(r"[\s ]+", " ", (text or "").replace("“", '"').replace("”", '"'))
    return bool(CLIENT_SLIDE_RE.search(t))


def usd_aed_rate(task: dict) -> float:
    try:
        r = float(task.get("fx_usd_aed") or 0)
    except (TypeError, ValueError):
        r = 0
    return r if r > 0 else DEFAULT_USD_AED


# Insights and commentary use professional, client-ready language. Informal or loaded words are
# replaced before anything reaches an Excel or PowerPoint output (applies to Claude and rule-based text).
_PRO_WORDS = [
    (r"dirt[- ]cheap", "highly cost-efficient"),
    (r"cheapest(?= (?:cpc|cpm|cpa|cpl|cost))", "lowest"), (r"cheaper(?= (?:cpc|cpm|cpa|cpl|cost))", "lower"),
    (r"cheapest", "most cost-efficient"), (r"cheaper", "more cost-efficient"), (r"cheaply", "cost-efficiently"),
    (r"cheap", "cost-efficient"),
    (r"most expensive|priciest", "highest-cost"), (r"more expensive|pricier", "higher-cost"),
    (r"expensive|pricey|costly", "higher-cost"),
    (r"worst", "weakest"), (r"terrible|awful|horrible|dismal|disastrous", "significantly weaker"),
    (r"poorly", "less effectively"), (r"poor|bad", "weaker"),
    (r"flopped", "underperformed"), (r"an? flop", "an underperforming item"), (r"flops?", "underperformed"),
    (r"an? waste of", "an inefficient use of"), (r"wasted", "less efficient"), (r"waste", "inefficiency"),
    (r"huge|massive|enormous", "substantial"),
    (r"amazing|awesome|fantastic|incredible|insane|crazy good|great", "strong"),
    (r"crazy", "notable"), (r"lots of|a lot of|tons of", "a high volume of"),
    (r"skyrocketed|exploded", "increased sharply"), (r"tanked|plummeted|crashed", "declined sharply"),
]
_PRO_RE = [(re.compile(rf"\b(?:{p})\b", re.I), r) for p, r in _PRO_WORDS]


def professionalize(text: str) -> str:
    """Replace informal wording with professional equivalents, keeping sentence-initial capitals."""
    out = text or ""
    for rx, rep in _PRO_RE:
        def _r(m, rep=rep):
            return rep[0].upper() + rep[1:] if m.group(0)[0].isupper() else rep
        out = rx.sub(_r, out)
    out = re.sub(r"!+", ".", out)
    return out


def unprofessional_terms(text: str) -> list[str]:
    return [m.group(0) for rx, _ in _PRO_RE for m in rx.finditer(text or "")]
