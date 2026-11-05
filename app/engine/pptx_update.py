"""Update an existing client deck in place (on a copy): keep design, masters, slide order and all
unrelated content; replace reporting figures in recognised metric tables and charts, refresh period
labels, flag what could not be updated. Never flattens slides to images."""
from __future__ import annotations

import copy
import difflib
import io
import re
import zipfile

from pptx import Presentation
from pptx.chart.data import CategoryChartData

from . import fmt as FM
from .pptx_inspect import MONTH_YEAR_RE, clean, inspect_deck, label_to_metric, slide_title

TITLE_SPLIT = re.compile(r"\s+[–—-]\s+|:\s+")


def nkey(s: str) -> str:
    return re.sub(r"[^a-z0-9#]+", "", (s or "").lower())


def nkey_loose(s: str) -> str:
    return re.sub(r"#\d+$", "", nkey(s))


def split_title(t: str) -> tuple[str, str]:
    parts = TITLE_SPLIT.split(t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", t.strip()


def _set_text(tf_or_cell, text: str):
    """Replace text keeping the formatting of the first run."""
    tf = tf_or_cell.text_frame if hasattr(tf_or_cell, "text_frame") else tf_or_cell
    paras = tf.paragraphs
    p0 = paras[0]
    runs = p0.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        p0.text = text
    for p in paras[1:]:
        p._p.getparent().remove(p._p)


def _replace_in_runs(tf, pattern: re.Pattern, repl_fn) -> list[tuple[str, str]]:
    changes = []
    for p in tf.paragraphs:
        full = "".join(r.text for r in p.runs)
        if not pattern.search(full):
            continue
        done = False
        for r in p.runs:
            if pattern.search(r.text):
                old = r.text
                r.text = pattern.sub(repl_fn, r.text)
                if old != r.text:
                    changes.append((old, r.text))
                done = True
        if not done and p.runs:  # label split across runs -> rewrite paragraph in first run's format
            new = pattern.sub(repl_fn, full)
            p.runs[0].text = new
            for r in p.runs[1:]:
                r._r.getparent().remove(r._r)
            changes.append((full, new))
    return changes


class DeckUpdater:
    def __init__(self, src: str, calc, period: dict, profile: dict, answers: dict, options: dict | None = None):
        self.src = src
        self.calc = calc
        self.period = period
        self.profile = profile or {}
        self.deck_cfg = self.profile.get("deck") or {}
        self.answers = answers or {}
        self.options = options or {}
        self.na = self.deck_cfg.get("na_text", "-")
        self.changes: list[dict] = []
        self.flags: list[dict] = []
        self.questions: list[dict] = []
        self.slide_map: list[dict] = []
        self.inspection = inspect_deck(src, self.profile)
        self.entities = self._entities()

    # ------------------------------------------------------------------
    def _entities(self) -> list[dict]:
        t = self.calc.tables.get("creative") or self.calc.tables.get("ad") or self.calc.tables.get("campaign")
        if not t:
            return []
        name_dim = [d for d in t.dims if d in ("creative", "ad_name", "campaign_name")][-1]
        ents = []
        for i, r in enumerate(t.rows):
            ents.append({"id": i, "name": r[name_dim], "brand": r.get("brand"), "objective": r.get("objective"),
                         "row": r, "key": nkey(r[name_dim]), "loose": nkey_loose(r[name_dim])})
        return ents

    def _brand_hint(self, slide_info) -> str | None:
        codes = (self.profile.get("naming") or {}).get("brand_codes") or {}
        brands = {e["brand"] for e in self.entities if e["brand"]}
        text = " ".join(t["text"] for t in slide_info["texts"]).lower()
        for code, b in codes.items():
            if b in brands and re.search(rf"\b{re.escape(code.lower())}\b", text):
                return b
        for b in brands:
            if b and b.lower() in text:
                return b
        return None

    def _candidates(self, slide_info) -> list[dict]:
        obj, name = split_title(slide_info["title"])
        k, kl = nkey(name), nkey_loose(name)
        exact = [e for e in self.entities if e["key"] == k]
        if not exact:
            exact = [e for e in self.entities if e["loose"] == kl or e["key"] == kl or e["loose"] == k]
        if not exact:
            scored = [(difflib.SequenceMatcher(None, e["loose"], kl).ratio(), e) for e in self.entities]
            best = max([s for s, _ in scored], default=0)
            exact = [e for s, e in scored if s >= 0.88 and s >= best - 0.01]
        hint = self._brand_hint(slide_info)
        if hint:
            exact = [e for e in exact if e["brand"] == hint] or exact
        if obj and len({e["objective"] for e in exact}) > 1:
            same = [e for e in exact if nkey(e["objective"] or "") == nkey(obj)]
            exact = same or exact
        return exact

    def match_slides(self):
        slides = [s for s in self.inspection["slides"] if s["classification"] == "metric_slide"]
        cands = {s["index"]: self._candidates(s) for s in slides}
        assigned: dict[int, dict] = {}
        used: set[int] = set()
        # employee answers first
        for s in slides:
            a = self.answers.get(f"deckslide:{s['index']}")
            if a:
                if a == "skip":
                    assigned[s["index"]] = None
                    continue
                for e in self.entities:
                    if f"{e['brand']}|{e['objective']}|{e['name']}" == a:
                        assigned[s["index"]] = e
                        used.add(e["id"])
        order = [s["index"] for s in slides]

        def neighbours(i):
            pos = order.index(i)
            prev = next((assigned.get(order[j]) for j in range(pos - 1, -1, -1) if assigned.get(order[j])), None)
            nxt = next((assigned.get(order[j]) for j in range(pos + 1, len(order)) if assigned.get(order[j])), None)
            pb = assigned.get(order[pos - 1]) if pos > 0 else None
            nb = assigned.get(order[pos + 1]) if pos + 1 < len(order) else None
            return pb, nb

        changed = True
        while changed:
            changed = False
            for i in order:
                if i in assigned:
                    continue
                c = [e for e in cands[i] if e["id"] not in used]
                if len(c) == 1:
                    assigned[i] = c[0]
                    used.add(c[0]["id"])
                    changed = True
                    continue
                if not c:
                    continue
                pb, nb = neighbours(i)
                support = {e["id"]: 0 for e in c}
                for nbh in (pb, nb):
                    if nbh:
                        for e in c:
                            if e["brand"] == nbh["brand"]:
                                support[e["id"]] += 1
                best = max(support.values())
                tops = [e for e in c if support[e["id"]] == best]
                if best > 0 and len(tops) == 1:
                    assigned[i] = tops[0]
                    used.add(tops[0]["id"])
                    changed = True
        # remaining ambiguous -> question (default = closest to figures already on the slide)
        for s in slides:
            i = s["index"]
            if i in assigned:
                continue
            c = [e for e in cands[i] if e["id"] not in used] or cands[i]
            if not c:
                self.slide_map.append({"slide": i, "title": s["title"], "status": "unmatched", "entity": None})
                self.flags.append({"slide": i, "severity": "warning", "message": f"Slide {i} '{s['title']}': no matching creative in the uploaded data - left unchanged (figures may be stale)"})
                continue
            default = self._closest_by_old_values(s, c)
            self.questions.append({
                "key": f"deckslide:{i}",
                "text": f"Slide {i} '{s['title']}' matches more than one creative in the data. Which one should it show?",
                "options": [{"value": f"{e['brand']}|{e['objective']}|{e['name']}", "label": f"{e['brand'] or ''} – {e['objective'] or ''} – {e['name']}"} for e in c] + [{"value": "skip", "label": "Leave this slide unchanged"}],
                "default": f"{default['brand']}|{default['objective']}|{default['name']}" if default else None,
                "blocking": True, "context": "Suggested option is the one whose figures are closest to those currently on the slide."})
        self.assigned = assigned
        return assigned

    def _closest_by_old_values(self, s, cands):
        best, best_d = None, None
        for e in cands:
            d = 0.0
            n = 0
            for t in s["tables"]:
                if len(t["cells"]) < 2:
                    continue
                for ci, m in t["metric_cols"].items():
                    if m in ("range", "audience_size"):
                        continue
                    old = FM_parse(t["cells"][1][ci])
                    new = self._value(e["row"], m)
                    if old is None and new is None:
                        continue
                    if old is None or new is None:
                        d += 1
                    else:
                        d += abs(old - new) / max(abs(old), abs(new), 1)
                    n += 1
            if n and (best_d is None or d < best_d):
                best, best_d = e, d
        return best or cands[0]

    def _value(self, row: dict, metric: str):
        if metric == "clicks":
            return row.get("clicks")
        if metric == "conversions":
            return row.get("conversions")
        return row.get(metric)

    # ------------------------------------------------------------------
    def run(self, out_path: str) -> dict:
        prs = Presentation(self.src)
        self.match_slides()
        if any(q["blocking"] for q in self.questions):
            return {"blocked": True, "questions": self.questions, "slide_map": self.slide_map, "flags": self.flags}
        style = self._detect_range_style() or self.deck_cfg.get("range_style") or "plain"  # the deck's own style wins
        ds, de = self.period.get("data_start"), self.period.get("data_end")
        range_text = FM.range_label(ds, de, style)
        used_entities = set()
        for idx, slide in enumerate(prs.slides, 1):
            info = self.inspection["slides"][idx - 1]
            cls = info["classification"]
            entry = {"slide": idx, "title": info["title"], "classification": cls, "status": "unchanged", "entity": None, "changes": 0}
            if cls == "metric_slide":
                e = self.assigned.get(idx)
                if e is None:
                    if not any(m["slide"] == idx for m in self.slide_map):
                        entry["status"] = "skipped"
                    else:
                        continue
                else:
                    used_entities.add(e["id"])
                    entry["entity"] = " – ".join(str(x) for x in (e["brand"], e["objective"], e["name"]) if x)
                    entry["changes"] = self._update_metric_slide(slide, idx, e, range_text)
                    entry["status"] = "updated"
                    if e["row"].get("_rows", 1) > 1:
                        self.flags.append({"slide": idx, "severity": "info", "message": f"Slide {idx}: creative ran in {e['row']['_rows']} ads - reach/frequency shown as '{self.na}' because reach is not additive"})
                    if self.options.get("replace_thumbnails") and self.options.get("creatives_zip"):
                        self._replace_thumbnail(slide, idx, e)
                    self._check_notes(slide, idx)
            elif cls == "cover":
                n = self._update_period_text(slide, idx, ds, de)
                entry["changes"] = n
                entry["status"] = "updated" if n else "unchanged"
            elif cls == "chart_slide":
                n = self._update_charts(slide, idx)
                entry["changes"] = n
                entry["status"] = "updated" if n else "unchanged"
            elif cls == "client_content":
                entry["status"] = "client content - preserved"
                if info["date_ranges"] or info["month_labels"]:
                    self.flags.append({"slide": idx, "severity": "info", "message": f"Slide {idx} '{info['title']}': client-supplied content with its own dates ({', '.join((info['date_ranges'] or info['month_labels'])[:2])}) - not changed; confirm with the client team"})
            for u in info["unsupported"]:
                self.flags.append({"slide": idx, "severity": "warning", "message": f"Slide {idx}: {u['detail']} ({u['shape']})"})
            self.slide_map.append(entry)
        # creatives in data without a slide
        missing = [e for e in self.entities if e["id"] not in used_entities]
        for e in missing:
            spend = e["row"].get("spend") or 0
            imp = e["row"].get("impressions") or 0
            if self.options.get("add_missing_slides") and (imp > 0):
                newidx = self._clone_slide_for(prs, e, range_text)
                if newidx:
                    self.flags.append({"slide": newidx, "severity": "info", "message": f"Added slide {newidx} for new creative '{e['name']}' ({e['brand']}) by duplicating an existing creative slide - picture is a placeholder copy, please replace"})
                    continue
            self.flags.append({"slide": None, "severity": "warning" if imp else "info",
                               "message": "No slide in the deck for " + " ".join(str(x) for x in (e["brand"], e["objective"]) if x) + (" " if (e["brand"] or e["objective"]) else "") + f"'{e['name']}' (impressions {FM.fmt_int(imp)}, spend {FM.fmt_num(spend)}). Not added." + (" Ask for 'add slides for new creatives' if needed." if imp else "")})
        self.slide_map.sort(key=lambda m: m["slide"])
        prs.save(out_path)
        return {"blocked": False, "changes": self.changes, "flags": self.flags, "slide_map": self.slide_map,
                "questions": self.questions, "range_text": range_text}

    def _detect_range_style(self) -> str:
        for s in self.inspection["slides"]:
            for t in s["tables"]:
                for ci, m in t["metric_cols"].items():
                    if m == "range" and len(t["cells"]) > 1 and t["cells"][1][ci].strip():
                        return "ordinal" if re.search(r"\d(st|nd|rd|th)\b", t["cells"][1][ci]) else "plain"
        return None

    def _update_metric_slide(self, slide, idx, e, range_text) -> int:
        n = 0
        for sh in slide.shapes:
            if not (getattr(sh, "has_table", False) and sh.has_table):
                continue
            tbl = sh.table
            header = [clean(c.text) for c in tbl.rows[0].cells]
            mcols = {i: label_to_metric(h, self.deck_cfg.get("metric_labels")) for i, h in enumerate(header)}
            if len([m for m in mcols.values() if m and m not in ("range", "audience_size")]) < 3:
                continue
            for ri in range(1, len(tbl.rows)):
                for ci, m in mcols.items():
                    if not m:
                        continue
                    cell = tbl.cell(ri, ci)
                    old = clean(cell.text)
                    if m == "range":
                        new = range_text
                    elif m == "audience_size":
                        self._once(idx, "audience", f"Slide {idx}: 'Audience Size' is not in the uploaded exports - left as '{old}'")
                        continue
                    else:
                        val = self._value(e["row"], m)
                        st = FM.CellStyle(old)
                        dflt = {"frequency_calc": 2, "ctr": 2, "vtr": 2, "cpc": 2, "cpm": 2, "cpa": 2, "spend": 2, "roas": 2}.get(m, 0)
                        if st.dp is None:
                            st.dp = dflt
                            st.suffix = "%" if m in ("ctr", "vtr") else ""
                        if m in ("ctr", "vtr") and not st.suffix:
                            st.suffix = "%"
                        new = st.format(val, dflt, self.na)
                    if new != old:
                        _set_text(cell, new)
                        self.changes.append({"slide": idx, "shape": sh.name, "row": ri, "col": header[ci], "metric": m, "old": old, "new": new})
                        n += 1
                    else:
                        self.changes.append({"slide": idx, "shape": sh.name, "row": ri, "col": header[ci], "metric": m, "old": old, "new": new, "unchanged": True})
        return n

    def _once(self, idx, kind, msg):
        key = (kind,)
        if not hasattr(self, "_seen"):
            self._seen = set()
        if key in self._seen:
            return
        self._seen.add(key)
        self.flags.append({"slide": idx, "severity": "info", "message": msg.replace(f"Slide {idx}: ", "Metric slides: ")})

    def _update_period_text(self, slide, idx, ds, de) -> int:
        if not ds or not de:
            return 0
        n = 0
        for sh in slide.shapes:
            if not (getattr(sh, "has_text_frame", False) and sh.has_text_frame):
                continue

            def repl(m):
                long = len(m.group(0).split()[0].rstrip(".")) > 4
                return FM.month_label(ds, de, long=long)
            for old, new in _replace_in_runs(sh.text_frame, MONTH_YEAR_RE, repl):
                if old != new:
                    self.changes.append({"slide": idx, "shape": sh.name, "metric": "period_label", "old": old, "new": new})
                    n += 1
        return n

    def _update_charts(self, slide, idx) -> int:
        n = 0
        for sh in slide.shapes:
            if not (getattr(sh, "has_chart", False) and sh.has_chart):
                continue
            ch = sh.chart
            plot = ch.plots[0]
            cats = [clean(str(c)) for c in plot.categories]
            table = None
            order = ["platform", "brand", "creative", "ad", "campaign", "country", "language", "market", "objective"]
            for tk in order + [k for k in self.calc.tables if k not in order]:
                t = self.calc.tables.get(tk)
                if t and sum(1 for c in cats if any(nkey(c) == nkey(str(r[t.dims[-1]])) for r in t.rows)) >= max(1, len(cats) // 2):
                    table = t
                    break
            if not table:
                self.flags.append({"slide": idx, "severity": "warning", "message": f"Slide {idx}: chart '{sh.name}' categories {cats[:4]} don't match any data dimension - not updated"})
                continue
            cd = CategoryChartData()
            cd.categories = cats
            ok = 0
            for s in plot.series:
                m = label_to_metric(s.name, self.deck_cfg.get("metric_labels"))
                if not m:
                    cd.add_series(s.name, list(s.values))
                    self.flags.append({"slide": idx, "severity": "warning", "message": f"Slide {idx}: chart series '{s.name}' not recognised - kept old values"})
                    continue
                vals = []
                for c in cats:
                    r = next((r for r in table.rows if nkey(str(r[table.dims[-1]])) == nkey(c)), None)
                    vals.append(self._value(r, m) if r else None)
                cd.add_series(s.name, vals)
                ok += 1
            if ok:
                old = [list(s.values) for s in plot.series]
                ch.replace_data(cd)  # also rewrites the embedded workbook + chart cache
                self.changes.append({"slide": idx, "shape": sh.name, "metric": "chart", "old": str(old)[:200], "new": "updated series " + ", ".join(s.name for s in plot.series)})
                n += 1
        return n

    def _check_notes(self, slide, idx):
        if slide.has_notes_slide:
            t = clean(slide.notes_slide.notes_text_frame.text)
            if re.search(r"\d", t):
                self.flags.append({"slide": idx, "severity": "warning", "message": f"Slide {idx}: speaker notes contain figures that may be stale - review: '{t[:80]}'"})

    def _replace_thumbnail(self, slide, idx, e):
        zpath = self.options["creatives_zip"]
        try:
            zf = zipfile.ZipFile(zpath)
        except Exception:  # noqa: BLE001
            return
        key = nkey_loose(e["name"])
        match = None
        for n in zf.namelist():
            base = re.sub(r"\.(png|jpe?g)$", "", n.split("/")[-1], flags=re.I)
            base = re.sub(r"[-_ ]?ss$", "", base, flags=re.I)
            if nkey_loose(base) == key:
                match = n
                break
        pics = [sh for sh in slide.shapes if sh.shape_type == 13]
        if not match or len(pics) != 1:
            if not match:
                self.flags.append({"slide": idx, "severity": "info", "message": f"Slide {idx}: no thumbnail named like '{e['name']}' in the creatives ZIP - picture kept"})
            return
        blob = zf.read(match)
        if len(blob) > 20_000_000:
            return
        pic = pics[0]
        rid = pic._element.blipFill.blip.rEmbed
        img_part = slide.part.related_part(rid)
        img_part._blob = blob
        self.changes.append({"slide": idx, "shape": pic.name, "metric": "thumbnail", "old": "previous image", "new": match})

    def _clone_slide_for(self, prs, e, range_text):
        """Duplicate the most similar creative slide (same brand/objective) for a creative without a slide."""
        best = None
        for idx, ent in self.assigned.items():
            if ent and ent["brand"] == e["brand"]:
                if best is None or (ent["objective"] == e["objective"]):
                    best = idx
        if best is None:
            return None
        src = prs.slides[best - 1]
        layout = src.slide_layout
        new = prs.slides.add_slide(layout)
        for shp in list(new.shapes):
            shp._element.getparent().remove(shp._element)
        rid_map = {}
        for rel in src.part.rels.values():
            if "image" in rel.reltype or "media" in rel.reltype:
                rid_map[rel.rId] = new.part.relate_to(rel._target, rel.reltype)
        RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        for shp in src.shapes:
            el = copy.deepcopy(shp._element)
            for node in el.iter():
                for attr in ("embed", "link", "id"):
                    v = node.get(RNS + attr)
                    if v and v in rid_map:
                        node.set(RNS + attr, rid_map[v])
            new.shapes._spTree.insert_element_before(el, "p:extLst")
        # move after source slide
        sldIdLst = prs.slides._sldIdLst
        ids = list(sldIdLst)
        new_id = ids[-1]
        sldIdLst.remove(new_id)
        sldIdLst.insert(best, new_id)
        newidx = best + 1
        # retitle and fill
        for sh in new.shapes:
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame and clean(sh.text_frame.text) == slide_title(src):
                obj = split_title(slide_title(src))[0] or (e["objective"] or "")
                _set_text(sh, f"{obj} – {e['name'].replace('-', ' ')}")
        self._update_metric_slide(new, newidx, e, range_text)
        return newidx


def FM_parse(s: str):
    s = clean(s).replace(",", "").replace("%", "")
    s = re.sub(r"^[A-Za-z]{1,4}\s?", "", s)
    try:
        return float(s)
    except ValueError:
        return None
