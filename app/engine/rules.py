"""Agency-wide reporting rules (Trafficomm standing instructions, Nov 2026).

These apply to every task and every client, on top of the client's own reporting rules. They are
code, not a per-client profile, so they also reach clients added later.
"""
import re

# Snapchat and TikTok raw exports are billed in USD; reports are in AED.
PLATFORM_SOURCE_CURRENCY = {"snapchat": "USD", "tiktok": "USD"}
DEFAULT_USD_AED = 3.6725  # official peg; employees can change it on each task

# "Views" means a specific play definition per platform.
VIEWS_FIELD = {"meta": "video_plays_3s", "snapchat": "video_views_snap"}
VIEWS_LABEL = {"meta": "3-second video plays", "snapchat": "2-second video views"}

# Clicks are always the raw report's link-click metric (Snapchat reports it as Swipe Ups).
CLICK_METRIC = "link_clicks"
SNAPCHAT_SWIPE_UPS_ARE_LINK_CLICKS = True

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
