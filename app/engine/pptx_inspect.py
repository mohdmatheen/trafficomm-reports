"""Inspect an existing client deck: slide inventory, tables/charts mapped to metrics, client-owned
content, dates, speaker notes and unsupported objects."""
from __future__ import annotations

import re
from collections import Counter

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Emu

# Deck column label -> metric key (lower-cased, normalised)
DECK_LABELS = {
    "range": "range", "date range": "range", "period": "range", "dates": "range",
    "audience size": "audience_size",
    "reach": "reach", "impressions": "impressions", "impr": "impressions",
    "frequency": "frequency_calc", "freq": "frequency_calc",
    "views": "video_plays_3s", "video views": "video_plays_3s", "3s views": "video_plays_3s", "3-second video plays": "video_plays_3s",
    "vtr": "vtr", "view rate": "vtr",
    "clicks": "clicks", "link clicks": "link_clicks", "all clicks": "clicks_all",
    "ctr": "ctr", "cpc": "cpc", "cpm": "cpm",
    "spend": "spend", "amount spent": "spend", "cost": "spend", "media spend": "spend", "budget spent": "spend",
    "leads": "conversions", "conversions": "conversions", "results": "conversions",
    "cpa": "cpa", "cpl": "cpa", "cost per lead": "cpa", "cost per result": "cpa",
    "roas": "roas", "swipe ups": "swipe_ups", "thruplays": "thruplays",
}
MONTH_YEAR_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(20\d{2})\b", re.I)
DATE_RANGE_RE = re.compile(r"\b\d{1,2}(st|nd|rd|th)?\s*[–-]\s*\d{1,2}(st|nd|rd|th)?\s+\w+\s+20\d{2}|\b\w+ \d{1,2}, 20\d{2}\s*[–-]\s*\w+ \d{1,2}, 20\d{2}", re.I)
DEFAULT_CLIENT_MARKERS = ["to be shared by the client", "to update", "client to provide", "tbc by client"]


def clean(s: str) -> str:
    return (s or "").replace("​", "").replace("\xa0", " ").strip()


def label_to_metric(label: str, overrides: dict | None = None) -> str | None:
    l = re.sub(r"\s+", " ", clean(label).lower()).strip(" :")
    if overrides:
        for k, v in overrides.items():
            if k.lower() == l:
                return v
    return DECK_LABELS.get(l)


def _shape_kind(sh) -> str:
    try:
        st = sh.shape_type
    except NotImplementedError:
        st = None
    if getattr(sh, "has_table", False) and sh.has_table:
        return "table"
    if getattr(sh, "has_chart", False) and sh.has_chart:
        return "chart"
    if st == MSO_SHAPE_TYPE.PICTURE:
        return "picture"
    if st == MSO_SHAPE_TYPE.GROUP:
        return "group"
    xml = sh._element.xml if hasattr(sh, "_element") else ""
    if "graphicFrame" in sh._element.tag:
        if "diagram" in xml:
            return "smartart"
        if "oleObj" in xml:
            return "ole"
        return "graphic_other"
    if "p:pic" in xml and ("videoFile" in xml or "audioFile" in xml or "media" in xml.lower()):
        return "media"
    if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
        return "text"
    return "other"


def slide_title(slide) -> str:
    try:
        if slide.shapes.title is not None and clean(slide.shapes.title.text_frame.text):
            return clean(slide.shapes.title.text_frame.text)
    except Exception:  # noqa: BLE001
        pass
    cands = []
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            t = clean(sh.text_frame.text)
            if t and len(t) < 90 and not any(m in t.lower() for m in DEFAULT_CLIENT_MARKERS):
                cands.append((sh.top or 0, -(sh.width or 0), t))
    if not cands:
        return ""
    cands.sort()
    return cands[0][2].split("\n")[0]


def inspect_deck(path: str, profile: dict | None = None) -> dict:
    profile = profile or {}
    deck_cfg = profile.get("deck") or {}
    markers = [m.lower() for m in deck_cfg.get("client_content_markers", [])] + DEFAULT_CLIENT_MARKERS
    overrides = deck_cfg.get("metric_labels") or {}
    prs = Presentation(path)
    fonts = Counter()
    slides = []
    for idx, slide in enumerate(prs.slides, 1):
        info = {"index": idx, "title": slide_title(slide), "layout": slide.slide_layout.name, "tables": [], "charts": [],
                "pictures": 0, "texts": [], "unsupported": [], "client_markers": [], "month_labels": [], "date_ranges": [],
                "notes": "", "has_numbers_in_notes": False}

        def walk(shapes, depth=0):
            for sh in shapes:
                kind = _shape_kind(sh)
                if kind == "table":
                    tbl = sh.table
                    cells = [[clean(c.text) for c in r.cells] for r in tbl.rows]
                    header = cells[0] if cells else []
                    mc = {i: label_to_metric(h, overrides) for i, h in enumerate(header)}
                    mc = {i: m for i, m in mc.items() if m}
                    info["tables"].append({"shape_id": sh.shape_id, "name": sh.name, "rows": len(cells), "cols": len(header),
                                           "header": header, "metric_cols": mc, "cells": cells[:25]})
                elif kind == "chart":
                    ch = sh.chart
                    try:
                        plot = ch.plots[0]
                        cats = [clean(str(c)) for c in plot.categories]
                        series = [{"name": clean(s.name), "metric": label_to_metric(s.name, overrides), "values": list(s.values)} for s in plot.series]
                    except Exception:  # noqa: BLE001
                        cats, series = [], []
                    info["charts"].append({"shape_id": sh.shape_id, "name": sh.name, "type": str(ch.chart_type), "categories": cats, "series": series})
                elif kind == "picture":
                    info["pictures"] += 1
                elif kind == "group":
                    if depth < 3:
                        walk(sh.shapes, depth + 1)
                    if any(re.search(r"\d", clean(s.text_frame.text)) for s in sh.shapes if getattr(s, "has_text_frame", False) and s.has_text_frame):
                        info["unsupported"].append({"shape": sh.name, "kind": "group", "detail": "Grouped shapes containing figures - not updated automatically"})
                elif kind in ("smartart", "ole", "media", "graphic_other"):
                    info["unsupported"].append({"shape": sh.name, "kind": kind,
                                                "detail": {"smartart": "SmartArt diagram", "ole": "Embedded OLE object (e.g. pasted Excel)", "media": "Audio/video", "graphic_other": "Unknown graphic frame"}[kind] + " - not updated automatically"})
                if getattr(sh, "has_text_frame", False) and sh.has_text_frame and kind not in ("table", "chart"):
                    t = clean(sh.text_frame.text)
                    if t:
                        info["texts"].append({"shape_id": sh.shape_id, "name": sh.name, "text": t[:500]})
                        low = t.lower()
                        for m in markers:
                            if m in low:
                                info["client_markers"].append(t[:80])
                                break
                        info["month_labels"] += [m.group(0) for m in MONTH_YEAR_RE.finditer(t)]
                        info["date_ranges"] += [m.group(0) for m in DATE_RANGE_RE.finditer(t)]
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            if r.font.name:
                                fonts[r.font.name] += 1
        walk(slide.shapes)
        if slide.has_notes_slide:
            n = clean(slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else "")
            info["notes"] = n[:500]
            info["has_numbers_in_notes"] = bool(re.search(r"\d", n))
        # classification
        metric_tables = [t for t in info["tables"] if len([m for m in t["metric_cols"].values() if m not in ("range", "audience_size")]) >= 3]
        if info["client_markers"]:
            cls = "client_content"
        elif metric_tables:
            cls = "metric_slide"
        elif any(any(s["metric"] for s in c["series"]) for c in info["charts"]):
            cls = "chart_slide"
        elif idx == 1 or (info["month_labels"] and len(info["texts"]) <= 3 and not info["tables"]):
            cls = "cover"
        elif info["tables"]:
            cls = "client_content" if not metric_tables else "metric_slide"
        elif info["pictures"] and not info["texts"]:
            cls = "image_only"
        else:
            cls = "other"
        info["classification"] = cls
        info["metric_tables"] = len(metric_tables)
        slides.append(info)
    return {"slide_width": prs.slide_width, "slide_height": prs.slide_height,
            "aspect": f"{Emu(prs.slide_width).inches:.2f}x{Emu(prs.slide_height).inches:.2f} in",
            "slide_count": len(slides), "fonts": dict(fonts.most_common(10)),
            "layouts": sorted({s["layout"] for s in slides}), "slides": slides,
            "summary": dict(Counter(s["classification"] for s in slides)),
            "unsupported_total": sum(len(s["unsupported"]) for s in slides)}
