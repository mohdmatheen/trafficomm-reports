"""Runs the local day x event reconciliation for an AI Support issue (FAB iOS All Events pilot).

Reads the uploaded exports and the verified report from the server's storage, compares them with the
client's current recipe and records aggregates on the issue. A candidate that reproduces every verified
date is proposed as a Client Intelligence rule for a reporting lead to approve; it is never applied here.
"""
from __future__ import annotations

import re

from ..db import Client, Profile, User
from ..engine import event_reconcile as R
from ..engine import event_report as ER
from . import fab_pilot, knowledge, notify
from .models import ClientRule, SupportIssue, SupportMessage


class ReconcileError(ValueError):
    pass


def _recipe(s, issue: SupportIssue) -> dict:
    c = s.get(Client, issue.client_id)
    prof = s.query(Profile).filter(Profile.client_id == c.id).first() if c else None
    recs = ER.recipes_for(c.name if c else "", (prof.settings or {}) if prof is not None else {})
    for r in recs:
        if r.get("key") == ER.FAB_IOS_PLT_ALL_EVENTS["key"]:
            return r
    return ER.FAB_IOS_PLT_ALL_EVENTS


def _extra_exclusions(text: str) -> list[str]:
    """Campaign names the employee listed (comma, semicolon or line separated); free text is ignored."""
    out = []
    for part in re.split(r"[,;\n]+", text or ""):
        p = part.strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-. ]{2,80}", p) and ("_" in p or "-" in p):
            out.append(p)
    return out[:30]


def run_reconciliation(s, issue: SupportIssue, user: User) -> dict:
    from .service import event
    if issue.pilot != fab_pilot.PILOT_KEY:
        raise ReconcileError("Reconciliation is available for the FAB iOS All Events pilot.")
    up = fab_pilot.uploaded_slots(s, issue)
    missing = [sl["label"] for sl in fab_pilot.SLOTS if sl["key"] in ("ios_in_app", "ios_non_organic", "verified_report") and not up.get(sl["key"])]
    if missing:
        raise ReconcileError("Upload first: " + "; ".join(missing) + ".")
    recipe = _recipe(s, issue)
    raw = [{"name": up[k][-1].original_name, "path": up[k][-1].stored_path} for k in ("ios_in_app", "ios_non_organic")]
    ver = up["verified_report"][-1]
    try:
        prev = ER.read_previous(ver.stored_path)
        target, unknown = R.verified_cells(prev, recipe)
        if not target:
            raise ReconcileError("The verified report has no day-by-event figures this report type knows. Upload the All Events report as sent to FAB.")
        ans = fab_pilot.answers(issue)
        out = R.reconcile(recipe, raw, target, _extra_exclusions(ans.get("extra_exclusions", "")))
    except ER.ReportError as e:
        raise ReconcileError(f"{e} {e.fix}") from e
    out["unmatched_verified_labels"] = unknown[:20]
    meta = dict(issue.meta or {})
    meta["reconciliation"] = out
    issue.meta = meta
    event(s, issue.id, user.id, "reconciliation", out["verdict"][:300])
    lines = [f"{d['date']}: portal {d['portal']}, verified {d['verified']} (cell difference {d['cell_error']})" for d in out["baseline"]["per_date"]]
    s.add(SupportMessage(issue_id=issue.id, kind="system", engine="rules",
                         text="Local reconciliation finished.\n" + "\n".join(lines) + "\n\n" + out["verdict"]))
    cand = out.get("rule_candidate")
    key = knowledge.slug(f"ios-all-events-{cand['key']}") if cand else ""
    pending = cand and s.query(ClientRule).filter(ClientRule.client_id == issue.client_id, ClientRule.key == key,
                                                  ClientRule.status.in_(("proposed", "approved"))).first()
    if cand and issue.client_id and not pending:
        body = (f"{cand['label']}. Reproduces the verified All Events figures exactly on {', '.join(out['dates'])} "
                f"(found by local reconciliation on {issue.ref}). Change: {cand['spec']}")
        knowledge.propose(s, user, issue.client_id, f"iOS All Events: {cand['label']}"[:300], body,
                              "exclusion" if "exclu" in cand["key"] else "calculation", key=key,
                              spec={"recipe": recipe.get("key"), **cand["spec"]}, source="reconciliation", issue_id=issue.id)
        approvers = [u.id for u in notify.leads_for_client(s, issue.client_id)] or [u.id for u in notify.admins(s)]
        notify.push(s, [a for a in approvers if a != user.id], "approval_required",
                    f"{issue.ref}: a reconciliation rule needs approval", issue=issue, link=f"/intelligence/{issue.client_id}")
    return out
