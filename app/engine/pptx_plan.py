"""Booked vs delivered in the client deck: fills tables that compare the media plan with delivery
(columns such as 'Booked Impressions', 'Delivered', '% Delivered', 'Pacing'), and, when the deck has
no such table, adds one slide with the comparison. Client-owned slides are never touched."""
from __future__ import annotations

import copy

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from . import fields as F
from . import fmt as FM
from . import media_plan as MP
from . import rules as R
from .pptx_inspect import clean, label_to_metric

DELIVERED_OF = {"spend": "budget", "impressions": "impressions", "reach": "reach", "views": "views", "clicks": "clicks", "link_clicks": "clicks"}


def _slide_text(slide) -> str:
    out = []
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            out.append(sh.text_frame.text)
        if getattr(sh, "has_table", False) and sh.has_table:
            out += [c.text for r in sh.table.rows for c in r.cells]
    return " ".join(out)


def _set_text(cell, text: str):
    tf = cell.text_frame
    p = tf.paragraphs[0]
    runs = p.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        p.text = text
    for extra in tf.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)


def _row_target(label: str, cmp: dict):
    l = clean(label)
    if not l:
        return None
    if MP.TOTAL_RE.match(l) or l.lower() in ("overall", "all platforms", "grand total"):
        return cmp.get("total")
    plat = MP.platform_of(l)
    rows = cmp.get("rows") or []
    if cmp.get("by_campaign"):
        camp = MP.match_campaign(l, sorted({r["campaign"] for r in rows if r.get("campaign")}))
        hit = [r for r in rows if r.get("campaign") == camp and (plat is None or r["platform"] == plat)] if camp else []
        if len(hit) == 1:
            return hit[0]
    hit = [r for r in rows if r["platform"] == plat] if plat else []
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:  # several plan campaigns on one platform row: add them up
        agg = {"label": hit[0]["label"]}
        for m in cmp["metrics"]:
            parts = [h[m] for h in hit]
            def s(k):
                v = [p[k] for p in parts if p[k] is not None]
                return sum(v) if v else None
            agg[m] = {"booked": s("booked"), "expected": s("expected"), "delivered": s("delivered")}
            from .calc import safe_div
            agg[m]["delivery_pct"] = safe_div(agg[m]["delivered"], agg[m]["booked"], 100.0)
            agg[m]["pacing_pct"] = safe_div(agg[m]["delivered"], agg[m]["expected"], 100.0)
        agg["flight_left"] = any(h.get("flight_left") for h in hit)
        agg["status"] = MP._status(agg)
        return agg
    return None


def update_plan_tables(prs, cmp: dict, na: str = "-") -> tuple[list, list, set]:
    changes, flags, slides = [], [], set()
    for idx, slide in enumerate(prs.slides, 1):
        if R.is_client_slide_text(_slide_text(slide)):
            continue
        for sh in slide.shapes:
            if not (getattr(sh, "has_table", False) and sh.has_table):
                continue
            tbl = sh.table
            header = [clean(c.text) for c in tbl.rows[0].cells]
            if not MP.is_plan_header(header):
                continue
            slides.add(idx)
            kinds = {}
            booked_metrics = []
            for ci, h in enumerate(header):
                k = MP.deck_plan_column(h)
                if k:
                    kinds[ci] = k
                    if k[0] == "booked" and k[1]:
                        booked_metrics.append(k[1])
                else:
                    m = label_to_metric(h)
                    if m in DELIVERED_OF:
                        kinds[ci] = ("delivered", DELIVERED_OF[m])
            primary = next((m for m in ("impressions", "views", "clicks", "budget") if m in booked_metrics), booked_metrics[0] if booked_metrics else "impressions")
            label_col = next((i for i in range(len(header)) if i not in kinds), 0)
            unmatched = []
            for ri in range(1, len(tbl.rows)):
                lab = clean(tbl.cell(ri, label_col).text)
                target = _row_target(lab, cmp)
                if target is None:
                    if lab:
                        unmatched.append(lab)
                    continue
                for ci, (kind, metric) in kinds.items():
                    cell = tbl.cell(ri, ci)
                    old = clean(cell.text)
                    m = metric or primary
                    x = target.get(m) or {}
                    if kind == "status":
                        new = target.get("status") or na
                    else:
                        val = {"booked": x.get("booked"), "delivered": x.get("delivered"), "pct": x.get("delivery_pct"), "pacing": x.get("pacing_pct")}[kind]
                        st = FM.CellStyle(old)
                        if kind in ("pct", "pacing"):
                            if st.dp is None or not st.suffix:
                                st.dp = 1
                            st.suffix = "%"
                        elif st.dp is None:
                            st.dp = 0
                        new = st.format(val, 0, na)
                    rec = {"slide": idx, "shape": sh.name, "row": ri, "col": header[ci], "metric": f"plan_{kind}_{m}", "old": old, "new": new}
                    if new != old:
                        _set_text(cell, new)
                    else:
                        rec["unchanged"] = True
                    changes.append(rec)
            if unmatched:
                flags.append({"slide": idx, "severity": "warning", "message": f"Slide {idx}: booked vs delivered rows not in the media plan comparison - left unchanged: " + ", ".join(unmatched[:6])})
    return changes, flags, slides


def _closing(slide) -> bool:
    t = _slide_text(slide).lower()
    return any(w in t for w in ("thank you", "thanks", "questions?", "the end")) and len(t) < 200


def add_plan_slide(prs, cmp: dict, currency: str | None, period_label: str, na: str = "-") -> int | None:
    """One slide with the comparison, using the deck's 'Title Only' layout so it carries the deck's
    master design. Inserted before a closing 'Thank you' slide when there is one."""
    rows = cmp.get("rows") or []
    if not rows:
        return None
    layout = next((l for l in prs.slide_layouts if l.name.lower().strip() in ("title only", "title")), None) or prs.slide_layouts[min(5, len(prs.slide_layouts) - 1)]
    slide = prs.slides.add_slide(layout)
    title = f"Delivered vs booked – {period_label}"
    if slide.shapes.title is not None:
        slide.shapes.title.text = title
    for ph in list(slide.placeholders):
        if slide.shapes.title is not None and ph.placeholder_format.idx == slide.shapes.title.placeholder_format.idx:
            continue
        ph._element.getparent().remove(ph._element)  # empty body/footer placeholders would show prompts
    if slide.shapes.title is None:
        tb = slide.shapes.add_textbox(Emu(int(prs.slide_width * 0.05)), Emu(int(prs.slide_height * 0.05)), Emu(int(prs.slide_width * 0.9)), Emu(int(prs.slide_height * 0.1)))
        tb.text_frame.text = title
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(24)
        tb.text_frame.paragraphs[0].runs[0].font.bold = True
    cur = currency or ""
    metrics = [m for m in ("budget", "impressions", "views", "clicks") if m in cmp["metrics"]][:3]
    hdr = ["Platform"]
    for m in metrics:
        if m == "budget":
            hdr += [f"Booked budget ({cur})" if cur else "Booked budget", f"Spend ({cur})" if cur else "Spend", "% of budget"]
        else:
            hdr += [f"Booked {m}", f"Delivered {m}", "% delivered"]
    hdr.append("Pacing")
    body = rows + [cmp["total"]] if len(rows) > 1 else rows
    left, top = int(prs.slide_width * 0.04), int(prs.slide_height * 0.22)
    ttl = slide.shapes.title
    if ttl is not None and ttl.top is not None and ttl.height is not None:
        top = max(top, int(ttl.top + ttl.height + prs.slide_height * 0.03))
    width = int(prs.slide_width * 0.92)
    height = int(prs.slide_height * 0.07) * (len(body) + 1)
    gf = slide.shapes.add_table(len(body) + 1, len(hdr), Emu(left), Emu(top), Emu(width), Emu(height))
    gf.name = "Booked vs delivered"
    t = gf.table
    for ci, h in enumerate(hdr):
        t.cell(0, ci).text = h
    for ri, r in enumerate(body, 1):
        lab = MP.display(r)
        vals = [lab if r is not cmp["total"] else "Total"]
        for m in metrics:
            x = r.get(m) or {}
            if m == "budget":
                vals += [FM.fmt_num(x.get("booked"), 0, na), FM.fmt_num(x.get("delivered"), 0, na), FM.fmt_pct(x.get("delivery_pct"), 1, na)]
            else:
                vals += [FM.fmt_int(x.get("booked"), na), FM.fmt_int(x.get("delivered"), na), FM.fmt_pct(x.get("delivery_pct"), 1, na)]
        vals.append(r.get("status") or na)
        for ci, v in enumerate(vals):
            t.cell(ri, ci).text = v
    from pptx.enum.text import PP_ALIGN
    for ri, row in enumerate(t.rows):
        for ci, c in enumerate(row.cells):
            for p in c.text_frame.paragraphs:
                if ci > 0 and ci < len(hdr) - 1:
                    p.alignment = PP_ALIGN.RIGHT
                for run in p.runs:
                    run.font.size = Pt(10 if len(hdr) > 8 else 12)
    notes = [n for n in cmp.get("notes", []) if "prorated" in n or "no flight dates" in n][:1]
    tb = slide.shapes.add_textbox(Emu(left), Emu(top + height + int(prs.slide_height * 0.04)), Emu(width), Emu(int(prs.slide_height * 0.1)))
    tb.text_frame.word_wrap = True
    tb.text_frame.text = ("Booked figures from the client media plan for this reporting period (net media). " + " ".join(notes)).strip()
    for run in tb.text_frame.paragraphs[0].runs:
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    # move before a closing slide
    lst = prs.slides._sldIdLst
    ids = list(lst)
    n = len(ids)
    if n >= 3 and _closing(prs.slides[n - 2]):
        el = ids[-1]
        lst.remove(el)
        lst.insert(n - 2, el)
        return n - 1
    return n


def apply_to_deck(path: str, cmp: dict, currency: str | None, period_label: str, add_slide: bool = True, na: str = "-") -> dict:
    prs = Presentation(path)
    changes, flags, slides = update_plan_tables(prs, cmp, na)
    added = None
    if not slides and add_slide:
        added = add_plan_slide(prs, cmp, currency, period_label, na)
        if added:
            flags.append({"slide": added, "severity": "info", "message": f"Slide {added} 'Delivered vs booked' added from the media plan (the deck had no booked vs delivered table). "
                                                                       "Ask for 'no booked vs delivered slide' in feedback to leave it out."})
    prs.save(path)
    return {"changes": changes, "flags": flags, "slides": sorted(slides), "added_slide": added}
