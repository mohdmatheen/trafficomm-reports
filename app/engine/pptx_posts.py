"""Row tables in client decks: tables that list several posts, ads or segments, one per row, on a
slide titled after a platform (e.g. 'Facebook Performance' with a row per boosted post), often
grouped into month sections ('August Performance', 'September Performance').

The creative-per-slide updater (pptx_update.DeckUpdater) cannot match these: the slide title is a
platform, not a creative. This module

* reads posts from the dataset (platform, post date, title and link from the agency's naming
  convention 'FB_..._Post-2_05-Oct_(100 days to go)_(https://...)'),
* when the reporting month has no section yet, copies the latest month section for it and, if the
  employee asked, removes the oldest one (a rolling 'August-September' -> 'September-October' deck),
* rebuilds post-list tables from the posts of that platform, matches other row tables by label,
* fills the screenshot grid slides of the new section with the uploaded screenshots of that platform.

It works on a python-pptx Presentation in place and reports changes/flags like DeckUpdater."""
from __future__ import annotations

import copy
import datetime as dt
import io
import math
import re

from pptx.dml.color import RGBColor
from pptx.util import Pt

from . import fmt as FM
from . import rules as R
from .pptx_inspect import clean, label_to_metric

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
SECTION_RE = re.compile(rf"^\s*({MONTH_RE})\.?(?:\s+(20\d\d))?\s+(?:performance|results|report|overview|highlights)\s*$", re.I)
MONTH_RANGE_RE = re.compile(rf"\b({MONTH_RE})\b(\s*[-–—]\s*|\s+(?:to|and|&)\s+)\b({MONTH_RE})\b", re.I)
# '..._Post-2_05-Oct_(100 days to go)_(https://...)', 'Post_30-Oct_( latest episode)', 'Post-21-Sep-(New York ...)'
POST_RE = re.compile(rf"post(?:[-_ ]?\d+)?[-_ ]+(\d{{1,2}})[-_ ]({MONTH_RE})[-_ ]*\(\s*([^()]*?)\s*\)", re.I)
POST_NOPAREN_RE = re.compile(rf"post(?:[-_ ]?\d+)?[-_ ]+(\d{{1,2}})[-_ ]({MONTH_RE})[-_ ]*\(\s*([^()]+?)(?=https?://|$)", re.I)
URL_RE = re.compile(r"https?://[^\s()]+")
X_STATUS_RE = re.compile(r"promoted post (\d{8,})", re.I)

# deck platform word -> (dataset platform, matcher on the row's text)
PLATFORM_WORDS = {
    "facebook": "facebook", "fb": "facebook", "instagram": "instagram", "ig": "instagram", "insta": "instagram",
    "youtube": "youtube", "yt": "youtube", "google": "google", "linkedin": "linkedin", "li": "linkedin",
    "tiktok": "tiktok", "tt": "tiktok", "x": "x", "twitter": "x", "snapchat": "snapchat", "snap": "snapchat",
    "meta": "meta",
}
DISPLAY = {"facebook": "Facebook", "instagram": "Instagram", "youtube": "YouTube", "linkedin": "LinkedIn", "tiktok": "TikTok",
           "x": "X", "snapchat": "Snapchat", "google": "Google Ads", "meta": "Meta"}
PREFIX_FOR = {"facebook": "FB", "instagram": "IG", "youtube": "YT", "linkedin": "LinkedIn", "tiktok": "TikTok", "x": "X",
              "snapchat": "Snap", "google": "Google", "meta": "Meta"}
NAME_HEADERS = {"name", "post", "post name", "posts", "creative", "creatives", "ad", "ad name", "ads", "campaign", "campaigns",
                "ad set", "adset", "targeting", "audience", "segment", "market", "country", "platform", "content", "title"}
LINK_HEADERS = {"link", "links", "url", "urls", "post link", "post url"}


def month_no(word: str) -> int | None:
    w = (word or "").lower()[:3]
    for i, m in enumerate(MONTHS, 1):
        if m.startswith(w):
            return i
    return None


def nk(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def deck_platform(title: str) -> str | None:
    """'Facebook Performance' -> 'facebook'; 'X' -> 'x'. None when the title names no platform."""
    words = re.findall(r"[a-z]+", (title or "").lower())
    for w in words:
        if w in PLATFORM_WORDS and (w != "x" or len(words) <= 3):
            return PLATFORM_WORDS[w]
    return None


def row_platform(r: dict) -> str:
    p = (r.get("platform") or "").lower()
    text = " ".join(str(r.get(k) or "") for k in ("publisher_platform", "campaign_name", "adset_name", "ad_name", "creative")).lower()
    if p == "meta":
        pub = str(r.get("publisher_platform") or "").lower()
        if "instagram" in pub or re.search(r"(^|[^a-z])(ig|instagram)[_ -]", text) or "instagram.com" in text:
            return "instagram"
        return "facebook"
    if p in ("google_ads", "google", "youtube"):
        return "youtube" if ("youtu" in text or "youtube" in text or p == "youtube") else "google"
    return p


def parse_post(r: dict) -> dict:
    """Post date, title and link from the row's names (agency naming convention)."""
    fields = [str(r.get(k)) if r.get(k) is not None and str(r.get(k)).lower() not in ("nan", "none", "(not set)") else ""
              for k in ("ad_name", "adset_name", "campaign_name", "creative")]
    day = mon = title = None
    for f in fields:
        m = POST_RE.search(f) or POST_NOPAREN_RE.search(f)
        if m:
            day, mon, title = int(m.group(1)), month_no(m.group(2)), re.sub(r"\s+", " ", m.group(3)).strip(" _-")
            break
    url = None
    for f in fields:
        u = URL_RE.search(f)
        if u:
            url = u.group(0).rstrip(")_.,")
            break
    if not url:
        m = X_STATUS_RE.search(" ".join(fields))
        if m:
            url = f"https://x.com/i/status/{m.group(1)}"
    label = next((f for f in fields if f.strip()), "")
    return {"day": day, "month": mon, "title": title, "url": url, "label": label}


def url_key(u: str | None) -> str | None:
    if not u:
        return None
    m = re.search(r"status/(\d{8,})", u)
    if m:
        return "x" + m.group(1)
    u = re.sub(r"^https?://(www\.|m\.)?", "", u.split("?")[0]).rstrip("/")
    return nk(u)


class Posts:
    """Posts aggregated per (platform, post) from the prepared dataset of the reporting period."""

    METRICS = ["impressions", "reach", "views", "spend", "clicks", "conversions", "link_clicks"]

    def __init__(self, dataset, year: int | None):
        self.year = year
        self.items: list[dict] = []
        self.inactive = 0
        self.unnamed = 0
        if dataset is None or not len(dataset):
            return
        d = dataset[dataset["_period"] == "current"] if "_period" in dataset.columns else dataset
        groups: dict[tuple, dict] = {}
        for _, row in d.iterrows():
            r = row.to_dict()
            plat = row_platform(r)
            p = parse_post(r)
            if not p["label"] and not p["url"]:
                self.unnamed += 1  # e.g. a 'Total Reach & Frequency' summary row: not a post
                continue
            if p["title"] and p["day"] and p["month"]:
                key = (plat, p["month"], p["day"], nk(p["title"]))
            elif url_key(p["url"]):
                key = (plat, "u", url_key(p["url"]))
            else:
                key = (plat, "n", nk(p["label"]))
            g = groups.setdefault(key, {"platform": plat, **p, "rows": 0, "_labels": set(), **{m: None for m in self.METRICS}})
            g["rows"] += 1
            g["url"] = g["url"] or p["url"]
            for k in ("ad_name", "adset_name", "campaign_name", "creative"):
                if r.get(k) and str(r.get(k)) != "nan":
                    g["_labels"].add(nk(r[k]))
            for m in self.METRICS:
                v = r.get(m)
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    g[m] = (g[m] or 0) + float(v)
        for g in groups.values():
            if not (g["impressions"] or 0) and not (g["spend"] or 0):
                self.inactive += 1
                continue
            g["frequency_calc"] = (g["impressions"] / g["reach"]) if g["impressions"] and g["reach"] else None
            g["ctr"] = (100.0 * g["clicks"] / g["impressions"]) if g["clicks"] is not None and g["impressions"] else None
            g["cpm"] = (1000.0 * g["spend"] / g["impressions"]) if g["spend"] is not None and g["impressions"] else None
            g["cpc"] = (g["spend"] / g["clicks"]) if g["spend"] is not None and g["clicks"] else None
            g["vtr"] = (100.0 * g["views"] / g["impressions"]) if g["views"] is not None and g["impressions"] else None
            g["video_plays_3s"] = g["views"]
            self.items.append(g)
        self.items.sort(key=lambda g: (g["platform"], g["month"] or 99, g["day"] or 99, g["title"] or g["label"]))

    def for_platform(self, plat: str) -> list[dict]:
        if plat == "meta":
            return [g for g in self.items if g["platform"] in ("facebook", "instagram")]
        return [g for g in self.items if g["platform"] == plat]

    def platforms(self) -> set[str]:
        return {g["platform"] for g in self.items}


def post_name(g: dict, prefix: str, long_month: bool) -> str:
    if g["title"] and g["day"] and g["month"]:
        mon = MONTHS[g["month"] - 1].capitalize() if long_month else MONTHS[g["month"] - 1][:3].capitalize()
        return f"{prefix}-Post-{g['day']:02d}-{mon}_({g['title']})"
    return g["label"]


# ----------------------------------------------------------------------------------------------
# deck structure
def table_shapes(slide):
    return [sh for sh in slide.shapes if getattr(sh, "has_table", False) and sh.has_table]


def pictures(slide):
    return [sh for sh in slide.shapes if sh.shape_type == 13]


def slide_texts(slide) -> list[str]:
    return [clean(sh.text_frame.text) for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.has_text_frame and clean(sh.text_frame.text)]


def row_table_info(tbl, overrides=None) -> dict | None:
    """A table with a label column, >=2 metric columns and >=2 data rows."""
    rows = [[clean(c.text) for c in r.cells] for r in tbl.rows]
    if len(rows) < 3:
        return None
    header = rows[0]
    from .media_plan import is_plan_header
    if is_plan_header(header):
        return None  # booked vs delivered table: filled from the media plan
    mcols = {i: label_to_metric(h, overrides) for i, h in enumerate(header)}
    mcols = {i: m for i, m in mcols.items() if m and m not in ("range", "audience_size")}
    if len(mcols) < 2:
        return None
    label_col = next((i for i, h in enumerate(header) if h.lower() in NAME_HEADERS and i not in mcols), None)
    if label_col is None:
        label_col = next((i for i in range(len(header)) if i not in mcols and not header[i].lower() in LINK_HEADERS), None)
    if label_col is None:
        return None
    link_col = next((i for i, h in enumerate(header) if h.lower() in LINK_HEADERS), None)
    labels = [r[label_col] for r in rows[1:]]
    postlike = sum(1 for l in labels if POST_RE.search(l) or re.search(rf"post[-_ ]+\d{{1,2}}[-_ ]{MONTH_RE}", l, re.I))
    return {"header": header, "metric_cols": mcols, "label_col": label_col, "link_col": link_col, "labels": labels,
            "post_list": postlike >= max(1, len([l for l in labels if l]) // 2)}


def is_row_table(slide, tbl, overrides=None) -> dict | None:
    """Row table on a platform slide, or a table whose first column is a labelled list
    ('Targeting', 'Campaign', 'Market' ...) with several rows."""
    inf = row_table_info(tbl, overrides)
    if not inf:
        return None
    if deck_platform(" ".join(slide_texts(slide))):
        return inf
    labels = [l for l in inf["labels"] if l]
    if inf["header"][inf["label_col"]].lower() in NAME_HEADERS and len(set(labels)) >= 2:
        return inf
    return None


def find_sections(prs) -> list[dict]:
    """Month sections: a slide whose only text is '<Month> Performance' and the slides after it."""
    heads = []
    for i, s in enumerate(prs.slides):
        t = slide_texts(s)
        if len(t) == 1 and not table_shapes(s):
            m = SECTION_RE.match(t[0])
            if m:
                heads.append((i, month_no(m.group(1)), int(m.group(2)) if m.group(2) else None, t[0]))
    secs = []
    n = len(prs.slides)
    for k, (i, mon, yr, text) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else n
        # the section ends before a closing slide (title layout without tables/platform content)
        body = list(range(i + 1, end))
        while body:
            last = prs.slides[body[-1]]
            if table_shapes(last) or deck_platform(" ".join(slide_texts(last))):
                break
            body.pop()
        secs.append({"head": i, "month": mon, "year": yr, "text": text, "slides": body})
    return secs


def duplicate_slide(prs, src_index: int, insert_at: int):
    """Copy slide src_index (0-based) to position insert_at, keeping pictures and table formatting."""
    src = prs.slides[src_index]
    new = prs.slides.add_slide(src.slide_layout)
    for shp in list(new.shapes):
        shp._element.getparent().remove(shp._element)
    rid_map = {}
    for rel in list(src.part.rels.values()):
        if rel.is_external:
            rid_map[rel.rId] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        elif "slideLayout" in rel.reltype or "notesSlide" in rel.reltype:
            continue
        else:
            rid_map[rel.rId] = new.part.relate_to(rel._target, rel.reltype)
    RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    for el in src.shapes._spTree.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag in ("nvGrpSpPr", "grpSpPr", "extLst"):
            continue
        e2 = copy.deepcopy(el)
        for node in e2.iter():
            for attr in list(node.attrib):
                if attr.startswith(RNS) and node.get(attr) in rid_map:
                    node.set(attr, rid_map[node.get(attr)])
        new.shapes._spTree.append(e2)
    if src.background is not None and src._element.cSld.bg is not None:
        new._element.cSld.insert(0, copy.deepcopy(src._element.cSld.bg))
    lst = prs.slides._sldIdLst
    el = list(lst)[-1]
    lst.remove(el)
    lst.insert(insert_at, el)
    return new


def delete_slide(prs, index: int):
    lst = prs.slides._sldIdLst
    sld = list(lst)[index]
    prs.part.drop_rel(sld.rId)
    lst.remove(sld)


def set_cell(cell, text: str):
    tf = cell.text_frame
    p0 = tf.paragraphs[0]
    if p0.runs:
        p0.runs[0].text = text
        for r in p0.runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        p0.text = text
    for p in tf.paragraphs[1:]:
        p._p.getparent().remove(p._p)
    for hl in cell._tc.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}hlinkClick"):
        hl.getparent().remove(hl)


def resize_rows(sh, n_data: int):
    """Make the table hold exactly n_data data rows (copies the last data row's formatting)."""
    tbl = sh.table
    trs = list(tbl._tbl.tr_lst)
    while len(trs) - 1 < n_data:
        new = copy.deepcopy(trs[-1])
        trs[-1].addnext(new)
        trs = list(tbl._tbl.tr_lst)
    while len(trs) - 1 > n_data and len(trs) > 2:
        tbl._tbl.remove(trs[-1])
        trs = list(tbl._tbl.tr_lst)


def fit_table(sh, slide_height: int):
    """Shrink row heights (and fonts, down to 6pt) when the table would run off the slide."""
    tbl = sh.table
    total = sum(r.height for r in tbl.rows)
    room = slide_height - sh.top - int(slide_height * 0.03)
    if total <= room or total == 0:
        sh.height = total
        return False
    f = room / total
    for r in tbl.rows:
        r.height = int(r.height * f)
        for c in r.cells:
            for p in c.text_frame.paragraphs:
                for run in p.runs:
                    if run.font.size:
                        run.font.size = Pt(max(6, run.font.size.pt * max(f, 0.6)))
    sh.height = sum(r.height for r in tbl.rows)
    return True


# ----------------------------------------------------------------------------------------------
# screenshots
SHOT_PREFIX_RE = re.compile(r"^\s*([A-Za-z]+)[-_ ]+post", re.I)


def shot_platform(label: str) -> str | None:
    m = SHOT_PREFIX_RE.match(label or "")
    return PLATFORM_WORDS.get(m.group(1).lower()) if m else None


def shot_order_key(label: str):
    m = re.search(rf"(\d{{1,2}})[-_ ]({MONTH_RE})", label or "", re.I)
    return (month_no(m.group(2)) or 99, int(m.group(1))) if m else (99, 99)


def place_grid(slide, blobs: list[tuple[str, bytes]], box=None) -> int:
    """Replace the slide's pictures with the screenshots, laid out in a grid inside the area the old
    pictures covered (keeps each screenshot's aspect ratio, black border)."""
    from PIL import Image
    pics = pictures(slide)
    if box is None:
        if not pics:
            return 0
        left = min(p.left for p in pics)
        top = min(p.top for p in pics)
        right = max(p.left + p.width for p in pics)
        bottom = max(p.top + p.height for p in pics)
        box = (left, top, right - left, bottom - top)
    for p in pics:
        p._element.getparent().remove(p._element)
    if not blobs:
        return 0
    sizes = []
    for _, b in blobs:
        with Image.open(io.BytesIO(b)) as im:
            sizes.append(im.size)
    ar = sum(w / h for w, h in sizes) / len(sizes)
    bl, bt, bw, bh = box
    n = len(blobs)
    best = None
    for rows in range(1, n + 1):
        cols = math.ceil(n / rows)
        cw, chh = bw / cols, bh / rows
        w = min(cw * 0.92, chh * 0.92 * ar)
        if best is None or w > best[0]:
            best = (w, rows, cols)
    w, rows, cols = best
    h = w / ar
    cw, chh = bw / cols, bh / rows
    for k, ((label, b), (iw, ih)) in enumerate(zip(blobs, sizes)):
        r, c = divmod(k, cols)
        in_row = min(cols, n - r * cols)
        x0 = bl + (bw - in_row * cw) / 2 + c * cw
        sw = min(w, h * iw / ih)
        sh_ = sw * ih / iw
        pic = slide.shapes.add_picture(io.BytesIO(b), int(x0 + (cw - sw) / 2), int(bt + r * chh + (chh - sh_) / 2), int(sw), int(sh_))
        pic.line.color.rgb = RGBColor(*R.SCREENSHOT_BORDER_RGB)
        pic.line.width = Pt(R.SCREENSHOT_BORDER_PT)
        pic.name = f"Screenshot {k + 1}"
    return n


# ----------------------------------------------------------------------------------------------
class RowTableUpdater:
    def __init__(self, prs, calc, period: dict, deck_cfg: dict, answers: dict, options: dict, screenshots: dict):
        self.prs = prs
        self.calc = calc
        self.period = period or {}
        self.deck_cfg = deck_cfg or {}
        self.answers = answers or {}
        self.options = options or {}
        self.qprefix = self.options.get("question_prefix") or ""
        self.na = self.deck_cfg.get("na_text", "-")
        self.screenshots = screenshots
        self.used_screenshots: set[str] = set()
        self.changes: list[dict] = []
        self.flags: list[dict] = []
        self.questions: list[dict] = []
        self.slide_status: dict[int, dict] = {}   # final 1-based slide index -> status entry
        ds = self.period.get("data_start")
        de = self.period.get("data_end")
        self.month = (de.month if de else None) if (ds and de and (ds.year, ds.month) == (de.year, de.month)) else None
        self.year = de.year if de else None
        self.posts = Posts(getattr(calc, "dataset", None), self.year)

    # ------------------------------------------------------------------
    def has_row_tables(self) -> bool:
        for s in self.prs.slides:
            for sh in table_shapes(s):
                if is_row_table(s, sh.table, self.deck_cfg.get("metric_labels")):
                    return True
        return False

    def plan_roll(self) -> dict | None:
        """When the reporting month has no section, return what to do (may raise a question)."""
        secs = find_sections(self.prs)
        if not secs or not self.month:
            return None
        if any(s["month"] == self.month for s in secs):
            return {"secs": secs, "add": False, "drop": []}
        latest = secs[-1]
        new_name = MONTHS[self.month - 1].capitalize()
        drop = []
        if len(secs) >= 2:
            oldest = secs[0]
            old_name = MONTHS[oldest["month"] - 1].capitalize()
            instr = (self.options.get("instructions") or "").lower()
            asked_drop = re.search(rf"(remove|delete|drop|exclude|without)\b[^.\n]{{0,40}}\b{old_name.lower()[:3]}", instr)
            asked_keep = re.search(rf"(keep|retain)\b[^.\n]{{0,40}}\b{old_name.lower()[:3]}", instr)
            key = f"{self.qprefix}deck_roll"
            a = self.answers.get(key)
            if a is None and asked_drop and not asked_keep:
                a = "drop"
            elif a is None and asked_keep and not asked_drop:
                a = "keep"
            if a is None:
                self.questions.append({
                    "key": key, "blocking": True, "default": "drop",
                    "text": (f"{self.options['deck_name']}: " if self.options.get("deck_name") else "") +
                            f"The deck has month sections for {', '.join(MONTHS[s['month'] - 1].capitalize() for s in secs)}. "
                            f"A {new_name} section will be added (copied from {MONTHS[latest['month'] - 1].capitalize()}). What should happen to {old_name}?",
                    "options": [{"value": "drop", "label": f"Remove the {old_name} section (keep {len(secs)} months)"},
                                {"value": "keep", "label": f"Keep {old_name} as well"}],
                    "context": "Suggested option keeps the deck the same length, like a rolling report."})
                return {"secs": secs, "blocked": True}
            if a == "drop":
                drop = [oldest]
        return {"secs": secs, "add": True, "latest": latest, "drop": drop}

    def restructure(self, roll: dict):
        """Copy the latest section for the reporting month, drop the oldest if asked, fix the cover."""
        secs = roll["secs"]
        new_name = MONTHS[self.month - 1].capitalize()
        latest = roll["latest"]
        idxs = [latest["head"]] + latest["slides"]
        insert_at = idxs[-1] + 1
        self.new_slides = []
        for k, si in enumerate(idxs):
            duplicate_slide(self.prs, si, insert_at + k)
        head = self.prs.slides[insert_at]
        for sh in head.shapes:
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame and SECTION_RE.match(clean(sh.text_frame.text)):
                old = clean(sh.text_frame.text)
                new = re.sub(MONTH_RE, new_name, old, count=1, flags=re.I) if not re.search(r"20\d\d", old) else \
                    re.sub(rf"{MONTH_RE}\.?\s+20\d\d", f"{new_name} {self.year}", old, count=1, flags=re.I)
                for p in sh.text_frame.paragraphs:
                    for r in p.runs:
                        if re.search(MONTH_RE, r.text, re.I):
                            r.text = re.sub(rf"\b{MONTH_RE}\b", new_name, r.text, count=1, flags=re.I)
                            break
                self.changes.append({"slide": insert_at + 1, "shape": sh.name, "metric": "section", "old": old, "new": clean(sh.text_frame.text)})
        new_ids = [id(self.prs.slides[insert_at + k]._element) for k in range(len(idxs))]
        removed = 0
        for sec in roll["drop"]:
            for si in sorted([sec["head"]] + sec["slides"], reverse=True):
                delete_slide(self.prs, si)
                removed += 1
            self.flags.append({"slide": None, "severity": "info",
                               "message": f"Removed the {MONTHS[sec['month'] - 1].capitalize()} section ({len(sec['slides']) + 1} slides) as asked"})
        self.new_section_slides = [i for i, s in enumerate(self.prs.slides) if id(s._element) in new_ids]
        self.flags.append({"slide": self.new_section_slides[0] + 1, "severity": "info",
                           "message": f"Added a {new_name} section (slides {self.new_section_slides[0] + 1}-{self.new_section_slides[-1] + 1}) "
                                      f"copied from {MONTHS[latest['month'] - 1].capitalize()} and filled with the uploaded data"})
        # cover / title month ranges ('Boosting August-September Report' -> 'September-October')
        months_now = [s["month"] for s in find_sections(self.prs)]
        if months_now:
            for i, s in enumerate(self.prs.slides):
                if i not in (0, len(self.prs.slides) - 1):
                    continue
                for sh in s.shapes:
                    if not (getattr(sh, "has_text_frame", False) and sh.has_text_frame):
                        continue
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            m = MONTH_RANGE_RE.search(r.text)
                            if m:
                                longf = len(m.group(1)) > 4
                                a = MONTHS[months_now[0] - 1].capitalize()
                                b = MONTHS[months_now[-1] - 1].capitalize()
                                if not longf:
                                    a, b = a[:3], b[:3]
                                old = r.text
                                r.text = r.text[:m.start()] + a + m.group(2) + b + r.text[m.end():]
                                self.changes.append({"slide": i + 1, "shape": sh.name, "metric": "period_label", "old": old, "new": r.text})
        return removed

    # ------------------------------------------------------------------
    def run(self, target_slides: set[int] | None = None):
        """Update row tables. target_slides (0-based): restrict post-list rebuilding to these slides
        (the reporting month's section); other row tables are matched by label only."""
        secs = find_sections(self.prs)
        cur_sec = next((s for s in secs if s["month"] == self.month), None)
        in_month = set(([cur_sec["head"]] + cur_sec["slides"]) if cur_sec else [])
        sh_h = self.prs.slide_height
        grid_slides: dict[str, list[int]] = {}
        for i, s in enumerate(self.prs.slides):
            title = " ".join(slide_texts(s))
            plat = deck_platform(title)
            tables = [(sh, is_row_table(s, sh.table, self.deck_cfg.get("metric_labels"))) for sh in table_shapes(s)]
            tables = [(sh, inf) for sh, inf in tables if inf]
            if not tables:
                if plat and len(pictures(s)) >= 2 and (not secs or i in in_month):
                    grid_slides.setdefault(plat, []).append(i)
                continue
            for sh, inf in tables:
                if secs and i not in in_month and inf["post_list"]:
                    self.slide_status[i + 1] = {"status": "earlier month - kept", "changes": 0}
                    continue
                rebuilt = bool(inf["post_list"] and plat)
                n = self._rebuild_post_table(i, s, sh, inf, plat, sh_h) if rebuilt else self._match_rows(i, sh, inf, plat)
                self.slide_status[i + 1] = {"status": "updated" if n else "unmatched", "changes": n, "platform": plat, "rebuilt": rebuilt and bool(n)}
        self._fill_grids(grid_slides, new_section=bool(getattr(self, "new_section_slides", None)))
        if not any(v.get("rebuilt") for v in self.slide_status.values()):
            return
        if self.posts.unnamed:
            self.flags.append({"slide": None, "severity": "info",
                               "message": f"{self.posts.unnamed} row(s) without a campaign, ad set or ad name (e.g. a totals sheet) were not treated as posts"})
        if self.posts.inactive:
            self.flags.append({"slide": None, "severity": "info",
                               "message": f"{self.posts.inactive} post(s) in the exports had no impressions or spend in this period and were left out of the tables"})

    def _value(self, g: dict, metric: str):
        if metric in ("link_clicks", "clicks"):
            return g.get("clicks") if g.get("clicks") is not None else g.get("link_clicks")
        return g.get(metric)

    def _fmt(self, old: str, metric: str, val, column_vals=None):
        st = FM.CellStyle(old)
        if metric == "spend" and st.numeric:
            # money: 2 decimals when any figure in the column has cents (never more than 2)
            vals = [v for v in (column_vals or [val]) if v is not None]
            st.dp = 2 if any(abs(v - round(v)) > 0.004 for v in vals) else min(st.dp or 0, 2)
        dflt = {"frequency_calc": 2, "ctr": 2, "vtr": 2, "cpc": 2, "cpm": 2, "cpa": 2, "spend": 2, "roas": 2}.get(metric, 0)
        if not st.numeric:
            st.dp = dflt if metric not in ("spend",) else 2
            st.thousands = True
            st.suffix = "%" if metric in ("ctr", "vtr") else ""
        return st.format(val, dflt, self.na)

    def _rebuild_post_table(self, i, slide, sh, inf, plat, sh_h) -> int:
        posts = self.posts.for_platform(plat)
        if not posts:
            have = ", ".join(sorted(self.posts.platforms())) or "none"
            self.flags.append({"slide": i + 1, "severity": "warning",
                               "message": f"Slide {i + 1} '{' '.join(slide_texts(slide))[:40]}': no {DISPLAY.get(plat, plat)} posts in the uploaded data "
                                          f"(platforms found: {have}) - table left as it was"})
            return 0
        old_rows = [[clean(c.text) for c in r.cells] for r in sh.table.rows]
        sample = old_rows[1]
        first_label = next((r[inf["label_col"]] for r in old_rows[1:] if r[inf["label_col"]]), "")
        m = re.match(r"^([A-Za-z]+)-post", first_label, re.I)
        prefix = m.group(1) if m else PREFIX_FOR.get(plat, plat.capitalize())
        long_month = bool(re.search(r"-(january|february|march|april|june|july|august|september|october|november|december)\b", " ".join(r[inf["label_col"]] for r in old_rows[1:]), re.I)) and \
            not re.search(r"-(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)[_(]", " ".join(r[inf["label_col"]] for r in old_rows[1:]), re.I)
        resize_rows(sh, len(posts))
        n = 0
        for ri, g in enumerate(posts, 1):
            name = post_name(g, prefix, long_month)
            set_cell(sh.table.cell(ri, inf["label_col"]), name)
            if inf["link_col"] is not None:
                set_cell(sh.table.cell(ri, inf["link_col"]), g["url"] or self.na)
            for ci, met in inf["metric_cols"].items():
                val = self._value(g, met)
                new = self._fmt(sample[ci], met, val, [self._value(x, met) for x in posts])
                set_cell(sh.table.cell(ri, ci), new)
                self.changes.append({"slide": i + 1, "shape": sh.name, "row": ri, "col": inf["header"][ci], "metric": met,
                                     "old": old_rows[ri][ci] if ri < len(old_rows) else "", "new": new, "post": name})
                n += 1
        missing = sorted({inf["header"][ci] for ci, met in inf["metric_cols"].items() if all(self._value(g, met) is None for g in posts)})
        if missing:
            self.flags.append({"slide": i + 1, "severity": "warning",
                               "message": f"Slide {i + 1} ({DISPLAY.get(plat, plat)}): {', '.join(missing)} not in the uploaded {DISPLAY.get(plat, plat)} export - shown as '{self.na}'"})
        if fit_table(sh, sh_h):
            self.flags.append({"slide": i + 1, "severity": "info", "message": f"Slide {i + 1}: {len(posts)} posts - rows made smaller to fit the slide; check readability"})
        self.flags.append({"slide": i + 1, "severity": "info", "message": f"Slide {i + 1}: {DISPLAY.get(plat, plat)} table rebuilt with {len(posts)} post(s) from the uploaded data"})
        return n

    def _match_rows(self, i, sh, inf, plat) -> int:
        """Rows are segments/campaigns/ads: update rows whose label matches the data, flag the rest."""
        d = getattr(self.calc, "dataset", None)
        n = 0
        unmatched = []
        posts = self.posts.for_platform(plat) if plat else self.posts.items
        for ri, label in enumerate(inf["labels"], 1):
            if not label:
                continue
            key = nk(label)
            hits = [g for g in posts if key and (key in g["_labels"] or (g["title"] and nk(g["title"]) == key) or url_key(g.get("url")) == key)]
            if not hits and d is not None:
                hits = self._dim_match(d, label, plat)
            if not hits:
                unmatched.append(label)
                continue
            agg = {m: (sum(h[m] for h in hits if h.get(m) is not None) if any(h.get(m) is not None for h in hits) else None) for m in Posts.METRICS}
            agg["frequency_calc"] = agg["impressions"] / agg["reach"] if agg.get("impressions") and agg.get("reach") else None
            agg["ctr"] = 100 * agg["clicks"] / agg["impressions"] if agg.get("clicks") is not None and agg.get("impressions") else None
            agg["cpm"] = 1000 * agg["spend"] / agg["impressions"] if agg.get("spend") is not None and agg.get("impressions") else None
            agg["cpc"] = agg["spend"] / agg["clicks"] if agg.get("spend") is not None and agg.get("clicks") else None
            agg["vtr"] = 100 * agg["views"] / agg["impressions"] if agg.get("views") is not None and agg.get("impressions") else None
            agg["video_plays_3s"] = agg["views"]
            for ci, met in inf["metric_cols"].items():
                cell = sh.table.cell(ri, ci)
                old = clean(cell.text)
                new = self._fmt(old, met, self._value(agg, met))
                if new != old:
                    set_cell(cell, new)
                    self.changes.append({"slide": i + 1, "shape": sh.name, "row": ri, "col": inf["header"][ci], "metric": met, "old": old, "new": new})
                    n += 1
        if unmatched:
            hdr = inf["header"][inf["label_col"]]
            self.flags.append({"slide": i + 1, "severity": "warning",
                               "message": f"Slide {i + 1}: {len(unmatched)} row(s) of the '{hdr}' table are not in the uploaded data ({', '.join(unmatched[:4])}) - left unchanged. "
                                          f"The exports need a breakdown or campaign/ad set names that contain these {hdr.lower()} labels."})
        return n

    def _dim_match(self, d, label, plat) -> list[dict]:
        key = nk(label)
        if not key or len(key) < 3:
            return []
        cur = d[d["_period"] == "current"] if "_period" in d.columns else d
        for dim in ("country", "market", "region", "campaign_name", "adset_name", "ad_name", "creative", "platform"):
            if dim not in cur.columns:
                continue
            vals = cur[dim].dropna().astype(str)
            exact = cur[vals.map(nk) == key] if len(vals) else []
            sel = exact if len(exact) else cur[cur[dim].astype(str).map(lambda v: key in nk(v))]
            if len(sel):
                if plat:
                    sel = sel[[row_platform(r.to_dict()) == plat or plat == "meta" for _, r in sel.iterrows()]]
                if len(sel):
                    g = {m: (float(sel[m].sum(min_count=1)) if m in sel.columns and sel[m].notna().any() else None) for m in Posts.METRICS}
                    return [g]
        return []

    def _fill_grids(self, grid_slides: dict[str, list[int]], new_section: bool):
        if not self.screenshots:
            if new_section:
                plats = sorted(grid_slides)
                if plats:
                    self.flags.append({"slide": None, "severity": "warning",
                                       "message": "No screenshots were uploaded: the new month's screenshot slides still show the previous month's posts (" + ", ".join(DISPLAY.get(p, p) for p in plats) + ")"})
            return
        by_plat: dict[str, list[str]] = {}
        for k, s in self.screenshots.items():
            p = shot_platform(s["label"])
            if p:
                by_plat.setdefault(p, []).append(k)
        delete = []
        for plat, idxs in grid_slides.items():
            keys = sorted(by_plat.get(plat, []), key=lambda k: shot_order_key(self.screenshots[k]["label"]))
            if not keys:
                if new_section:
                    self.flags.append({"slide": idxs[0] + 1, "severity": "warning",
                                       "message": f"Slide {idxs[0] + 1}: no {DISPLAY.get(plat, plat)} screenshots uploaded (name them like '{PREFIX_FOR.get(plat, plat)}-Post-05-Oct_(title).png') - previous pictures kept"})
                continue
            blobs = []
            for k in keys:
                try:
                    blobs.append((self.screenshots[k]["label"], self.screenshots[k]["load"]()))
                except Exception:  # noqa: BLE001
                    self.flags.append({"slide": idxs[0] + 1, "severity": "warning", "message": f"Screenshot '{self.screenshots[k]['label']}' could not be read"})
            caps = [max(len(pictures(self.prs.slides[i])), 1) for i in idxs]
            pos = 0
            for j, (si, cap) in enumerate(zip(idxs, caps)):
                take = blobs[pos:] if j == len(idxs) - 1 else blobs[pos:pos + cap]
                pos += len(take)
                if not take:
                    delete.append(si)
                    continue
                placed = place_grid(self.prs.slides[si], take)
                self.changes.append({"slide": si + 1, "shape": "screenshots", "metric": "screenshot", "old": f"{cap} picture(s)",
                                     "new": f"{placed} screenshot(s) (black border): " + ", ".join(t[0] for t in take[:3]) + ("..." if len(take) > 3 else "")})
                self.slide_status[si + 1] = {"status": "updated", "changes": placed, "platform": plat}
            self.used_screenshots.update(keys)
        self.deleted = []
        for si in sorted(set(delete), reverse=True):
            delete_slide(self.prs, si)
            self.deleted.append(si + 1)
            for c in self.changes + self.flags:
                if isinstance(c.get("slide"), int) and c["slide"] > si + 1:
                    c["slide"] -= 1
            self.flags.append({"slide": None, "severity": "info", "message": f"Removed an extra screenshot slide (fewer screenshots this month)"})
            self.slide_status = {(k - 1 if k > si + 1 else k): v for k, v in self.slide_status.items() if k != si + 1}
