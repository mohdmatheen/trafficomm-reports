"""Daily app-event reports from AppsFlyer raw exports (first client: FAB CASA Always On).

What an employee did by hand every morning, as built-in rules:
  1. keep only the report's campaigns in each raw export (iOS and Android, in-app and non-organic
     in-app events), read the device ID from Event Value and drop duplicates (same device + event);
  2. merge the in-app and non-organic exports per platform and drop duplicates again;
  3. merge iOS and Android and do a final de-duplication;
  4. count events per day and fill the report template (one block per event, then a day x event table);
  5. paste the day rows into the client's monthly Google Sheet.

A "recipe" describes one report type (campaigns, events, platforms, columns). The built-in FAB recipe
is below; recipes Claude writes for new report types are saved on the client's profile once the
employee's Google Sheet is verified, and from then on run without Claude.

Nothing here calls a model. Every error message says what to fix.
"""
from __future__ import annotations

import calendar
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
from collections import Counter, OrderedDict

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
FAB_CASA_ALWAYS_ON = {
    "key": "fab_casa_always_on",
    "name": "FAB CASA Always On daily report",
    "builtin": True,
    "client_match": ["fab", "first abu dhabi bank"],
    "sheet_title": "FAB_CASA Always On",
    "file_prefix": "FAB_CASA Always On Report",
    "title_cell": None,
    "platforms": ["ios", "android"],
    "sources": ["in_app", "non_organic"],
    "campaign_match": "exact",
    "campaigns": ["Nine_Thousand_Cashback_PLT", "Dining_PLT", "Twenty_Percent_Cashback_PLT", "Travel_PLT", "Cinema_PLT"],
    "id_field": "event_value.deviceId",
    "columns": [
        {"header": "casa_ntb_congratulations_view", "event": "casa_ntb_congratulations_view"},
        {"header": "ntb_casa_isl_congratulation_view", "event": "ntb_casa_isl_congratulation_view"},
        {"header": "casa_etb_congratulations_view", "event": "casa_etb_congratulations_view"},
        {"header": "casa_etb_isl_congratulations_view", "event": "casa_etb_isl_congratulations_view"},
    ],
    "sheet_campaign_label": "CASA",
}
_ALWAYS_ON_CAMPAIGNS = list(FAB_CASA_ALWAYS_ON["campaigns"])

# FAB iOS (PLT) reports. Both use only the iOS app (id1383237548) in-app + non-organic exports and the
# monthly tab name changes ("FAB_IOS-PLT - Nov_All events"), so they are matched on their columns.
FAB_IOS_PLT_ALL_EVENTS = {
    "key": "fab_ios_plt_all_events",
    "name": "FAB IOS-PLT All events daily report",
    "builtin": True,
    "client_match": ["fab", "first abu dhabi bank"],
    "sheet_title": "FAB_IOS-PLT - Nov_All events",
    "file_prefix": "FAB_IOS-PLT Report All events",
    "title_cell": "IOS",
    "platforms": ["ios"],
    "sources": ["in_app", "non_organic"],
    # Every campaign except the 5 CASA Always On ones (those have their own report). Including them is what
    # roughly doubled 01-02 Nov when this layout was first read as a new report type.
    "campaign_match": "exclude",
    "campaigns": _ALWAYS_ON_CAMPAIGNS,
    "match_title": True,  # tab name must match too (the month part may change)
    "id_field": "event_value.deviceId",
    "columns": [
        {"header": "casa_and_cc_ntb_uae_contact_continue_click", "event": "", "constant_zero": True},
        {"header": "ntb_casa_isl_congratulation_view", "event": "ntb_casa_isl_congratulation_view"},
        {"header": "IOS Contact Details", "event": "cc_ntb_contactDetail_continue_click"},
        {"header": "IOS Islamic Cards", "event": "cc_ntb_isl_card_created"},
        {"header": "IOS ISL Contact details", "event": "cc_ntb_isl_contactdetail_continue_click"},
        {"header": "ntb_bundle_account_created", "event": "", "constant_zero": True},
        {"header": "ntb_bundle_card_created", "event": "", "constant_zero": True},
        {"header": "ntb_bundle_isl_account_created", "event": "", "constant_zero": True},
        {"header": "ntb_bundle_isl_card_created", "event": "", "constant_zero": True},
        {"header": "ntb_casa_account_created", "event": "", "constant_zero": True},
        {"header": "ntb_casa_isl_account_created", "event": "", "constant_zero": True},
        {"header": "IOS Fab Cards", "event": "ntb_cc_card_created"},
        {"header": "casa_cc_ntb_contactDetail_continue_click", "event": "", "constant_zero": True},
    ],
    "sheet_campaign_label": "",
}
FAB_IOS_PLT_CASA = {
    "key": "fab_ios_plt_casa",
    "name": "FAB IOS-PLT CASA daily report",
    "builtin": True,
    "client_match": ["fab", "first abu dhabi bank"],
    "sheet_title": "FAB_IOS CASA- Nov_All events",
    "file_prefix": "FAB_IOS-PLT Report - CASA",
    "title_cell": "IOS",
    "platforms": ["ios"],
    "sources": ["in_app", "non_organic"],
    "campaign_match": "contains",
    "campaigns": ["CASA"],
    "match_title": True,
    "id_field": "event_value.deviceId",
    "columns": [
        {"header": "casa_ntb_congratulations_view", "event": "casa_ntb_congratulations_view"},
        {"header": "ntb_casa_isl_congratulation_view", "event": "ntb_casa_isl_congratulation_view"},
    ],
    "sheet_campaign_label": "",
}
BUILTIN_RECIPES = [FAB_CASA_ALWAYS_ON, FAB_IOS_PLT_ALL_EVENTS, FAB_IOS_PLT_CASA]

PLATFORM_LABEL = {"ios": "iOS", "android": "Android"}
SOURCE_LABEL = {"in_app": "in-app events", "non_organic": "non-organic in-app events"}
REQUIRED_COLS = ("Event Time", "Event Name", "Campaign")
ID_COLUMNS = {"appsflyer id": "AppsFlyer ID", "idfv": "IDFV", "idfa": "IDFA", "advertising id": "Advertising ID",
              "android id": "Android ID", "customer user id": "Customer User ID"}


class ReportError(Exception):
    """A problem the employee can fix. `fix` says how."""

    def __init__(self, title: str, fix: str, detail: list[str] | None = None):
        super().__init__(title)
        self.title, self.fix, self.detail = title, fix, detail or []

    def as_dict(self) -> dict:
        return {"title": self.title, "fix": self.fix, "detail": self.detail}


def recipes_for(client_name: str, profile: dict) -> list[dict]:
    """Built-in recipes for this client plus the ones saved after a verified Claude-assisted report."""
    name = (client_name or "").strip().lower()
    out = []
    for r in BUILTIN_RECIPES:
        if any(name == m or name.startswith(m + " ") or name.startswith(m + "_") or m in name.split() for m in r["client_match"]):
            out.append(r)
    for r in (profile or {}).get("event_recipes") or []:
        if isinstance(r, dict) and r.get("columns"):
            out.append(r)
    return out


def has_event_reports(client_name: str, profile: dict) -> bool:
    return bool(recipes_for(client_name, profile))


def recipe_signature(recipe: dict) -> str:
    cols = [c["header"].strip().lower() for c in recipe.get("columns", [])]
    return hashlib.sha256(json.dumps([(recipe.get("sheet_title") or "").strip().lower(), cols], sort_keys=True).encode()).hexdigest()[:16]


def validate_recipe(r: dict) -> list[str]:
    """Problems with a recipe (one Claude wrote, or one saved earlier). Empty list = usable."""
    p = []
    if not isinstance(r, dict):
        return ["not an object"]
    if not (r.get("sheet_title") or "").strip():
        p.append("no sheet title")
    if not r.get("columns"):
        p.append("no event columns")
    for c in r.get("columns") or []:
        if not (c.get("header") or "").strip():
            p.append("a column has no header")
        if not (c.get("event") or "").strip() and not c.get("constant_zero"):
            p.append(f"column '{c.get('header')}' has no event")
    if r.get("campaign_match") not in ("exact", "contains", "exclude", "all"):
        p.append("campaign_match must be exact, contains, exclude or all")
    if r.get("campaign_match") in ("exact", "contains", "exclude") and not r.get("campaigns"):
        p.append("no campaigns listed")
    if not set(r.get("platforms") or []) <= {"ios", "android"} or not r.get("platforms"):
        p.append("platforms must be ios and/or android")
    if not set(r.get("sources") or []) <= {"in_app", "non_organic"} or not r.get("sources"):
        p.append("sources must be in_app and/or non_organic")
    idf = (r.get("id_field") or "").strip()
    if not (idf.startswith("event_value.") or idf.lower() in ID_COLUMNS):
        p.append("id_field must be event_value.<key> or one of " + ", ".join(ID_COLUMNS.values()))
    return p


# ---------------------------------------------------------------------------
# Reading AppsFlyer raw exports
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^(?P<app>.+?)_(?P<kind>non-organic-in-app-events|in-app-events)_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})", re.I)


def _read_table(path: str) -> tuple[list[str], list[list]]:
    if path.lower().endswith(".xlsx"):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it, [])]
        rows = [["" if v is None else v for v in r] for r in it]
        wb.close()
        return head, rows
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    rd = csv.reader(io.StringIO(text))
    head = [h.strip().lstrip("﻿") for h in next(rd, [])]
    return head, [r for r in rd if any(c.strip() for c in r)]


def sniff_appsflyer(path: str, name: str) -> dict | None:
    """Light check used at upload time: is this an AppsFlyer raw in-app events export, and which one?"""
    try:
        head, rows = _read_table(path)
    except Exception:  # noqa: BLE001
        return None
    if not all(c in head for c in REQUIRED_COLS) or "Event Value" not in head:
        return None
    info = _describe(name, head, rows)
    info.pop("_rows", None)
    return info


def _describe(name: str, head: list[str], rows: list[list]) -> dict:
    idx = {h: i for i, h in enumerate(head)}
    m = _NAME_RE.match(os.path.basename(name))
    platform = None
    app = m.group("app") if m else ""
    if app:
        platform = "ios" if re.fullmatch(r"id\d+", app, re.I) else "android" if "." in app else None
    if not platform:
        vals = Counter(str(r[idx["Platform"]]).lower() for r in rows[:500] if "Platform" in idx and idx["Platform"] < len(r))
        top = vals.most_common(1)[0][0] if vals else ""
        platform = "ios" if top == "ios" else "android" if top == "android" else None
    if not platform and "App ID" in idx and rows:
        a = str(rows[0][idx["App ID"]]) if idx["App ID"] < len(rows[0]) else ""
        platform = "ios" if re.fullmatch(r"id\d+", a, re.I) else "android" if "." in a else None
    source = None
    if m:
        source = "non_organic" if m.group("kind").lower().startswith("non-organic") else "in_app"
    times = [str(r[idx["Event Time"]])[:10] for r in rows if idx["Event Time"] < len(r) and str(r[idx["Event Time"]])[:4].isdigit()]
    return {"platform": platform, "source": source, "app": app or None, "rows": len(rows),
            "file_start": m.group("start") if m else None, "file_end": m.group("end") if m else None,
            "first_event": min(times) if times else None, "last_event": max(times) if times else None, "_rows": rows}


def _device_id(rec: dict, id_field: str) -> str:
    if id_field.startswith("event_value."):
        key = id_field.split(".", 1)[1]
        v = rec.get("Event Value") or ""
        if isinstance(v, str) and v.strip().startswith("{"):
            try:
                d = json.loads(v)
                got = d.get(key) if isinstance(d, dict) else None
                return str(got).strip() if got not in (None, "") else ""
            except ValueError:
                m = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(key), v)
                return m.group(1).strip() if m else ""
        return ""
    col = ID_COLUMNS.get(id_field.lower(), id_field)
    return str(rec.get(col) or "").strip()


def campaign_rule_text(recipe: dict) -> str:
    """The campaign filter in words, for checks and error messages."""
    mode, camps = recipe.get("campaign_match", "exact"), ", ".join(recipe.get("campaigns") or [])
    return {"all": "all campaigns", "exact": f"only these campaigns: {camps}", "contains": f"campaigns whose name contains: {camps}",
            "exclude": f"every campaign except: {camps}"}.get(mode, camps)


def _campaign_ok(c: str, recipe: dict) -> bool:
    mode = recipe.get("campaign_match", "exact")
    if mode == "all":
        return True
    c = (c or "").strip()
    if mode == "exact":
        return c in set(recipe["campaigns"])
    if mode == "exclude":
        return c not in set(recipe["campaigns"])
    low = c.lower()
    return any(x.lower() in low for x in recipe["campaigns"])


def _parse_day(s: str) -> dt.date | None:
    s = str(s or "").strip()
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Previous day's report
# ---------------------------------------------------------------------------
def read_previous(path: str) -> dict:
    """Layout and figures of the previous day's report (pivot output in A:B, day x event table from E)."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ReportError("The previous day's report couldn't be opened",
                          "Upload yesterday's report exactly as it was sent (an .xlsx file such as 'FAB_CASA Always On Report_24_11_2026.xlsx').",
                          [f"{type(e).__name__}: {str(e)[:200]}"])
    best = None
    for ws in wb.worksheets:
        hdr_row = None
        for r in range(1, min(ws.max_row, 15) + 1):
            if str(ws.cell(r, 1).value or "").strip().lower() == "row labels":
                hdr_row = r
                break
        if hdr_row is None:
            continue
        headers, col = [], 6
        # the day x event table starts in column F (E holds the dates); find it from the header row
        start = None
        for c in range(3, min(ws.max_column, 60) + 1):
            v = ws.cell(hdr_row, c).value
            if isinstance(v, str) and v.strip() and c >= 4:
                start = c
                break
        if start:
            col = start
            while col <= ws.max_column and isinstance(ws.cell(hdr_row, col).value, str) and ws.cell(hdr_row, col).value.strip():
                headers.append(ws.cell(hdr_row, col).value.strip())
                col += 1
        blocks: OrderedDict[str, dict] = OrderedDict()
        cur = None
        grand = None
        for r in range(hdr_row + 1, ws.max_row + 1):
            a, b = ws.cell(r, 1).value, ws.cell(r, 2).value
            if a is None and b is None:
                continue
            if isinstance(a, str) and a.strip().lower() == "grand total":
                grand = b
                break
            if isinstance(a, (dt.datetime, dt.date)):
                if cur is not None:
                    d = a.date() if isinstance(a, dt.datetime) else a
                    blocks[cur]["days"][d] = int(b or 0)
            elif isinstance(a, str) and a.strip():
                cur = a.strip()
                blocks[cur] = {"total": int(b or 0) if isinstance(b, (int, float)) else None, "days": {}}
        title_cell = None
        for r in range(1, hdr_row):
            v = ws.cell(r, 1).value
            if isinstance(v, str) and v.strip():
                title_cell = v.strip()
        dates = [d for blk in blocks.values() for d in blk["days"]]
        cand = {"sheet_title": ws.title, "title_cell": title_cell, "header_row": hdr_row, "headers": headers, "blocks": blocks,
                "grand_total": grand, "last_day": max(dates) if dates else None, "first_day": min(dates) if dates else None,
                "column_events": _column_events(ws, hdr_row, start, headers, blocks)}
        if best is None or len(headers) > len(best["headers"]):
            best = cand
    wb.close()
    if not best or not best["headers"]:
        raise ReportError("The previous day's report isn't in the expected layout",
                          "Upload yesterday's report as it was sent to the client: the event blocks with 'Row Labels' / 'Count of Event Name' in "
                          "columns A-B and the day-by-event table with the event names in row 3 from column F. Don't upload the Google Sheet here.")
    return best


def _column_events(ws, hdr_row, start, headers, blocks) -> list[dict]:
    """Which A:B block each day-table column reads (from its VLOOKUP formula), or a constant."""
    # data_only workbooks hold no formulas: previous_layout_formulas() refines this from the formulas
    return [{"header": h, "event": h if h in blocks else None} for h in headers] if start else []


def previous_layout_formulas(path: str, prev: dict) -> list[dict]:
    """Map each day-table column to the event block its VLOOKUP points at (columns can be renamed,
    e.g. 'IOS Contact Details' reading the cc_ntb_contactDetail_continue_click block)."""
    try:
        wb = openpyxl.load_workbook(path)
    except Exception:  # noqa: BLE001
        return prev["column_events"]
    ws = wb[prev["sheet_title"]]
    hdr = prev["header_row"]
    # block row ranges
    ranges = []
    cur, first = None, None
    for r in range(hdr + 1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and a.strip():
            if cur:
                ranges.append((cur, first, r - 1))
            if a.strip().lower() == "grand total":
                cur = None
                break
            cur, first = a.strip(), r + 1
    if cur:
        ranges.append((cur, first, ws.max_row))
    out = []
    start = None
    for c in range(4, ws.max_column + 1):
        if isinstance(ws.cell(hdr, c).value, str) and ws.cell(hdr, c).value.strip():
            start = c
            break
    for i, h in enumerate(prev["headers"]):
        f = ws.cell(hdr + 1, (start or 6) + i).value
        ev, const = None, False
        if isinstance(f, str) and f.startswith("="):
            m = re.search(r"\$A\$(\d+):\$B\$(\d+)", f)
            if m:
                r0 = int(m.group(1))
                for name, a, b in ranges:
                    if a <= r0 <= b + 1:
                        ev = name
                        break
        elif isinstance(f, (int, float)) and not f:
            const = True
        out.append({"header": h, "event": ev or (h if h in prev["blocks"] else None), "constant_zero": const and not ev})
    wb.close()
    return out


_MONTH_RE = re.compile(r"(?<![a-z])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", re.I)


def title_key(title: str) -> str:
    """A tab name with the month and year taken out: 'FAB_IOS-PLT - Nov_All events' and '... - Dec_All events' are the same report."""
    t = _MONTH_RE.sub("<m>", (title or "").strip().lower())
    return re.sub(r"\s+", " ", re.sub(r"\d+", "<n>", t))


def match_recipe(prev: dict, recipes: list[dict]) -> dict | None:
    heads = [h.strip().lower() for h in prev["headers"]]
    title = prev["sheet_title"].strip().lower()
    for r in recipes:
        rh = [c["header"].strip().lower() for c in r["columns"]]
        if rh == heads and (r.get("sheet_title") or "").strip().lower() == title:
            return r
    for r in recipes:  # same columns, month in the tab name changed
        if [c["header"].strip().lower() for c in r["columns"]] == heads and title_key(r.get("sheet_title")) == title_key(prev["sheet_title"]):
            return r
    for r in recipes:  # same columns, sheet renamed (not for report types that share columns with others)
        if not r.get("match_title") and [c["header"].strip().lower() for c in r["columns"]] == heads:
            return r
    return None


def describe_unknown(prev: dict, recipes: list[dict]) -> list[str]:
    lines = [f"Previous report tab '{prev['sheet_title']}' has these event columns: " + ", ".join(prev["headers"]) + "."]
    for r in recipes:
        lines.append(f"Known report type '{r['name']}' (tab '{r['sheet_title']}') has: " + ", ".join(c["header"] for c in r["columns"]) + ".")
    if not recipes:
        lines.append("No report type is saved for this client yet.")
    return lines


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def build_counts(recipe: dict, raw_files: list[dict]) -> dict:
    """raw_files: [{name, path}]. Returns counts per (day, event) plus every intermediate step."""
    probs = validate_recipe(recipe)
    if probs:
        raise ReportError("The saved report type is incomplete", "Ask an admin to check this client's saved report types.", probs)
    loaded = []
    unreadable, not_af = [], []
    for f in raw_files:
        try:
            head, rows = _read_table(f["path"])
        except Exception as e:  # noqa: BLE001
            unreadable.append(f"{f['name']}: {type(e).__name__}")
            continue
        missing = [c for c in REQUIRED_COLS if c not in head]
        if recipe["id_field"].startswith("event_value.") and "Event Value" not in head:
            missing.append("Event Value")
        elif not recipe["id_field"].startswith("event_value.") and ID_COLUMNS.get(recipe["id_field"].lower(), recipe["id_field"]) not in head:
            missing.append(ID_COLUMNS.get(recipe["id_field"].lower(), recipe["id_field"]))
        if missing:
            not_af.append(f"{f['name']}: missing column(s) {', '.join(missing)}")
            continue
        info = _describe(f["name"], head, rows)
        loaded.append({"name": f["name"], "head": head, "info": info})
    if unreadable:
        raise ReportError("Some raw files couldn't be read", "Re-download them from AppsFlyer (Export data > Raw data reports) as CSV and upload again.", unreadable)
    if not_af:
        raise ReportError("Some uploaded files aren't AppsFlyer in-app event exports",
                          "Upload the AppsFlyer raw reports 'In-app events' and 'Non-organic in-app events' (CSV) for each app. Remove other files.", not_af)
    unknown_kind = [f["name"] for f in loaded if not f["info"]["platform"] or not f["info"]["source"]]
    if unknown_kind:
        raise ReportError("Couldn't tell which app or export type some files are",
                          "Keep AppsFlyer's original file names, e.g. 'id1383237548_in-app-events_2026-11-01_2026-11-25_Asia_Dubai.csv' (iOS) and "
                          "'com.fab.personalbanking_non-organic-in-app-events_2026-11-01_2026-11-25_Asia_Dubai.csv' (Android).", unknown_kind)
    slots: dict[tuple, list] = {}
    for f in loaded:
        slots.setdefault((f["info"]["platform"], f["info"]["source"]), []).append(f)
    dup = [f"{PLATFORM_LABEL[k[0]]} {SOURCE_LABEL[k[1]]}: " + ", ".join(x["name"] for x in v) for k, v in slots.items() if len(v) > 1]
    if dup:
        raise ReportError("The same export was uploaded more than once",
                          "Keep one file per app and export type (the latest download) and remove the others.", dup)
    need = [(p, s) for p in recipe["platforms"] for s in recipe["sources"]]
    missing = [f"{PLATFORM_LABEL[p]} {SOURCE_LABEL[s]}" for p, s in need if (p, s) not in slots]
    if missing:
        raise ReportError(f"{len(missing)} raw export{'s are' if len(missing) > 1 else ' is'} missing",
                          f"This report needs {len(need)} AppsFlyer exports: " + "; ".join(f"{PLATFORM_LABEL[p]} {SOURCE_LABEL[s]}" for p, s in need)
                          + ". Download the missing one(s) for the same dates and add them to the task.", ["Missing: " + ", ".join(missing)])
    extra = [f["name"] for k, v in slots.items() if k not in need for f in v]
    warnings = []
    if extra:
        warnings.append("Not used by this report type (ignored): " + ", ".join(extra))
    ends = {f["info"]["file_end"] or f["info"]["last_event"] for k in need for f in slots[k]}
    starts = {f["info"]["file_start"] or f["info"]["first_event"] for k in need for f in slots[k]}
    if len({e for e in ends if e}) > 1:
        raise ReportError("The raw exports cover different dates",
                          "Download all exports for the same date range (from the 1st of the month to today) and upload them together.",
                          [f"{f['name']}: {f['info']['file_start']} to {f['info']['file_end']}" for k in need for f in slots[k]])

    events = [c["event"] for c in recipe["columns"] if c.get("event")]
    ev_set = set(events)
    steps, other_events, no_id, per_file = [], Counter(), 0, {}
    deduped: dict[tuple, list] = {}
    for key in need:
        f = slots[key][0]
        head = f["head"]
        idx = {h: i for i, h in enumerate(head)}
        kept, seen, total, in_camp = [], set(), 0, 0
        for r in f["info"]["_rows"]:
            total += 1
            rec = {h: (r[i] if i < len(r) else "") for h, i in idx.items()}
            if not _campaign_ok(str(rec.get("Campaign") or ""), recipe):
                continue
            in_camp += 1
            ev = str(rec.get("Event Name") or "").strip()
            if ev not in ev_set:
                other_events[ev] += 1
                continue
            did = _device_id(rec, recipe["id_field"])
            if not did:
                no_id += 1
                continue
            day = _parse_day(rec.get("Event Time"))
            if not day:
                continue
            k = (did, ev)
            if k in seen:
                continue
            seen.add(k)
            kept.append({"day": day, "event": ev, "id": did, "event_time": str(rec.get("Event Time")), "campaign": str(rec.get("Campaign") or ""),
                         "media_source": str(rec.get("Media Source") or ""), "partner": str(rec.get("Partner") or ""),
                         "campaign_id": str(rec.get("Campaign ID") or ""), "event_value": str(rec.get("Event Value") or ""),
                         "platform": key[0], "source": key[1]})
        deduped[key] = kept
        per_file[f["name"]] = {"rows": total, "campaign_rows": in_camp, "unique": len(kept)}
        steps.append({"step": f"{PLATFORM_LABEL[key[0]]} {SOURCE_LABEL[key[1]]}", "file": f["name"], "rows": total,
                      "in_campaigns": in_camp, "after_dedup": len(kept)})

    def merge(lists):
        seen, out = set(), []
        for lst in lists:
            for x in lst:
                k = (x["id"], x["event"])
                if k not in seen:
                    seen.add(k)
                    out.append(x)
        return out

    per_platform = {}
    for p in recipe["platforms"]:
        parts = [deduped[(p, s)] for s in recipe["sources"]]
        per_platform[p] = merge(parts)
        steps.append({"step": f"{PLATFORM_LABEL[p]} combined", "rows": sum(len(x) for x in parts), "after_dedup": len(per_platform[p])})
    final = merge([per_platform[p] for p in recipe["platforms"]])
    steps.append({"step": "Final (" + " + ".join(PLATFORM_LABEL[p] for p in recipe["platforms"]) + ")",
                  "rows": sum(len(v) for v in per_platform.values()), "after_dedup": len(final)})
    if not any(v["campaign_rows"] for v in per_file.values()):
        raise ReportError("None of the rows belong to this report's campaigns",
                          "Check that you exported the right apps and dates. This report counts " + campaign_rule_text(recipe) + ".",
                          [f"{n}: {v['rows']} rows, 0 in these campaigns" for n, v in per_file.items()])
    if no_id:
        warnings.append(f"{no_id} event row(s) had no device ID in '{recipe['id_field']}' and were left out (same as the manual process).")
    if other_events:
        warnings.append("Other events in these campaigns are not part of this report (not counted): "
                        + ", ".join(f"{k} ({v})" for k, v in other_events.most_common(8)))
    counts: dict[dt.date, Counter] = {}
    for x in final:
        counts.setdefault(x["day"], Counter())[x["event"]] += 1
    file_end = max((e for e in ends if e), default=None)
    last = max(counts) if counts else None
    end_day = dt.date.fromisoformat(file_end) if file_end else last
    if last and end_day and last > end_day:
        end_day = last
    month_start = end_day.replace(day=1) if end_day else None
    start = min(starts) if starts else None
    if start and month_start and dt.date.fromisoformat(start) > month_start:
        warnings.append(f"The exports start on {start}, not on {month_start.isoformat()}. Days before {start} will show 0 - export from the 1st of the month.")
    out_month = {d: c for d, c in counts.items() if month_start and d >= month_start}
    if len(out_month) < len(counts):
        warnings.append(f"Events before {month_start.isoformat()} were left out: the report covers one month.")
    return {"counts": out_month, "final": [x for x in final if month_start and x["day"] >= month_start], "steps": steps, "warnings": warnings,
            "end_day": end_day, "month_start": month_start, "events": events, "per_file": per_file}


def compare_previous(prev: dict, run: dict, recipe: dict) -> dict:
    """Days before the previous report's last day must not change; its last day was usually a part day."""
    col_events = [c for c in recipe["columns"] if c.get("event")]
    issues, same_days = [], 0
    if not prev.get("last_day"):
        return {"status": "warn", "text": "The previous report has no day figures to compare with.", "issues": []}
    if prev["last_day"].replace(day=1) != run["month_start"]:
        if run["month_start"] and prev["last_day"] < run["month_start"]:
            return {"status": "pass", "text": f"New month: the previous report covered {prev['last_day']:%B %Y}, this one starts on {run['month_start']:%d %B %Y}.",
                    "issues": [], "new_month": True}
        return {"status": "fail", "text": "The previous report is for a later month than the raw exports.", "issues": [
            f"Previous report ends {prev['last_day']:%d %b %Y}; the exports end {run['end_day']:%d %b %Y}. Upload today's exports, or the right previous report."]}
    if run["end_day"] and run["end_day"] < prev["last_day"]:
        return {"status": "fail", "text": "The raw exports are older than the previous report.", "issues": [
            f"The previous report already has {prev['last_day']:%d %b}; these exports end on {run['end_day']:%d %b}. Download today's exports from AppsFlyer."]}
    d = run["month_start"]
    while d < prev["last_day"]:
        row_ok = True
        for c in col_events:
            old = (prev["blocks"].get(c["event"]) or {}).get("days", {}).get(d, 0)
            new = run["counts"].get(d, Counter()).get(c["event"], 0)
            if old != new:
                row_ok = False
                issues.append(f"{d:%d %b} {c['header']}: previous report {old}, now {new}")
        same_days += row_ok
        d += dt.timedelta(days=1)
    n_days = (prev["last_day"] - run["month_start"]).days
    if not issues:
        text = (f"{run['month_start']:%d}-{(prev['last_day'] - dt.timedelta(days=1)):%d %b} match the previous report exactly"
                if n_days > 0 else "Nothing to compare yet (first day of the month).")
        return {"status": "pass", "text": text, "issues": [], "matched_days": same_days, "compared_days": n_days}
    return {"status": "warn", "text": f"{len(issues)} figure(s) on days already reported changed since the previous report", "issues": issues[:40],
            "matched_days": same_days, "compared_days": n_days}


def sheet_rows(recipe: dict, run: dict) -> list[dict]:
    """The rows the employee pastes into the client's monthly Google Sheet: one per day."""
    out = []
    d = run["month_start"]
    while d and d <= run["end_day"]:
        c = run["counts"].get(d, Counter())
        vals = [0 if col.get("constant_zero") else c.get(col["event"], 0) for col in recipe["columns"]]
        out.append({"date": d.isoformat(), "day": f"{d.day}-{d:%B}", "values": vals, "daily": sum(vals)})
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Writing the report workbook (same layout as the employee's file)
# ---------------------------------------------------------------------------
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
BOLD = Font(bold=True)
THIN = Side(style="thin", color="9BC2E6")


def write_workbook(recipe: dict, run: dict, prev: dict | None, path: str) -> dict:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (prev or {}).get("sheet_title") or recipe["sheet_title"]
    title_cell = (prev or {}).get("title_cell") if prev else recipe.get("title_cell")
    if title_cell:
        ws["A2"] = title_cell
        ws["A2"].font = BOLD
    hdr = 3
    ws.cell(hdr, 1, "Row Labels").font = BOLD
    ws.cell(hdr, 2, "Count of Event Name").font = BOLD
    for c in (1, 2):
        ws.cell(hdr, c).fill = HEAD_FILL
        ws.cell(hdr, c).border = Border(bottom=THIN)
    # blocks in the column order; an event with no rows still gets no block (as the pivot does)
    r = hdr + 1
    ranges = {}
    grand = 0
    order = []
    for col in recipe["columns"]:
        ev = col.get("event")
        if ev and ev not in order:
            order.append(ev)
    for ev in order:
        days = sorted((d, c[ev]) for d, c in run["counts"].items() if c.get(ev))
        if not days:
            continue
        tot = sum(n for _, n in days)
        ws.cell(r, 1, ev).font = BOLD
        ws.cell(r, 2, tot).font = BOLD
        first = r + 1
        for d, n in days:
            r += 1
            cell = ws.cell(r, 1, dt.datetime(d.year, d.month, d.day))
            cell.number_format = "yyyy-mm-dd"
            cell.alignment = Alignment(indent=1, horizontal="left")
            ws.cell(r, 2, n)
        ranges[ev] = (first, r)
        grand += tot
        r += 1
    ws.cell(r, 1, "Grand Total").font = BOLD
    ws.cell(r, 2, grand).font = BOLD
    for c in (1, 2):
        ws.cell(r, c).fill = HEAD_FILL
        ws.cell(r, c).border = Border(top=THIN)
    # day x event table, E = dates, F.. = one column per report column
    rows = sheet_rows(recipe, run)
    for j, col in enumerate(recipe["columns"]):
        cc = ws.cell(hdr, 6 + j, col["header"])
        cc.font = BOLD
        cc.alignment = Alignment(horizontal="center")
    for i, row in enumerate(rows):
        rr = hdr + 1 + i
        d = dt.date.fromisoformat(row["date"])
        dc = ws.cell(rr, 5, dt.datetime(d.year, d.month, d.day))
        dc.number_format = "yyyy-mm-dd"
        for j, col in enumerate(recipe["columns"]):
            ev = col.get("event")
            if col.get("constant_zero") or ev not in ranges:
                ws.cell(rr, 6 + j, 0)
            else:
                a, b = ranges[ev]
                ws.cell(rr, 6 + j, f"=IFERROR(VLOOKUP($E{rr},$A${a}:$B${b},2,0),0)")
    widths = {"A": 32.7, "B": 19.2, "C": 4, "D": 4, "E": 12.3}
    for k, w in widths.items():
        ws.column_dimensions[k].width = w
    for j, col in enumerate(recipe["columns"]):
        ws.column_dimensions[get_column_letter(6 + j)].width = max(14, len(col["header"]) + 6)
    ws.freeze_panes = "A4"

    # Google Sheet rows: values only, ready to paste
    gs = wb.create_sheet("Google Sheet rows")
    heads = ["Day", "Daily Count"] + [c["header"] for c in recipe["columns"]]
    for j, h in enumerate(heads, 1):
        c = gs.cell(1, j, h)
        c.font = BOLD
        c.fill = HEAD_FILL
    for i, row in enumerate(rows, 2):
        gs.cell(i, 1, row["day"])
        gs.cell(i, 2, row["daily"])
        for j, v in enumerate(row["values"], 3):
            gs.cell(i, j, v)
    gs.column_dimensions["A"].width = 16
    gs.column_dimensions["B"].width = 12
    for j in range(len(recipe["columns"])):
        gs.column_dimensions[get_column_letter(3 + j)].width = max(14, len(recipe["columns"][j]["header"]) + 4)

    # Final deduplicated: the rows behind every count (the source of the pivot in the manual file)
    fd = wb.create_sheet("Final deduplicated")
    cols = ["Event Time", "Event Name", "Event Value", "ID", "Partner", "Media Source", "Campaign", "Campaign ID", "Platform", "Export"]
    for j, h in enumerate(cols, 1):
        fd.cell(1, j, h).font = BOLD
    for i, x in enumerate(sorted(run["final"], key=lambda x: (x["day"], x["event"], x["event_time"])), 2):
        fd.cell(i, 1, dt.datetime(x["day"].year, x["day"].month, x["day"].day)).number_format = "yyyy-mm-dd"
        vals = [x["event"], x["event_value"], x["id"], x["partner"], x["media_source"], x["campaign"], x["campaign_id"],
                PLATFORM_LABEL[x["platform"]], SOURCE_LABEL[x["source"]]]
        for j, v in enumerate(vals, 2):
            fd.cell(i, j, v)
    for k, w in {"A": 12, "B": 34, "C": 52, "D": 40, "E": 16, "F": 22, "G": 30, "H": 18, "I": 9, "J": 26}.items():
        fd.column_dimensions[k].width = w
    fd.freeze_panes = "A2"
    wb.calculation.fullCalcOnLoad = True
    wb.save(path)
    return {"grand_total": grand, "blocks": {ev: {"rows": list(ranges[ev])} for ev in ranges}}


def output_name(recipe: dict, end_day: dt.date, prev_name: str | None = None) -> str:
    prefix = recipe.get("file_prefix")
    if not prefix and prev_name:
        prefix = re.sub(r"[_\s-]*\d{1,2}[_.-]\d{1,2}[_.-]\d{4}.*$", "", os.path.splitext(os.path.basename(prev_name))[0]) or None
    prefix = prefix or recipe["sheet_title"]
    return f"{prefix}_{end_day:%d_%m_%Y}.xlsx"


def read_report_back(path: str, recipe: dict) -> dict[str, dict]:
    """Re-read the written workbook (A:B blocks) - the figures shown in the portal come from the file itself."""
    prev = read_previous(path)
    return {ev: {d.isoformat(): n for d, n in blk["days"].items()} for ev, blk in prev["blocks"].items()}


# ---------------------------------------------------------------------------
# Google Sheet verification
# ---------------------------------------------------------------------------
_MONTH_NUM = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_NUM.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTH_NUM["sept"] = 9


def _sheet_day(v, year: int, month: int) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # spreadsheet serial date in a CSV
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})(?:[\s\-/.,]+(\d{2,4}))?", s)
    if m and m.group(2).lower() in _MONTH_NUM:
        y = int(m.group(3)) if m.group(3) else year
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, _MONTH_NUM[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s\-/.]+(\d{1,2})(?:[\s\-/.,]+(\d{2,4}))?", s)
    if m and m.group(1).lower() in _MONTH_NUM:
        y = int(m.group(3)) if m.group(3) else year
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, _MONTH_NUM[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:  # the agency's sheets are day/month
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "")
    if s in ("-", "—"):
        return 0
    try:
        return float(s)
    except ValueError:
        return "text"


def _load_grids(path: str, name: str) -> list[tuple[str, list[list]]]:
    if name.lower().endswith(".csv"):
        head, rows = _read_table(path)
        return [("CSV", [head] + rows)]
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        out.append((ws.title, grid))
    wb.close()
    return out


def verify_sheet(path: str, name: str, recipe: dict, rows: list[dict]) -> dict:
    """Compare the employee's Google Sheet export with the report's day rows. Every day in the
    report must be in the sheet with the same figure in every event column."""
    try:
        grids = _load_grids(path, name)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "title": "The Google Sheet export couldn't be opened", "errors": [
            f"{type(e).__name__}: {str(e)[:160]}"], "fix": "In Google Sheets use File > Download > Microsoft Excel (.xlsx) and upload that file.", "warnings": []}
    if not rows:
        return {"ok": False, "title": "The report has no day rows to check", "errors": [], "fix": "Rebuild the report first.", "warnings": []}
    first = dt.date.fromisoformat(rows[0]["date"])
    heads = [c["header"].strip().lower() for c in recipe["columns"]]
    best = None
    for tab, grid in grids:
        for ri, row in enumerate(grid[:60]):
            cells = [str(v or "").strip().lower() for v in row]
            pos = {h: cells.index(h) for h in heads if h in cells}
            if len(pos) < len(heads):
                continue
            day_col = next((i for i, v in enumerate(cells) if v in ("day", "date", "days")), None)
            daily_col = next((i for i, v in enumerate(cells) if v in ("daily count", "daily total", "total daily")), None)
            found = {}
            for rj in range(ri + 1, len(grid)):
                r = grid[rj]
                cand = [day_col] if day_col is not None else range(0, min(len(r), min(pos.values())))
                d = None
                for ci in cand:
                    if ci is not None and ci < len(r):
                        d = _sheet_day(r[ci], first.year, first.month)
                        if d:
                            break
                if d and d.year == first.year and d.month == first.month:
                    found.setdefault(d, (rj + 1, r))
            score = len([x for x in rows if dt.date.fromisoformat(x["date"]) in found])
            if best is None or score > best["score"]:
                best = {"tab": tab, "header_row": ri + 1, "pos": pos, "found": found, "score": score, "daily_col": daily_col}
    month = f"{first:%B %Y}"
    if not best:
        return {"ok": False, "title": "The event columns weren't found in the Google Sheet export", "errors": [
            "Expected a header row with: " + ", ".join(c["header"] for c in recipe["columns"]) + "."],
            "fix": "Download the whole monthly Google Sheet (File > Download > Microsoft Excel .xlsx), not the report file, and upload it here.", "warnings": []}
    if not best["score"]:
        return {"ok": False, "title": f"No {month} rows found in the Google Sheet export", "errors": [
            f"Tab '{best['tab']}' has the event columns (row {best['header_row']}) but no day rows for {month}."],
            "fix": f"Add the {month} day rows to the Google Sheet (Day like '{rows[0]['day']}'), paste the figures from the report, then download and upload again.",
            "warnings": []}
    errors, warnings, matched = [], [], 0
    for x in rows:
        d = dt.date.fromisoformat(x["date"])
        if d not in best["found"]:
            errors.append(f"{x['day']} is missing from the sheet. Add a row for it and paste: " +
                          ", ".join(f"{c['header']} {v}" for c, v in zip(recipe["columns"], x["values"])) + ".")
            continue
        rno, r = best["found"][d]
        bad = []
        for c, want in zip(recipe["columns"], x["values"]):
            ci = best["pos"][c["header"].strip().lower()]
            got = _num(r[ci] if ci < len(r) else None)
            if got == "text" or got is None:
                bad.append(f"{c['header']} is {'empty' if got is None else 'not a number'} (should be {want})")
            elif abs(float(got) - want) > 1e-9:
                bad.append(f"{c['header']} is {int(got) if float(got).is_integer() else got} (should be {want})")
        if bad:
            errors.append(f"{x['day']} (sheet row {rno}): " + "; ".join(bad) + ".")
        else:
            matched += 1
            if best["daily_col"] is not None:
                got = _num(r[best["daily_col"]] if best["daily_col"] < len(r) else None)
                if isinstance(got, (int, float)) and abs(got - x["daily"]) > 1e-9:
                    warnings.append(f"{x['day']} (sheet row {rno}): Daily Count shows {int(got) if float(got).is_integer() else got}, "
                                    f"the event columns add up to {x['daily']}. Check the Daily Count formula in that row.")
    last = dt.date.fromisoformat(rows[-1]["date"])
    later = sorted(d for d in best["found"] if d > last)
    for d in later:
        rno, r = best["found"][d]
        vals = [_num(r[best["pos"][h]] if best["pos"][h] < len(r) else None) for h in heads]
        if any(isinstance(v, (int, float)) and v for v in vals):
            warnings.append(f"{d.day}-{d:%B} (sheet row {rno}) already has figures, but the report ends on {last.day}-{last:%B}. Clear that row unless it's intended.")
    ok = not errors
    return {"ok": ok, "tab": best["tab"], "header_row": best["header_row"], "matched": matched, "days": len(rows),
            "title": (f"Google Sheet matches the report: {matched} of {len(rows)} days in tab '{best['tab']}'" if ok else
                      f"Google Sheet doesn't match the report on {len(errors)} day{'s' if len(errors) != 1 else ''}"),
            "errors": errors, "warnings": warnings,
            "fix": None if ok else "Copy the rows from the 'Google Sheet rows' tab of the report (or the table in the portal) into the monthly Google Sheet, "
                                    "then download it again (File > Download > Microsoft Excel .xlsx) and upload it here."}


# ---------------------------------------------------------------------------
# Claude: describing a new report type
# ---------------------------------------------------------------------------
def recipe_context(raw_files: list[dict], prev: dict, instructions: str, prev_columns: list[dict]) -> str:
    """What Claude sees to write a recipe: file kinds, headers, campaign and event names with counts,
    and the previous report's layout. No device IDs or other row-level data."""
    lines = []
    for f in raw_files:
        try:
            head, rows = _read_table(f["path"])
        except Exception:  # noqa: BLE001
            continue
        info = _describe(f["name"], head, rows)
        idx = {h: i for i, h in enumerate(head)}
        camps = Counter(str(r[idx["Campaign"]]) for r in rows if "Campaign" in idx and idx["Campaign"] < len(r))
        evs = Counter(str(r[idx["Event Name"]]) for r in rows if "Event Name" in idx and idx["Event Name"] < len(r))
        ev_sample = ""
        if "Event Value" in idx and rows:
            ev_sample = str(rows[0][idx["Event Value"]])[:120]
            ev_sample = re.sub(r'"([^"]+)"\s*:\s*"[^"]*"', r'"\1":"<value>"', ev_sample)
        lines.append(json.dumps({"file": f["name"], "platform": info["platform"], "export": info["source"], "rows": info["rows"],
                                 "dates": [info["file_start"], info["file_end"]], "columns": head,
                                 "event_value_keys_example": ev_sample,
                                 "campaigns": camps.most_common(25), "events": evs.most_common(40)}, default=str))
    blocks = {k: {"total": v["total"], "days": len(v["days"])} for k, v in prev["blocks"].items()}
    return ("<raw_exports>\n" + "\n".join(lines) + "\n</raw_exports>\n"
            f"<previous_report>{json.dumps({'tab': prev['sheet_title'], 'title_cell': prev['title_cell'], 'event_blocks_in_A_B': blocks, 'day_table_columns': prev_columns, 'last_day': str(prev['last_day'])}, default=str)}</previous_report>\n"
            f"<employee_instructions>{(instructions or '')[:4000]}</employee_instructions>")


RECIPE_PROMPT = """An employee needs a daily app-event count report built from AppsFlyer raw exports. Describe the report as a recipe.
How the report is made (fixed steps, you only choose the parameters):
 1. In each raw export keep rows whose Campaign matches (campaign_match 'exact' = Campaign equals one of `campaigns`;
    'contains' = Campaign contains one of `campaigns` as text; 'exclude' = every campaign except those in `campaigns`;
    'all' = no campaign filter).
 2. Keep rows whose Event Name is one of the columns' events. Device ID = id_field ('event_value.deviceId' reads the deviceId key
    of the Event Value JSON; or a column such as 'AppsFlyer ID', 'IDFV'). Drop duplicates (same device + event) per export,
    then per platform (in-app + non-organic), then across platforms.
 3. Count per day and event. columns = the day table columns of the previous report, in the same order, with their exact header;
    event = the Event Name counted in that column; constant_zero true only when the previous report shows 0 typed in (no formula).
Use the previous report's day table columns and blocks to decide. platforms: 'ios' and/or 'android'; sources: 'in_app' and/or 'non_organic'
(include only the exports this report uses, e.g. an 'IOS' report uses ios only). sheet_title = previous report tab name; title_cell = the
text above 'Row Labels' if any. Never invent campaign or event names: copy them from the data. If the instructions and data don't let you
decide, set ok=false and say in question exactly what the employee must clarify."""
