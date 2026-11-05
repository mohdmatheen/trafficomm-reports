"""Presentation formatting (display rounding only - stored values keep full precision)."""
from __future__ import annotations

import datetime as dt
import math
import re

NA = "N/A"


def is_na(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def fmt_int(x, na=NA) -> str:
    return na if is_na(x) else f"{x:,.0f}"


def fmt_num(x, dp=2, na=NA) -> str:
    return na if is_na(x) else f"{x:,.{dp}f}"


def fmt_pct(x, dp=2, na=NA) -> str:
    return na if is_na(x) else f"{x:,.{dp}f}%"


def fmt_money(x, cur="", dp=2, na=NA) -> str:
    if is_na(x):
        return na
    return f"{cur} {x:,.{dp}f}".strip()


def fmt_metric(metric: str, x, cur: str = "", na=NA) -> str:
    if metric in ("spend", "purchase_value"):
        return fmt_money(x, cur, 2, na)
    if metric in ("cpc", "cpm", "cpa"):
        return fmt_money(x, cur, 2, na)
    if metric in ("ctr", "vtr", "share", "delta_pct"):
        return fmt_pct(x, 2, na)
    if metric in ("roas", "frequency_calc", "frequency"):
        return fmt_num(x, 2, na)
    return fmt_int(x, na)


def ordinal(n: int) -> str:
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def range_label(a: dt.date | None, b: dt.date | None, style: str = "plain") -> str:
    if not a or not b:
        return NA
    if style == "ordinal":
        if a.year == b.year and a.month == b.month:
            return f"{ordinal(a.day)} – {ordinal(b.day)} {b.strftime('%b %Y')}"
        if a.year == b.year:
            return f"{ordinal(a.day)} {a.strftime('%b')} – {ordinal(b.day)} {b.strftime('%b %Y')}"
        return f"{ordinal(a.day)} {a.strftime('%b %Y')} – {ordinal(b.day)} {b.strftime('%b %Y')}"
    if a.year == b.year and a.month == b.month:
        return f"{a.day}–{b.day} {b.strftime('%b %Y')}"
    return f"{a.strftime('%d %b %Y')} – {b.strftime('%d %b %Y')}"


def month_label(a: dt.date | None, b: dt.date | None, long=False) -> str:
    if not a or not b:
        return ""
    f = "%B %Y" if long else "%b %Y"
    if (a.year, a.month) == (b.year, b.month):
        return a.strftime(f)
    return f"{a.strftime('%b')} – {b.strftime(f)}" if a.year == b.year else f"{a.strftime(f)} – {b.strftime(f)}"


class CellStyle:
    """Learns how an existing deck cell formats a number so replacements look the same."""

    def __init__(self, sample: str):
        s = (sample or "").replace("\xa0", " ").strip()
        self.sample = s
        self.prefix = ""
        self.suffix = ""
        m = re.match(r"^([A-Za-z$€£]{1,4}\s?)?(-?[\d,]*\.?\d+)\s*(%|x)?$", s)
        self.numeric = bool(m)
        if m:
            self.prefix = m.group(1) or ""
            self.suffix = m.group(3) or ""
            num = m.group(2)
            self.thousands = "," in num or len(num.split(".")[0]) < 4
            self.dp = len(num.split(".")[1]) if "." in num else 0
        else:
            self.thousands = True
            self.dp = None

    def format(self, x, default_dp=0, na="-") -> str:
        if is_na(x):
            return na
        dp = self.dp if self.dp is not None else default_dp
        body = f"{x:,.{dp}f}" if self.thousands else f"{x:.{dp}f}"
        return f"{self.prefix}{body}{self.suffix}"
