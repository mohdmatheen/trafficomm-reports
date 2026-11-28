"""Client Intelligence: versioned, approval-gated client reporting rules.

* Anyone with access to a client may PROPOSE a rule (employees, the support agent, the engineering agent).
* Only an admin, or a reporting lead assigned to that client, may APPROVE or REJECT it.
* Approving version n supersedes the previously approved version of the same key; history is kept.
* Only approved rules are used by the support agent to answer issues deterministically. Rules that
  change calculations become permanent reporting logic only through a reviewed pull request
  (engineering workflow); the rule records that PR.
* Rules seeded from code (built-in FAB report types, client profiles) are marked source='code'.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import func, select

from ..db import AppSetting, AuditLog, Client, Profile, User, UserClient, now
from .models import RULE_CATEGORIES, ClientRule

WORD = re.compile(r"[a-z0-9_]{3,}")
STOP = set("the and for with from that this was were are has have had not but you your our their its into than then when what which who "
           "report reports portal total totals figure figures number numbers please kindly issue problem".split())


class RuleError(ValueError):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:100] or "rule"


def can_approve(s, user: User, client_id: int) -> bool:
    if user.role == "admin":
        return True
    if not getattr(user, "is_lead", False):
        return False
    return bool(s.scalar(select(UserClient.id).where(UserClient.user_id == user.id, UserClient.client_id == client_id)))


def keywords(*texts) -> list[str]:
    out = []
    for t in texts:
        for w in WORD.findall(str(t or "").lower()):
            if w not in STOP and w not in out:
                out.append(w)
    return out[:60]


def propose(s, user: User | None, client_id: int, title: str, body: str, category: str, key: str | None = None,
            spec: dict | None = None, source: str = "employee", issue_id: int | None = None, pr_url: str | None = None) -> ClientRule:
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise RuleError("Give the rule a short title.")
    if not body:
        raise RuleError("Describe the rule: what it applies to and exactly what to do.")
    if category not in RULE_CATEGORIES:
        raise RuleError("Choose a rule category.")
    key = slug(key or title)
    last = s.scalar(select(func.max(ClientRule.version)).where(ClientRule.client_id == client_id, ClientRule.key == key)) or 0
    r = ClientRule(client_id=client_id, key=key, version=last + 1, category=category, title=title[:300], body=body[:20000],
                   spec=spec or {}, keywords=keywords(title, body, json.dumps(spec or {})), status="proposed", source=source,
                   issue_id=issue_id, pr_url=pr_url, proposed_by=user.id if user else None)
    s.add(r)
    s.flush()
    s.add(AuditLog(user_id=user.id if user else None, action="rule_propose", target=f"client {client_id} {key} v{r.version}"))
    return r


def decide(s, user: User, rule_id: int, approve: bool, note: str = "", validated: bool | None = None) -> ClientRule:
    r = s.get(ClientRule, rule_id)
    if not r:
        raise RuleError("Unknown rule")
    if not can_approve(s, user, r.client_id):
        raise RuleError("Only an admin or a reporting lead for this client can approve rules.")
    if r.status != "proposed":
        raise RuleError(f"This version is already {r.status}.")
    if approve and r.proposed_by == user.id and user.role != "admin":
        raise RuleError("A rule needs a second person: ask another reporting lead or an admin to approve your proposal.")
    r.status = "approved" if approve else "rejected"
    r.decided_by, r.decided_at, r.decision_note = user.id, now(), (note or "")[:500]
    if validated is not None:
        r.validated = bool(validated)
    if approve:
        for old in s.scalars(select(ClientRule).where(ClientRule.client_id == r.client_id, ClientRule.key == r.key,
                                                      ClientRule.status == "approved", ClientRule.id != r.id)):
            old.status = "superseded"
    s.add(AuditLog(user_id=user.id, action="rule_approve" if approve else "rule_reject", target=f"client {r.client_id} {r.key} v{r.version}"))
    return r


def approved(s, client_id: int) -> list[ClientRule]:
    return list(s.scalars(select(ClientRule).where(ClientRule.client_id == client_id, ClientRule.status == "approved")
                          .order_by(ClientRule.category, ClientRule.title)).all())


def history(s, client_id: int, key: str) -> list[ClientRule]:
    return list(s.scalars(select(ClientRule).where(ClientRule.client_id == client_id, ClientRule.key == key).order_by(ClientRule.version.desc())).all())


def lookup(s, client_id: int | None, text: str, limit: int = 3) -> list[tuple[ClientRule, float]]:
    """Approved rules that plausibly answer an issue (keyword overlap). Deterministic, no model call."""
    if not client_id:
        return []
    words = set(keywords(text))
    if not words:
        return []
    scored = []
    for r in approved(s, client_id):
        kw = set(r.keywords or [])
        hit = words & kw
        if len(hit) >= 2:
            scored.append((r, round(len(hit) / max(len(words), 1), 3)))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def _recipe_text(rec: dict) -> str:
    from ..engine.event_report import campaign_rule_text
    cols = "; ".join(f"{c['header']} = " + ("always 0 (not tracked)" if c.get("constant_zero") else f"event '{c['event']}'") for c in rec["columns"])
    return (f"Apps: {', '.join(rec['platforms'])}. Exports: {', '.join(rec['sources'])} (AppsFlyer raw, CSV). "
            f"Campaigns: {campaign_rule_text(rec)}. Device id: {rec['id_field']}. "
            "De-duplicate on (device, event) keeping the first occurrence: per file, then per platform, then across platforms. "
            f"Count by Event Time date (exports are in Asia/Dubai). Columns: {cols}.")


def seed_from_code(s) -> int:
    """Seed approved rules that already exist as code, once per rule key (idempotent)."""
    from ..engine.event_report import BUILTIN_RECIPES, recipes_for
    n = 0
    for c in s.scalars(select(Client)).all():
        prof = s.scalar(select(Profile).where(Profile.client_id == c.id))
        items = []
        for rec in recipes_for(c.name, {}):
            if rec in BUILTIN_RECIPES:
                items.append((f"recipe-{rec['key']}", "metric", rec["name"], _recipe_text(rec), {"recipe_key": rec["key"]}))
        if items and not s.scalar(select(ClientRule.id).where(ClientRule.client_id == c.id, ClientRule.key == "fab-sheet-check-open")):
            items.append(("fab-sheet-check-open", "validation", "Google Sheet check for All events (open question)",
                          "The 'Internal Report - FAB' workbook has no All events table; its month tabs (e.g. 'Cards November 2026') track single "
                          "campaigns. Do not assume the Cards tab represents the whole All events report. Which tab(s) the All events report is "
                          "checked against is waiting for the reporting lead's decision.", {}))
        if prof and prof.settings:
            keep = {k: v for k, v in prof.settings.items() if k in ("name", "school_codes", "currency", "timezone", "clicks_definition",
                                                                     "conversion_priority", "naming", "brand_codes", "markets")}
            if keep:
                items.append(("client-profile", "profile", f"{c.name} reporting profile (from the client profile)",
                              "Settings the engine applies for this client:\n" + json.dumps(keep, indent=1, default=str)[:6000], {}))
        for key, cat, title, body, spec in items:
            if s.scalar(select(ClientRule.id).where(ClientRule.client_id == c.id, ClientRule.key == key)):
                continue
            s.add(ClientRule(client_id=c.id, key=key, version=1, category=cat, title=title, body=body, spec=spec,
                             keywords=keywords(title, body), status="approved" if key != "fab-sheet-check-open" else "proposed",
                             source="code", decided_at=now(), decision_note="Seeded from the code that runs today", validated=key != "fab-sheet-check-open"))
            n += 1
    if not s.get(AppSetting, "aisupport.seeded_v1"):
        s.add(AppSetting(key="aisupport.seeded_v1", value=now().isoformat()))
    return n
