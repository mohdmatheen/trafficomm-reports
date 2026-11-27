"""Data for the AI Support administrator control centre."""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from sqlalchemy import func, select

from ..db import AIRequest, AppSetting, AuditLog, Client, User, now
from . import controls
from .models import OPEN_STATUSES, EngineeringRun, SupportEvent, SupportIssue, SupportMessage


def control_centre(s) -> dict:
    t = now()
    month = controls.month_start(t)
    d90 = t - dt.timedelta(days=90)
    issues = s.scalars(select(SupportIssue).order_by(SupportIssue.updated_at.desc()).limit(500)).all()
    clients = {c.id: c for c in s.scalars(select(Client).order_by(Client.name)).all()}
    users = {u.id: u for u in s.scalars(select(User).order_by(User.name)).all()}
    runs = s.scalars(select(EngineeringRun).order_by(EngineeringRun.id.desc()).limit(200)).all()
    issue_by_id = {i.id: i for i in issues}
    open_i = [i for i in issues if i.status in OPEN_STATUSES]
    resolved = [i for i in issues if i.status == "resolved" and i.resolved_at and i.resolved_at >= d90]
    hours = [(i.resolved_at - i.created_at).total_seconds() / 3600 for i in resolved if i.created_at]
    avg_h = round(sum(hours) / len(hours), 1) if hours else None
    active_runs = [r for r in runs if r.status in ("queued", "dispatched", "pr_open", "checks_failed", "checks_passed", "awaiting_approval", "approved", "merged", "deployed")
                   and not r.finished_at]
    failed_runs = [r for r in runs if r.status == "failed"]
    # recurring: same client + category three months, 2+ issues
    rec = Counter((i.client_id, i.category) for i in issues if i.created_at >= d90)
    recurring = [{"client": clients[c].name if c in clients else "No client", "category": cat, "count": n} for (c, cat), n in rec.most_common() if n >= 2][:12]
    per_client = defaultdict(lambda: {"open": 0, "resolved": 0, "total": 0})
    for i in issues:
        k = clients[i.client_id].name if i.client_id in clients else "No client"
        per_client[k]["total"] += 1
        per_client[k]["open" if i.status in OPEN_STATUSES else "resolved"] += 1
    # AI consumption this month
    support_spend = controls.ai_spend(s, month, controls.SUPPORT_STEPS)
    all_spend = controls.ai_spend(s, month)
    support_reqs = s.scalar(select(func.count(AIRequest.id)).where(AIRequest.started_at >= month, AIRequest.step.in_(controls.SUPPORT_STEPS))) or 0
    budget = float(controls.get(s, "monthly_ai_budget_usd"))
    # employee activity
    msg_counts = Counter(r for r in s.scalars(select(SupportMessage.author_id).where(SupportMessage.created_at >= month, SupportMessage.author_id.is_not(None))))
    rep_counts = Counter(i.reporter_id for i in issues if i.created_at >= month)
    req_counts = Counter(r for r in s.scalars(select(AIRequest.user_id).where(AIRequest.started_at >= month, AIRequest.step.in_(controls.SUPPORT_STEPS),
                                                                             AIRequest.attempt == 1)))
    activity = []
    for uid, u in users.items():
        if not u.active:
            continue
        lim_row = s.get(AppSetting, "aisupport.user_limit." + str(uid))
        activity.append({"u": u, "issues": rep_counts.get(uid, 0), "messages": msg_counts.get(uid, 0), "claude": req_counts.get(uid, 0),
                         "limit": controls.get_user_limit(s, uid), "custom_limit": bool(lim_row)})
    approvals = s.scalars(select(SupportEvent).where(SupportEvent.action.in_(("fix_approved", "rollback_opened", "emergency_stop"))).order_by(SupportEvent.id.desc()).limit(20)).all()
    rule_audit = s.scalars(select(AuditLog).where(AuditLog.action.in_(("rule_approve", "rule_reject", "engineering_approve", "engineering_rollback",
                                                                       "ai_support_settings", "client_ai_processing", "reporting_lead")))
                           .order_by(AuditLog.id.desc()).limit(30)).all()
    return {
        "stats": {"open": len(open_i), "waiting": sum(1 for i in open_i if i.status == "awaiting_employee"), "engineering": len(active_runs),
                  "validation": sum(1 for i in open_i if i.status == "awaiting_validation"), "failed": len(failed_runs),
                  "resolved90": len(resolved), "avg_hours": avg_h},
        "open_issues": open_i[:40], "waiting_issues": [i for i in open_i if i.status == "awaiting_employee"][:20],
        "validation_issues": [i for i in open_i if i.status == "awaiting_validation"][:20],
        "active_runs": active_runs[:30], "failed_runs": failed_runs[:20], "issue_by_id": issue_by_id, "recurring": recurring,
        "per_client": dict(sorted(per_client.items())), "clients": list(clients.values()), "users": users, "activity": activity,
        "spend": {"support": float(support_spend), "all": float(all_spend), "budget": budget, "support_requests": support_reqs,
                  "pct": min(100, round(float(all_spend) / budget * 100)) if budget else 0},
        "settings": controls.all_values(s), "approvals": approvals, "rule_audit": rule_audit,
    }
