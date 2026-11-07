"""Upload guidelines: the file naming convention and a light check of raw report names.

The check never blocks an upload. It returns short notes that the New task page shows
under the file, so the employee can rename and re-upload before submitting.

Convention for raw reports:  Client_Platform_Market_Period[_Note].ext
    e.g. BWA_Meta_UAE_Oct2026.xlsx, BWA_TikTok_KSA_Oct2026_clicks.xlsx
"""
from __future__ import annotations

import os
import re

NAMING_PATTERN = "Client_Platform_Market_Period"
NAMING_EXAMPLES = ["BWA_Meta_UAE_Oct2026.xlsx", "BWA_TikTok_KSA_Oct2026_clicks.xlsx", "BWA_Snapchat_UAE_Sep-Oct2026.csv"]

# platform -> words that identify it in a file name (matched as whole tokens)
PLATFORM_TOKENS = {
    "meta": {"meta", "facebook", "fb", "instagram", "ig", "fbig", "fb+ig"},
    "tiktok": {"tiktok", "tt"},
    "snapchat": {"snapchat", "snap", "sc"},
    "google_ads": {"google", "gads", "googleads", "adwords", "youtube", "yt", "dv360"},
    "linkedin": {"linkedin", "li"},
}
PLATFORM_NAMES = {"meta": "Meta", "tiktok": "TikTok", "snapchat": "Snapchat", "google_ads": "Google", "linkedin": "LinkedIn"}

_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
           "september|october|november|december")
_PERIOD_RES = [
    re.compile(rf"(?<![a-z])({_MONTHS})(?![a-z])"),          # Oct, October, Sep-Oct
    re.compile(r"(?<!\d)20\d{2}[-_.]?(0[1-9]|1[0-2])(?!\d)"),  # 2026-10, 202610
    re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-_.](0?[1-9]|1[0-2])[-_.](20)?\d{2}(?!\d)"),  # 01-10-2026
    re.compile(r"(?<![a-z])(q[1-4]|h[12]|ytd|w(ee)?k\s?\d{1,2})(?![a-z])"),
]
# names that browsers / ad platforms give a download by default
_GENERIC_RE = re.compile(r"^(export|report|data|download|untitled|book\d*|sheet\d*|file|new|copy|campaigns?|ads?)"
                         r"([\s_\-]*(\(\d+\)|\d+|copy|final|new|v\d+))*$")
_DUPLICATE_RE = re.compile(r"(\(\d+\)|\bcopy\b|copy of)", re.I)


def _tokens(stem: str) -> set[str]:
    low = stem.lower()
    toks = set(re.split(r"[\s_\-.,()\[\]]+", low)) - {""}
    toks |= {"fb+ig"} if "fb+ig" in low else set()
    return toks


def name_platforms(name: str) -> set[str]:
    toks = _tokens(os.path.splitext(name)[0])
    joined = os.path.splitext(name)[0].lower()
    found = {p for p, words in PLATFORM_TOKENS.items() if toks & words}
    # also catch glued names such as "BWAtiktokOct" for the long, unambiguous words
    for p, words in PLATFORM_TOKENS.items():
        if any(len(w) >= 6 and w in joined for w in words):
            found.add(p)
    return found


def has_period(name: str) -> bool:
    stem = os.path.splitext(name)[0].lower()
    return any(r.search(stem) for r in _PERIOD_RES)


def check_name(name: str, role: str | None, sheets: list[dict] | None = None) -> list[str]:
    """Notes about a raw report's file name. Empty when the name follows the convention."""
    if role != "raw":
        return []
    stem = os.path.splitext(name)[0].strip()
    # stored names are sanitised, so a browser's "export (1)" arrives as "export _1_"
    stem = re.sub(r"\s*[(_](\d{1,2})[)_]$", r" (\1)", stem)
    example = NAMING_EXAMPLES[0]
    notes: list[str] = []
    if _GENERIC_RE.match(stem.lower()):
        notes.append(f"'{name}' looks like a default download name. Rename it as {NAMING_PATTERN}, e.g. {example}.")
        return notes
    if _DUPLICATE_RE.search(stem):
        notes.append("The name looks like a duplicate download (\"(1)\" or \"copy\"). Check it isn't the same export uploaded twice.")
    in_name = name_platforms(name)
    detected = {s.get("platform_key") for s in (sheets or []) if s.get("platform_key") and s.get("platform_key") != "unknown"}
    if not in_name:
        notes.append(f"The name doesn't say the platform. Add it (Meta, TikTok, Snapchat, Google, LinkedIn), e.g. {example}.")
    elif detected and not (in_name & detected):
        said = ", ".join(PLATFORM_NAMES.get(p, p) for p in sorted(in_name))
        got = ", ".join(PLATFORM_NAMES.get(p, p) for p in sorted(detected))
        notes.append(f"The name says {said} but the columns look like a {got} export. Check the right file was uploaded.")
    if not has_period(name):
        notes.append("The name doesn't say the period. Add the month, e.g. Oct2026 or Sep-Oct2026.")
    data_sheets = [s for s in (sheets or []) if s.get("data_sheet") is not False]
    if sheets and data_sheets and all((s.get("rows") or 0) == 0 for s in data_sheets):
        notes.append("No data rows were found. Re-export the report with rows (not only totals), and check the date range.")
    return notes
