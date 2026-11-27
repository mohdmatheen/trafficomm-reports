"""AI Support pages and JSON API (mounted by app.web). Every route checks the signed-in user server-side."""
from __future__ import annotations

import json
import os
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import func, select

from ..auth import require_admin, require_user, visible_clients
from ..db import AIRequest, AuditLog, Client, Session, Task, User, UserClient, now
from . import controls, engineering as ENG, fab_pilot, knowledge, notify
from . import service as SV
from .models import (CATEGORIES, ISSUE_STATUSES, OPEN_STATUSES, RULE_CATEGORIES, ClientRule, EngineeringRun, Notification, SupportAttachment,
                     SupportEvent, SupportIssue, SupportMessage)

router = APIRouter()
_templates = None


def templates():
    global _templates
    if _templates is None:
        from ..web import templates as t
        _templates = t
    return _templates


def _err(e: Exception, code=400):
    raise HTTPException(status_code=404 if str(e) in ("Issue not found", "Task not found") else code, detail=str(e))


def _users(s, ids) -> dict:
    ids = {i for i in ids if i}
    return {u.id: u for u in s.scalars(select(User).where(User.id.in_(ids or {0}))).all()}


def _issue_or_404(s, user, issue_id) -> SupportIssue:
    try:
        return SV.get_checked(s, user, issue_id)
    except SV.SupportError as e:
        _err(e)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@router.get("/support", response_class=HTMLResponse)
def support_list(request: Request, status: str = "open", mine: str = ""):
    user = require_user(request)
    with Session() as s:
        q = SV.visible_issues_query(s, user)
        if status == "open":
            q = q.where(SupportIssue.status.in_(OPEN_STATUSES))
        elif status == "resolved":
            q = q.where(SupportIssue.status == "resolved")
        elif status == "waiting":
            q = q.where(SupportIssue.status == "awaiting_employee")
        if mine:
            q = q.where((SupportIssue.reporter_id == user.id) | (SupportIssue.assignee_id == user.id))
        issues = s.scalars(q.order_by(SupportIssue.updated_at.desc()).limit(200)).all()
        clients = {c.id: c.name for c in s.scalars(select(Client)).all()}
        users = _users(s, [i.reporter_id for i in issues])
        counts = {k: s.scalar(select(func.count()).select_from(SV.visible_issues_query(s, user).where(cond).subquery())) or 0
                  for k, cond in (("open", SupportIssue.status.in_(OPEN_STATUSES)), ("waiting", SupportIssue.status == "awaiting_employee"),
                                  ("resolved", SupportIssue.status == "resolved"))}
        return templates().TemplateResponse(request, "support_list.html", {
            "user": user, "nav": "support", "crumbs": [("AI Support", None)], "issues": issues, "clients": clients, "users": users,
            "status": status, "mine": mine, "counts": counts, "ISSUE_STATUSES": ISSUE_STATUSES, "CATEGORIES": CATEGORIES})


@router.get("/support/new", response_class=HTMLResponse)
def support_new(request: Request, task: int | None = None, v: int | None = None, pilot: str = ""):
    user = require_user(request)
    with Session() as s:
        t = None
        if task:
            from ..auth import get_task_checked
            t = get_task_checked(s, user, task)
        clients = visible_clients(s, user, include_archived=False)
        fab = [c for c in clients if fab_pilot.is_fab(c.name)]
        return templates().TemplateResponse(request, "support_new.html", {
            "user": user, "nav": "support", "crumbs": [("AI Support", "/support"), ("New issue", None)], "task": t, "version_id": v,
            "clients": clients, "fab_clients": fab, "pilot": pilot if pilot == fab_pilot.PILOT_KEY else "", "pilot_title": fab_pilot.TITLE})


@router.get("/support/{issue_id}", response_class=HTMLResponse)
def support_issue(request: Request, issue_id: int):
    user = require_user(request)
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        msgs = s.scalars(select(SupportMessage).where(SupportMessage.issue_id == i.id).order_by(SupportMessage.id)).all()
        atts = s.scalars(select(SupportAttachment).where(SupportAttachment.issue_id == i.id).order_by(SupportAttachment.id)).all()
        events = s.scalars(select(SupportEvent).where(SupportEvent.issue_id == i.id).order_by(SupportEvent.id.desc()).limit(40)).all()
        runs = s.scalars(select(EngineeringRun).where(EngineeringRun.issue_id == i.id).order_by(EngineeringRun.id.desc())).all()
        run = runs[0] if runs else None
        users = _users(s, [m.author_id for m in msgs] + [i.reporter_id, i.assignee_id] + [e.actor_id for e in events] + [r.approved_by for r in runs])
        client = s.get(Client, i.client_id) if i.client_id else None
        task = s.get(Task, i.task_id) if i.task_id else None
        from .agent import readiness
        ready, missing = readiness(s, i)
        can_approve_run, approve_why = (ENG.can_approve_run(s, user, run, i) if run else (False, ""))
        mark_read = s.scalars(select(Notification).where(Notification.user_id == user.id, Notification.issue_id == i.id, Notification.read_at.is_(None))).all()
        for n in mark_read:
            n.read_at = now()
        s.commit()
        ctx = {"user": user, "nav": "support", "crumbs": [("AI Support", "/support"), (i.ref, None)], "i": i, "msgs": msgs, "atts": atts,
               "events": events, "runs": runs, "run": run, "users": users, "client": client, "task": task, "ready": ready, "missing": missing,
               "can_approve_run": can_approve_run, "approve_why": approve_why, "ISSUE_STATUSES": ISSUE_STATUSES, "CATEGORIES": CATEGORIES,
               "eng": ENG.status_text(), "slots": fab_pilot.SLOTS, "questions": fab_pilot.QUESTIONS, "answers": fab_pilot.answers(i),
               "uploaded": fab_pilot.uploaded_slots(s, i) if i.pilot else {}, "token": _token(i, msgs, runs)}
        return templates().TemplateResponse(request, "support_issue.html", ctx)


def _token(i, msgs, runs) -> str:
    return f"{i.status}:{len(msgs)}:{runs[0].status if runs else ''}:{runs[0].pr_number if runs else ''}"


@router.get("/api/support/issues/{issue_id}/state")
def issue_state(request: Request, issue_id: int):
    user = require_user(request)
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        msgs = s.scalars(select(SupportMessage.id).where(SupportMessage.issue_id == i.id)).all()
        runs = s.scalars(select(EngineeringRun).where(EngineeringRun.issue_id == i.id).order_by(EngineeringRun.id.desc())).all()
        return {"token": _token(i, msgs, runs), "status": i.status}


@router.get("/support/attachments/{aid}/download")
def attachment_download(request: Request, aid: int):
    user = require_user(request)
    with Session() as s:
        a = s.get(SupportAttachment, aid)
        if not a:
            raise HTTPException(404, "Not found")
        _issue_or_404(s, user, a.issue_id)
        from ..storage import storage
        if not storage.path_ok(a.stored_path) or not os.path.exists(a.stored_path):
            raise HTTPException(404, "File no longer available")
        s.add(AuditLog(user_id=user.id, action="support_attachment_download", target=f"attachment {a.id}"))
        s.commit()
        return FileResponse(a.stored_path, filename=a.original_name)


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    user = require_user(request)
    with Session() as s:
        rows = s.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(100)).all()
        return templates().TemplateResponse(request, "notifications.html", {"user": user, "nav": "notifications",
                                                                            "crumbs": [("Notifications", None)], "rows": rows})


# ---------------------------------------------------------------------------
# issue API
# ---------------------------------------------------------------------------
@router.post("/api/support/issues")
async def api_create_issue(request: Request):
    user = require_user(request)
    d = await request.json()
    with Session() as s:
        pilot = d.get("pilot") if d.get("pilot") == fab_pilot.PILOT_KEY else None
        try:
            cid = int(d["client_id"]) if d.get("client_id") else None
            if pilot:
                c = s.get(Client, cid) if cid else None
                if not c or not fab_pilot.is_fab(c.name):
                    raise SV.SupportError("The FAB pilot needs the FAB client.")
            i, created = SV.create_issue(s, user, d.get("title") or (fab_pilot.TITLE if pilot else ""), d.get("text") or "",
                                         task_id=int(d["task_id"]) if d.get("task_id") else None,
                                         version_id=int(d["version_id"]) if d.get("version_id") else None, client_id=cid, pilot=pilot)
            if pilot and created:
                fab_pilot.save_answers(i, {"discrepancy_dates": next(q["default"] for q in fab_pilot.QUESTIONS if q["key"] == "discrepancy_dates")})
        except (SV.SupportError, ValueError) as e:
            _err(e)
        s.commit()
        return {"id": i.id, "ref": i.ref, "created": created, "url": f"/support/{i.id}"}


@router.post("/api/support/issues/{issue_id}/messages")
async def api_message(request: Request, issue_id: int):
    user = require_user(request)
    d = await request.json()
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        try:
            m = SV.post_message(s, i, user, d.get("text") or "")
        except SV.SupportError as e:
            _err(e)
        s.commit()
        return {"ok": True, "reply": m.text, "engine": m.engine, "status": i.status}


@router.post("/api/support/issues/{issue_id}/attachments")
async def api_attach(request: Request, issue_id: int, file: UploadFile = File(...), slot: str = Form("other")):
    user = require_user(request)
    with Session() as s:
        _issue_or_404(s, user, issue_id)
    fd, tmp = tempfile.mkstemp(prefix="sup_")
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > SV.MAX_ATTACH:
                out.close()
                os.remove(tmp)
                raise HTTPException(400, f"'{file.filename}' is larger than 50 MB.")
            out.write(chunk)
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        try:
            a = SV.add_attachment(s, i, user, tmp, file.filename or "upload", slot)
        except SV.SupportError as e:
            _err(e)
        s.commit()
        return {"id": a.id, "name": a.original_name, "slot": a.slot, "size": a.size}


@router.post("/api/support/issues/{issue_id}/pilot-answers")
async def api_pilot_answers(request: Request, issue_id: int):
    user = require_user(request)
    d = await request.json()
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        if i.pilot != fab_pilot.PILOT_KEY:
            raise HTTPException(400, "This issue has no pilot questions.")
        ans = fab_pilot.save_answers(i, d.get("answers") or {})
        lines = [f"{q['label']}\n→ {ans.get(q['key'])}" for q in fab_pilot.QUESTIONS if ans.get(q["key"])]
        s.add(SupportMessage(issue_id=i.id, author_id=user.id, kind="employee", text="Pilot answers updated:\n\n" + "\n\n".join(lines)))
        SV.event(s, i.id, user.id, "pilot_answers", ", ".join(k for k in ans if ans[k]))
        meta = dict(i.meta or {})
        meta["open_questions"] = [q for q in meta.get("open_questions") or [] if q not in {x["label"] for x in fab_pilot.QUESTIONS}]
        i.meta = meta
        if i.status == "awaiting_employee" and not meta["open_questions"]:
            SV.set_status(s, i, "investigating", user.id)
        s.commit()
        return {"ok": True, "missing": fab_pilot.pilot_missing(s, i)}


def _action(request, issue_id, fn):
    user = require_user(request)
    with Session() as s:
        i = _issue_or_404(s, user, issue_id)
        try:
            out = fn(s, i, user)
        except (SV.SupportError, ENG.EngineeringError) as e:
            _err(e)
        s.commit()
        return out or {"ok": True}


@router.post("/api/support/issues/{issue_id}/engineering")
async def api_engineering(request: Request, issue_id: int):
    d = await request.json() if (await request.body()) else {}
    return _action(request, issue_id, lambda s, i, u: {"run": ENG.request(s, i, u, force=bool(d.get("force"))).id})


@router.post("/api/support/issues/{issue_id}/resolve")
async def api_resolve(request: Request, issue_id: int):
    d = await request.json() if (await request.body()) else {}
    return _action(request, issue_id, lambda s, i, u: SV.resolve(s, i, u, d.get("note") or ""))


@router.post("/api/support/issues/{issue_id}/reopen")
async def api_reopen(request: Request, issue_id: int):
    d = await request.json() if (await request.body()) else {}
    return _action(request, issue_id, lambda s, i, u: SV.reopen(s, i, u, d.get("reason") or ""))


@router.post("/api/support/issues/{issue_id}/confirm")
async def api_confirm(request: Request, issue_id: int):
    d = await request.json()
    return _action(request, issue_id, lambda s, i, u: SV.confirm_corrected(s, i, u, bool(d.get("ok")), d.get("note") or ""))


def _run_action(request, rid, fn):
    user = require_user(request)
    with Session() as s:
        run = s.get(EngineeringRun, rid)
        if not run:
            raise HTTPException(404, "Not found")
        i = _issue_or_404(s, user, run.issue_id)
        try:
            out = fn(s, run, i, user)
        except (ENG.EngineeringError, SV.SupportError) as e:
            _err(e, 403 if "Only" in str(e) or "must approve" in str(e) else 400)
        s.commit()
        return out or {"ok": True}


@router.post("/api/support/runs/{rid}/approve")
async def api_run_approve(request: Request, rid: int):
    return _run_action(request, rid, lambda s, run, i, u: {"message": ENG.approve(s, run, u)})


@router.post("/api/support/runs/{rid}/rollback")
async def api_run_rollback(request: Request, rid: int):
    return _run_action(request, rid, lambda s, run, i, u: {"url": ENG.rollback(s, run, u)})


@router.post("/api/support/runs/{rid}/retry")
async def api_run_retry(request: Request, rid: int):
    def fn(s, run, i, u):
        if u.role != "admin":
            raise ENG.EngineeringError("Only an administrator can retry a failed run.")
        if run.status not in ("failed", "closed", "cancelled"):
            raise ENG.EngineeringError("Only a failed or closed run can be retried.")
        return {"run": ENG.request(s, i, u, force=True).id}
    return _run_action(request, rid, fn)


@router.post("/api/support/runs/{rid}/cancel")
async def api_run_cancel(request: Request, rid: int):
    def fn(s, run, i, u):
        if u.role != "admin":
            raise ENG.EngineeringError("Only an administrator can cancel a run.")
        if run.status not in ("queued", "dispatched", "pr_open", "checks_failed", "checks_passed", "awaiting_approval"):
            raise ENG.EngineeringError("This run can't be cancelled now.")
        run.status, run.finished_at = "cancelled", now()
        SV.set_status(s, i, "investigating", u.id, "engineering run cancelled")
        SV.event(s, i.id, u.id, "engineering_cancelled", f"run {run.id}")
    return _run_action(request, rid, fn)


@router.post("/api/notifications/read")
async def api_notifications_read(request: Request):
    user = require_user(request)
    d = await request.json() if (await request.body()) else {}
    with Session() as s:
        notify.mark_read(s, user.id, [int(x) for x in d.get("ids") or []] or None)
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# engineering callback (machine-to-machine, HMAC signed; no session)
# ---------------------------------------------------------------------------
@router.post("/api/engineering/callback")
async def api_engineering_callback(request: Request):
    body = await request.body()
    if not ENG.verify_signature(body, request.headers.get("x-trafficomm-timestamp", ""), request.headers.get("x-trafficomm-signature", "")):
        return JSONResponse({"detail": "Invalid signature"}, status_code=401)
    try:
        data = json.loads(body)
    except ValueError:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    with Session() as s:
        try:
            out = ENG.callback(s, data)
        except ENG.EngineeringError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        s.commit()
        return out


# ---------------------------------------------------------------------------
# Client Intelligence
# ---------------------------------------------------------------------------
@router.get("/intelligence", response_class=HTMLResponse)
def intelligence_index(request: Request):
    user = require_user(request)
    with Session() as s:
        clients = visible_clients(s, user)
        counts = {}
        for c in clients:
            counts[c.id] = {st: s.scalar(select(func.count(ClientRule.id)).where(ClientRule.client_id == c.id, ClientRule.status == st)) or 0
                            for st in ("approved", "proposed")}
        return templates().TemplateResponse(request, "intelligence.html", {"user": user, "nav": "intelligence", "crumbs": [("Client Intelligence", None)],
                                                                          "clients": clients, "counts": counts})


def _client_checked(s, user, cid) -> Client:
    c = s.get(Client, cid)
    allowed = SV.allowed_clients(s, user)
    if not c or (allowed is not None and cid not in allowed):
        raise HTTPException(404, "Client not found")
    return c


@router.get("/intelligence/{cid}", response_class=HTMLResponse)
def intelligence_client(request: Request, cid: int):
    user = require_user(request)
    with Session() as s:
        c = _client_checked(s, user, cid)
        rules = s.scalars(select(ClientRule).where(ClientRule.client_id == cid).order_by(ClientRule.category, ClientRule.key, ClientRule.version.desc())).all()
        current, proposed, history = {}, [], {}
        for r in rules:
            history.setdefault(r.key, []).append(r)
            if r.status == "approved":
                current[r.key] = r
            elif r.status == "proposed":
                proposed.append(r)
        by_cat: dict = {}
        for r in current.values():
            by_cat.setdefault(r.category, []).append(r)
        users = _users(s, [r.proposed_by for r in rules] + [r.decided_by for r in rules])
        return templates().TemplateResponse(request, "intelligence_client.html", {
            "user": user, "nav": "intelligence", "crumbs": [("Client Intelligence", "/intelligence"), (c.name, None)], "c": c,
            "by_cat": by_cat, "proposed": proposed, "history": history, "users": users, "RULE_CATEGORIES": RULE_CATEGORIES,
            "can_approve": knowledge.can_approve(s, user, cid)})


@router.post("/api/intelligence/{cid}/rules")
async def api_propose_rule(request: Request, cid: int):
    user = require_user(request)
    d = await request.json()
    with Session() as s:
        _client_checked(s, user, cid)
        try:
            r = knowledge.propose(s, user, cid, d.get("title") or "", d.get("body") or "", d.get("category") or "", key=d.get("key") or None)
        except knowledge.RuleError as e:
            _err(e)
        approvers = [u.id for u in notify.leads_for_client(s, cid)] or [u.id for u in notify.admins(s)]
        notify.push(s, [a for a in approvers if a != user.id], "approval_required", f"New client rule to review: {r.title}", link=f"/intelligence/{cid}")
        s.commit()
        return {"id": r.id, "version": r.version, "key": r.key}


@router.post("/api/intelligence/rules/{rid}/decide")
async def api_decide_rule(request: Request, rid: int):
    user = require_user(request)
    d = await request.json()
    with Session() as s:
        r = s.get(ClientRule, rid)
        if not r:
            raise HTTPException(404, "Not found")
        _client_checked(s, user, r.client_id)
        try:
            knowledge.decide(s, user, rid, bool(d.get("approve")), d.get("note") or "", d.get("validated"))
        except knowledge.RuleError as e:
            _err(e, 403 if ("Only" in str(e) or "second person" in str(e)) else 400)
        s.commit()
        return {"ok": True, "status": r.status}


# ---------------------------------------------------------------------------
# administrator control centre
# ---------------------------------------------------------------------------
@router.get("/admin/support", response_class=HTMLResponse)
def admin_support(request: Request):
    user = require_admin(request)
    with Session() as s:
        from .dashboard import control_centre
        ctx = control_centre(s)
        ctx.update({"user": user, "nav": "admin_support", "crumbs": [("Administration", "/admin"), ("AI Support control centre", None)],
                    "ISSUE_STATUSES": ISSUE_STATUSES, "CATEGORIES": CATEGORIES, "LABELS": controls.LABELS, "BOOL_KEYS": controls.BOOL_KEYS,
                    "eng": ENG.status_text(), "smtp": notify.smtp_configured()})
        return templates().TemplateResponse(request, "admin_support.html", ctx)


@router.put("/api/admin/support/settings")
async def api_admin_support_settings(request: Request):
    user = require_admin(request)
    d = await request.json()
    with Session() as s:
        changed = []
        try:
            for k, v in (d or {}).items():
                old = controls.get(s, k) if k in controls.DEFAULTS else None
                new = controls.set_value(s, k, v)
                if old != new:
                    changed.append(f"{k}={new}")
        except ValueError as e:
            _err(e)
        if changed:
            s.add(AuditLog(user_id=user.id, action="ai_support_settings", target=", ".join(changed)[:200]))
            if any(c.startswith("emergency_stop=1") for c in changed):
                SV.event(s, None, user.id, "emergency_stop", "Emergency stop switched ON")
        s.commit()
        return {"ok": True, "changed": changed}


@router.patch("/api/admin/support/clients/{cid}")
async def api_admin_support_client(request: Request, cid: int):
    user = require_admin(request)
    d = await request.json()
    with Session() as s:
        c = s.get(Client, cid)
        if not c:
            raise HTTPException(404, "Not found")
        if "ai_processing_allowed" in d:
            c.ai_processing_allowed = bool(d["ai_processing_allowed"])
            s.add(AuditLog(user_id=user.id, action="client_ai_processing", target=f"{c.name}: {'allowed' if c.ai_processing_allowed else 'not allowed'}"))
        s.commit()
        return {"ok": True, "ai_processing_allowed": c.ai_processing_allowed}


@router.patch("/api/admin/support/users/{uid}")
async def api_admin_support_user(request: Request, uid: int):
    user = require_admin(request)
    d = await request.json()
    with Session() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(404, "Not found")
        if "is_lead" in d:
            u.is_lead = bool(d["is_lead"])
            s.add(AuditLog(user_id=user.id, action="reporting_lead", target=f"{u.email}: {'lead' if u.is_lead else 'not lead'}"))
        if "monthly_limit" in d:
            try:
                val = controls.set_value(s, f"user_limit.{uid}", d["monthly_limit"])
            except (ValueError, TypeError):
                _err(ValueError("Monthly limit must be a whole number"))
            s.add(AuditLog(user_id=user.id, action="ai_user_limit", target=f"{u.email}: {val}"))
        s.commit()
        return {"ok": True}
