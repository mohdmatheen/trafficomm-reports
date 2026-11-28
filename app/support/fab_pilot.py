"""FAB iOS All Events discrepancy: the first end-to-end AI Support pilot.

Reported: the portal's IOS-PLT All events totals are higher than FAB's manually verified report
(25 Nov 2026: +11; 26 Nov 2026: +12; 26 Nov verified total 4191). The pilot collects exactly the
evidence an engineer needs, asks the assigned employee the open procedural questions directly,
and runs the local day x event reconciliation (app/engine/event_reconcile.py). Nothing here
subtracts a difference or hardcodes a total.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import SupportAttachment, SupportIssue

PILOT_KEY = "fab_ios_all_events"
TITLE = "FAB iOS All Events: portal totals higher than the verified report"

SLOTS = [
    {"key": "ios_in_app", "label": "iOS AppsFlyer in-app events export (CSV)", "ext": [".csv"], "required": True,
     "hint": "id1383237548_in-app-events_<from>_<to>_Asia_Dubai.csv, the same file used for the report."},
    {"key": "ios_non_organic", "label": "iOS AppsFlyer non-organic in-app events export (CSV)", "ext": [".csv"], "required": True,
     "hint": "id1383237548_non-organic-in-app-events_<from>_<to>_Asia_Dubai.csv"},
    {"key": "verified_report", "label": "Manually verified All Events report", "ext": [".xlsx", ".csv"], "required": True,
     "hint": "The report the team checked by hand (for 26 Nov it totals 4191). Several dates help validate the rule."},
    {"key": "internal_workbook", "label": "Internal FAB reporting workbook", "ext": [".xlsx"], "required": False,
     "hint": "‘Internal Report - FAB.xlsx’ (the Google Sheet export). Needed if the check compares month tabs."},
    {"key": "exclusions", "label": "Additional campaign exclusion instructions (optional file)", "ext": [".txt", ".xlsx", ".csv", ".docx", ".pdf"],
     "required": False, "hint": "Or type them in the questions below."},
]
SLOT_KEYS = {s["key"] for s in SLOTS}

QUESTIONS = [
    {"key": "discrepancy_dates", "label": "Which dates differ, and what are the portal and verified totals?",
     "default": "25 Nov 2026: portal higher by 11. 26 Nov 2026: portal higher by 12; verified total 4191.", "kind": "text"},
    {"key": "extra_exclusions", "label": "Besides the five CASA Always On campaigns, which campaigns (or campaign patterns) are excluded from All events, "
     "and why?", "kind": "text"},
    {"key": "dedup", "label": "How do you remove duplicates by hand? (Which ID, per day or per month, per file or after combining in-app and non-organic?)",
     "kind": "text"},
    {"key": "attribution", "label": "Which attribution rules apply? (e.g. are organic, retargeting, or specific media sources left out?)", "kind": "text"},
    {"key": "timezone", "label": "Which date and time zone do you count by? (Event Time or Install Time; Dubai time or UTC?)", "kind": "text"},
    {"key": "manual_adjustments", "label": "Do you make any manual adjustments after counting (removing test devices, late events, specific rows)?",
     "kind": "text"},
    {"key": "sheet_check", "label": "How should the Google Sheet check work for All events?", "kind": "choice",
     "options": [("A", "A: compare each month tab in the Internal Report against that campaign's figures"),
                 ("B", "B: a separate All events sheet will be provided"),
                 ("other", "Something else (explain below)")]},
    {"key": "sheet_check_detail", "label": "Anything else about the Google Sheet reconciliation?", "kind": "text", "optional": True},
]
QUESTION_KEYS = {q["key"] for q in QUESTIONS}


def is_fab(client_name: str | None) -> bool:
    n = (client_name or "").strip().lower()
    return n == "fab" or n.startswith("fab ") or n.startswith("fab_") or "first abu dhabi bank" in n


def answers(issue: SupportIssue) -> dict:
    return dict((issue.meta or {}).get("pilot_answers") or {})


def uploaded_slots(s, issue: SupportIssue) -> dict[str, list[SupportAttachment]]:
    out: dict[str, list] = {}
    for a in s.scalars(select(SupportAttachment).where(SupportAttachment.issue_id == issue.id).order_by(SupportAttachment.id)).all():
        out.setdefault(a.slot, []).append(a)
    return out


def pilot_missing(s, issue: SupportIssue) -> list[str]:
    if issue.pilot != PILOT_KEY:
        return []
    up, ans = uploaded_slots(s, issue), answers(issue)
    miss = [sl["label"] for sl in SLOTS if sl["required"] and not up.get(sl["key"])]
    if ans.get("sheet_check") == "A" and not up.get("internal_workbook"):
        miss.append("the Internal FAB reporting workbook (needed for option A)")
    unanswered = [q["label"] for q in QUESTIONS if not q.get("optional") and not str(ans.get(q["key"]) or "").strip()]
    if unanswered:
        miss.append(f"answers to {len(unanswered)} pilot question(s)")
    return miss


def pilot_questions(s, issue: SupportIssue) -> list[str]:
    """Unanswered pilot questions, phrased for the chat (the form on the issue page is the main way to answer)."""
    if issue.pilot != PILOT_KEY:
        return []
    ans = answers(issue)
    return [q["label"] for q in QUESTIONS if not q.get("optional") and not str(ans.get(q["key"]) or "").strip()][:3]


def save_answers(issue: SupportIssue, data: dict) -> dict:
    ans = answers(issue)
    for k, v in (data or {}).items():
        if k in QUESTION_KEYS:
            ans[k] = str(v or "").strip()[:4000]
    if ans.get("sheet_check") not in (None, "", "A", "B", "other"):
        ans["sheet_check"] = "other"
    meta = dict(issue.meta or {})
    meta["pilot_answers"] = ans
    issue.meta = meta
    return ans
