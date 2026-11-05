"""FastAPI web application: pages + JSON API. Every task/file/artifact route performs a
server-side access check (owner or admin)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from . import service as S
from .auth import current_user, get_task_checked, require_admin, require_user
from .config import BASE_DIR, settings
from .db import (AppSetting, Artifact, AuditLog, Client, Job, Message, Profile, Question, Session, StageEvent, Task, TaskFile, TaskVersion,
                 User, init_db, now)
from .engine import fields as F
from .engine import fmt as FM
from .engine.calc import METRIC_LABELS
from .storage import sign_download, storage, verify_download
from . import zoho as Z

log = logging.getLogger("trafficomm.web")
app = FastAPI(title="Trafficomm Reporting Workspace", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="tc_session", max_age=12 * 3600,
                   same_site="lax", https_only=settings.cookie_secure)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

STAGES = [("uploaded", "Uploaded"), ("inspecting", "Inspecting files"), ("mapping", "Mapping data"),
          ("awaiting_clarification", "Awaiting clarification"), ("calculating", "Calculating"), ("generating", "Generating files"),
          ("validating", "Validating"), ("ready_for_review", "Ready for review")]
DELIVERABLE_LABELS = {"excel": "Consolidated Excel report", "deck_update": "Updated client PowerPoint", "deck_new": "New PowerPoint", "pdf": "PDF export"}


def _ago(t: dt.datetime | None) -> str:
    if not t:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    s = (now() - t).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return t.strftime("%d %b %Y")


templates.env.filters["ago"] = _ago
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "Asia/Kolkata")


def _local(t, fmt="%d %b %Y, %H:%M"):
    """Timestamps are stored in UTC; show them in the workspace display zone (IST by default)."""
    if not t:
        return ""
    from zoneinfo import ZoneInfo
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    z = ZoneInfo(DISPLAY_TZ)
    return t.astimezone(z).strftime(fmt) + (" IST" if DISPLAY_TZ == "Asia/Kolkata" else " " + t.astimezone(z).tzname())


templates.env.filters["local"] = _local
templates.env.filters["fmt_metric"] = lambda v, m, cur="": FM.fmt_metric(m, v, cur)
templates.env.filters["mb"] = lambda n: f"{(n or 0) / 1024:.0f} KB" if (n or 0) < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"
templates.env.globals.update(STATUS_LABEL=S.STATUS_LABEL, PLATFORM_LABELS=F.PLATFORM_LABELS, METRIC_LABELS=METRIC_LABELS,
                             DELIVERABLE_LABELS=DELIVERABLE_LABELS, ai_mocked=not settings.ai_enabled)


@app.on_event("startup")
def _startup():
    init_db()


@app.middleware("http")
async def csrf_and_headers(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method in ("POST", "PATCH", "DELETE", "PUT"):
        if request.headers.get("x-requested-with") != "fetch":
            return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
    return resp


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        from urllib.parse import quote
        had_session = bool(request.session.get("uid"))  # signed in before, but the session is no longer valid
        request.session.clear()
        return RedirectResponse("/login?next=" + quote(request.url.path, safe="/") + ("&error=session_expired" if had_session else ""), status_code=303)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/d/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"user": current_user(request), "code": exc.status_code, "detail": exc.detail,
                                                              "crumbs": [("Error", None)]}, status_code=exc.status_code)


@app.exception_handler(S.UserError)
async def user_exc(request: Request, exc: S.UserError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/healthz")
def healthz():
    with Session() as s:
        s.execute(select(1))
        q = s.scalar(select(func.count(Job.id)).where(Job.status == "queued"))
    return {"ok": True, "queued_jobs": q, "ai": "hybrid" if settings.ai_enabled else "mocked"}


# ---------------------------------------------------------------------------
# Auth: Zoho is the only sign-in method. There is no password endpoint; POST /login is not routed
# (405), so credentials sent to it can never create a session.
# ---------------------------------------------------------------------------
LOGIN_NOTICES = {
    # code -> (tone, title, message). Codes, never free text, travel in the URL.
    "cancelled": ("info", "Sign-in cancelled", "You cancelled signing in with Zoho. You can try again whenever you’re ready."),
    "failed": ("error", "Zoho sign-in didn’t complete", "We couldn’t finish signing you in with Zoho. Please try again in a moment."),
    "unauthorized": ("error", "This account can’t access the workspace", "Sign in with your @trafficomm.com Zoho account. If you already did, ask your workspace administrator to grant you access."),
    "no_access": ("error", "You don’t have access yet", "Your Zoho account is valid, but it hasn’t been granted access to this workspace. Ask your workspace administrator to add you."),
    "revoked": ("error", "Your access has been removed", "Your access to the workspace was revoked. Contact your workspace administrator if this is unexpected."),
    "expired": ("warn", "Your sign-in timed out", "The sign-in attempt expired before it finished. Please sign in again."),
    "session_expired": ("warn", "Your session has expired", "For your security you’ve been signed out. Sign in again to continue where you left off."),
    "signed_out": ("info", "You’re signed out", "You have signed out of the reporting workspace."),
    "not_configured": ("error", "Sign-in isn’t available", "Zoho sign-in hasn’t been configured for this workspace. Contact your workspace administrator."),
}


def _login_ctx(request: Request, next: str = "/", error: str = "") -> dict:
    code = error if error in LOGIN_NOTICES else ("failed" if error else "")
    if not settings.zoho_enabled:
        code = "not_configured"
    notice = None
    if code:
        tone, title, msg = LOGIN_NOTICES[code]
        notice = {"code": code, "tone": tone, "title": title, "message": msg, "email": request.session.pop("login_email", None)}
    return {"user": None, "next": _safe_next(next), "notice": notice, "zoho_enabled": settings.zoho_enabled,
            "domains": settings.zoho_allowed_domains}


def _safe_next(next: str | None) -> str:
    return next if next and next.startswith("/") and not next.startswith("//") and "\\" not in next else "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", _login_ctx(request, next, error))


@app.get("/auth/zoho")
def zoho_start(request: Request, next: str = "/"):
    if not settings.zoho_enabled:
        return RedirectResponse("/login?error=not_configured", status_code=303)
    request.session.clear()
    request.session["zoho_next"] = _safe_next(next)
    return RedirectResponse(Z.authorize_url(request.session, str(request.base_url)), status_code=303)


def _zoho_fail(request: Request, code: str, email: str | None = None) -> RedirectResponse:
    request.session.clear()
    if email:
        request.session["login_email"] = email  # shown once on the sign-in page, never put in the URL
    return RedirectResponse("/login?error=" + code, status_code=303)


@app.get("/auth/zoho/callback")
def zoho_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not settings.zoho_enabled:
        return RedirectResponse("/login?error=not_configured", status_code=303)
    expected, nonce = request.session.pop("zoho_state", None), request.session.pop("zoho_nonce", None)
    nxt = request.session.pop("zoho_next", "/")
    if error:
        return _zoho_fail(request, "cancelled" if error == "access_denied" else "failed")
    if not code or not expected or not secrets.compare_digest(state, expected):
        return _zoho_fail(request, "expired")
    try:
        server = Z.accounts_server(request.query_params.get("accounts-server"))
        who = Z.identity(Z.exchange_code(code, server, str(request.base_url)), nonce, server)
    except Z.ZohoError as e:
        log.info("zoho sign-in rejected: %s (%s)", type(e).__name__, e.code)
        return _zoho_fail(request, e.code)
    email, is_boot_admin = who["email"], who["email"] in settings.zoho_admin_emails
    with Session() as s:
        # Existing accounts are linked by their (unique, case-insensitive) email. Nobody is created
        # from a matching domain alone: only people an admin granted, or ZOHO_ADMIN_EMAILS.
        u = s.scalar(select(User).where(func.lower(User.email) == email))
        if u is None:
            if not is_boot_admin:
                S.audit(s, None, "login_denied", email)
                s.commit()
                return _zoho_fail(request, "no_access", email)
            u = User(email=email, name=who["name"], role="admin", password_hash="!")
            s.add(u)
            s.flush()
            S.audit(s, u.id, "create_user_sso", email)
        if not u.active:
            S.audit(s, u.id, "login_denied", email)
            s.commit()
            return _zoho_fail(request, "revoked", email)
        if is_boot_admin and u.role != "admin":
            u.role = "admin"
        if u.name in ("", u.email.split("@")[0]) and who["name"]:
            u.name = who["name"]
        request.session.clear()
        request.session["uid"] = u.id
        request.session["via"] = "zoho"
        S.audit(s, u.id, "login_zoho", u.email)
        s.commit()
    return RedirectResponse(_safe_next(nxt), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login?error=signed_out", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(request, "account.html", {"user": user, "nav": "account", "crumbs": [("Your account", None)]})


# ---------------------------------------------------------------------------
# Overview and task list
# ---------------------------------------------------------------------------
# Overview status cards -> existing task statuses (meanings unchanged).
STATUS_GROUPS = {
    "processing": sorted(S.PROCESSING),
    "needs_answer": ["awaiting_clarification"],
    "ready_for_review": ["ready_for_review"],
    "approved": ["approved"],
    "blocked": ["validation_blocked", "failed"],
}
ARTIFACT_SHORT = {"excel": "Excel", "excel_consolidated": "Excel", "deck_update": "PPTX", "deck_new": "New PPTX", "pdf": "PDF"}


def task_action(t: Task) -> dict:
    """The next step for a task, from its real status."""
    st = t.status
    if st == "draft":
        return {"label": "Continue", "href": f"/tasks/{t.id}/edit", "primary": False}
    if st == "awaiting_clarification":
        return {"label": "Answer questions", "href": f"/tasks/{t.id}#questions", "primary": True}
    if st == "validation_blocked":
        return {"label": "Review issue", "href": f"/tasks/{t.id}#validation", "primary": True}
    if st == "failed":
        return {"label": "Review issue", "href": f"/tasks/{t.id}#progress", "primary": True}
    if st == "ready_for_review":
        return {"label": "Review report", "href": f"/tasks/{t.id}#outputs", "primary": False}
    return {"label": "Open workspace", "href": f"/tasks/{t.id}", "primary": False}


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace("(", " ").replace(")", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else "")) if parts else "?").upper()


templates.env.filters["initials"] = _initials


def _task_list(request: Request, user: User, view: str, q: str, status: str, client: str, owner: str, mine: str):
    with Session() as s:
        scope = [Task.deleted_at.is_(None)]
        if user.role != "admin" or mine == "1":
            scope.append(Task.owner_id == user.id)
        stmt = select(Task).where(*scope)
        if user.role == "admin" and owner and owner.isdigit() and mine != "1":
            stmt = stmt.where(Task.owner_id == int(owner))
        if q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.join(Client, Client.id == Task.client_id).where(or_(Task.title.ilike(like), Task.brand.ilike(like), Client.name.ilike(like)))
        if status in STATUS_GROUPS:
            stmt = stmt.where(Task.status.in_(STATUS_GROUPS[status]))
        elif status in S.STATUS_LABEL:
            stmt = stmt.where(Task.status == status)
        if client.isdigit():
            stmt = stmt.where(Task.client_id == int(client))
        tasks = s.scalars(stmt.order_by(Task.updated_at.desc()).limit(200)).all()
        rows = []
        for t in tasks:
            latest_ready = s.scalar(select(TaskVersion).where(TaskVersion.task_id == t.id, TaskVersion.status.in_(["ready_for_review"]))
                                    .order_by(TaskVersion.number.desc()).limit(1))
            arts = []
            if latest_ready:
                arts = s.scalars(select(Artifact).where(Artifact.version_id == latest_ready.id, Artifact.kind.in_(list(ARTIFACT_SHORT)))).all()
            cur = s.get(TaskVersion, t.current_version_id) if t.current_version_id else None
            rows.append({"t": t, "client": t.client.name, "owner": t.owner.name, "latest_ready": latest_ready, "arts": arts, "version": cur,
                         "action": task_action(t), "deck": "deck_update" in (t.deliverables or [])})
        raw_counts = dict(s.execute(select(Task.status, func.count()).where(*scope).group_by(Task.status)).all())
        counts = {k: sum(raw_counts.get(x, 0) for x in v) for k, v in STATUS_GROUPS.items()}
        total = sum(raw_counts.values())
        # tasks that need someone to act: questions to answer, validation blocked, failed
        attn_tasks = s.scalars(select(Task).where(*scope, Task.status.in_(["awaiting_clarification", "validation_blocked", "failed"]))
                               .order_by(Task.updated_at.desc()).limit(50)).all()
        attention = None
        if attn_tasks:
            n_q = sum(1 for t in attn_tasks if t.status == "awaiting_clarification")
            n_b = sum(1 for t in attn_tasks if t.status == "validation_blocked")
            n_f = sum(1 for t in attn_tasks if t.status == "failed")
            parts = []
            if n_q:
                parts.append(f"{n_q} waiting for answers to questions")
            if n_b:
                parts.append(f"{n_b} blocked by validation checks")
            if n_f:
                parts.append(f"{n_f} failed during processing")
            if len(attn_tasks) == 1:
                a = task_action(attn_tasks[0])
                attention = {"count": 1, "detail": {"awaiting_clarification": "Answer the open questions so processing can continue.",
                                                    "validation_blocked": "Some validation checks failed. Review them before the report can be used.",
                                                    "failed": "Processing stopped with an error. Review it and retry."}[attn_tasks[0].status],
                             "title_task": attn_tasks[0].title or "Untitled task", "label": a["label"], "href": a["href"]}
            else:
                only = {t.status for t in attn_tasks}
                href = "/tasks?status=" + ("needs_answer" if only == {"awaiting_clarification"} else "blocked" if only <= {"validation_blocked", "failed"} else "attention")
                detail = {"awaiting_clarification": "Answer the open questions so processing can continue.",
                          "validation_blocked": "Validation checks failed. Review them before the reports can be used.",
                          "failed": "Processing stopped with an error. Review each task and retry."}[attn_tasks[0].status] if len(only) == 1 else (", ".join(parts).capitalize() + ".")
                attention = {"count": len(attn_tasks), "detail": detail, "label": "Review tasks", "href": href}
        if status == "attention":
            rows = [r for r in rows if r["t"].status in ("awaiting_clarification", "validation_blocked", "failed")]
        clients = s.scalars(select(Client).order_by(Client.name)).all()
        owners = s.scalars(select(User).order_by(User.name)).all() if user.role == "admin" else []
    filtered = bool(q.strip() or status or client or (owner and mine != "1"))
    ctx = {"user": user, "view": view, "rows": rows, "q": q, "status": status, "client": client, "owner": owner, "mine": mine,
           "clients": clients, "owners": owners, "counts": counts, "total": total, "attention": attention, "filtered": filtered,
           "STATUS_GROUPS": STATUS_GROUPS, "ARTIFACT_SHORT": ARTIFACT_SHORT,
           "nav": "overview" if view == "overview" else "tasks",
           "crumbs": [("Overview", None)] if view == "overview" else [("Reporting tasks", None)]}
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", status: str = "", client: str = "", owner: str = "", mine: str = ""):
    user = require_user(request)
    return _task_list(request, user, "overview", q, status, client, owner, mine)


@app.get("/tasks", response_class=HTMLResponse)
def task_list(request: Request, q: str = "", status: str = "", client: str = "", owner: str = "", mine: str = ""):
    user = require_user(request)
    return _task_list(request, user, "tasks", q, status, client, owner, mine)


# ---------------------------------------------------------------------------
# New task
# ---------------------------------------------------------------------------
@app.get("/tasks/new", response_class=HTMLResponse)
def new_task_page(request: Request):
    user = require_user(request)
    with Session() as s:
        clients = s.scalars(select(Client).order_by(Client.id)).all()
        profiles = s.scalars(select(Profile).order_by(Profile.id)).all()
    default_client = next((c for c in clients if c.name == "Around The Clock Media"), clients[0] if clients else None)
    today = dt.date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    return templates.TemplateResponse(request, "new_task.html", _builder_ctx(user, clients, profiles, default_client, None, [],
                                                                            last_prev.replace(day=1), last_prev))


CURRENCIES = ["AED", "USD", "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "EUR", "GBP", "INR"]


def _builder_ctx(user, clients, profiles, default_client, draft, files, p_start, p_end) -> dict:
    cur = (draft.currency if draft else "") or "AED"
    return {"user": user, "clients": clients, "profiles": profiles, "default_client": default_client, "draft": draft, "draft_files": files,
            "p_start": p_start, "p_end": p_end, "currencies": CURRENCIES if cur in CURRENCIES else [cur] + CURRENCIES, "currency": cur,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024), "nav": "tasks",
            "crumbs": [("Reporting tasks", "/tasks"), ("Edit draft" if draft else "New report", None)]}


def _date(v):
    if not v:
        return None
    return dt.date.fromisoformat(v) if isinstance(v, str) else v


@app.post("/api/tasks")
async def create_task(request: Request):
    user = require_user(request)
    body = await request.json()
    with Session() as s:
        client = s.get(Client, int(body.get("client_id") or 0)) or s.scalar(select(Client).order_by(Client.id))
        prof = s.scalar(select(Profile).where(Profile.client_id == client.id))
        t = Task(client_id=client.id, profile_id=prof.id if prof else None, owner_id=user.id, status="draft",
                 currency=(prof.settings.get("default_currency") if prof else None) or "AED")
        s.add(t)
        s.commit()
        return {"id": t.id}


@app.patch("/api/tasks/{task_id}")
async def update_task(request: Request, task_id: int):
    user = require_user(request)
    body = await request.json()
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        if t.status != "draft":
            raise S.UserError("Submitted tasks can't be edited - give feedback to create a revision")
        for k in ("title", "brand", "timezone", "instructions"):
            if k in body:
                setattr(t, k, str(body[k] or "")[:20000 if k == "instructions" else 300])
        if "currency" in body:
            cur = str(body["currency"] or "").upper()[:3]
            if cur and not cur.isalpha():
                raise S.UserError("Invalid currency code")
            t.currency = cur
        if "fx_usd_aed" in body:
            raw = str(body["fx_usd_aed"] or "").strip()
            if raw:
                try:
                    r = float(raw)
                except ValueError:
                    raise S.UserError("USD to AED rate must be a number, e.g. 3.6725")
                if not 1 <= r <= 10:
                    raise S.UserError("USD to AED rate looks wrong (expected about 3.67)")
                t.fx_usd_aed = r
            else:
                t.fx_usd_aed = None
        for k in ("period_start", "period_end", "comparison_start", "comparison_end"):
            if k in body:
                try:
                    setattr(t, k, _date(body[k]))
                except ValueError:
                    raise S.UserError(f"Invalid date for {k}")
        if t.period_start and t.period_end and t.period_start > t.period_end:
            raise S.UserError("Reporting period start is after its end")
        if "deliverables" in body:
            t.deliverables = [d for d in body["deliverables"] if d in DELIVERABLE_LABELS]
        if "client_id" in body:
            c = s.get(Client, int(body["client_id"]))
            if c:
                t.client_id = c.id
        if "profile_id" in body:
            t.profile_id = int(body["profile_id"]) if body["profile_id"] else None
        s.commit()
        return {"ok": True}


@app.post("/api/tasks/{task_id}/files")
async def upload_file(request: Request, task_id: int, file: UploadFile = File(...), role: str = Form(""), for_revision: str = Form("")):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        vno = 1
        if t.status != "draft":
            last = s.scalar(select(func.max(TaskVersion.number)).where(TaskVersion.task_id == t.id)) or 0
            vno = last + 1
    fd, tmp = tempfile.mkstemp(prefix="upl_", suffix=os.path.splitext(file.filename or "")[1].lower()[:6])
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                out.close()
                os.remove(tmp)
                raise S.UserError(f"'{file.filename}' exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit")
            out.write(chunk)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        f = S.add_file(s, t, user, tmp, file.filename or "upload", role or None, vno)
        s.commit()
        return S.file_summary(f)


@app.patch("/api/files/{file_id}")
async def update_file(request: Request, file_id: int):
    user = require_user(request)
    body = await request.json()
    with Session() as s:
        f = s.get(TaskFile, file_id)
        if not f:
            raise HTTPException(404, "File not found")
        t = get_task_checked(s, user, f.task_id)
        if "role" in body:
            role = body["role"]
            ext = os.path.splitext(f.original_name)[1].lower()
            if role not in S.ROLE_EXT or ext not in S.ROLE_EXT[role]:
                raise S.UserError(f"A {ext} file can't have the role '{role}'")
            f.role = role
        if "excluded" in body:
            f.excluded = bool(body["excluded"])
        if body.get("remove") and (t.status == "draft" or f.added_in_version > (s.scalar(select(func.max(TaskVersion.number)).where(TaskVersion.task_id == t.id)) or 0)):
            f.removed = True
        s.commit()
        return S.file_summary(f)


@app.post("/api/tasks/{task_id}/submit")
def submit(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        s.refresh(t, with_for_update=True)  # row lock: a double click / second tab can't queue a second job
        if t.status != "draft":
            raise S.UserError("This task was already submitted")
        files = s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False))).all()
        if "deck_update" in (t.deliverables or []) and not any(f.role == "client_deck" for f in files):
            raise S.UserError("You selected 'Updated client PowerPoint' but no client PPTX is uploaded. Upload the deck under Client PowerPoint, or deselect that output.")
        v = S.submit_task(s, t, user)
        s.commit()
        return {"ok": True, "version": v.number, "url": f"/tasks/{t.id}"}


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
def _state_token(s, t: Task) -> dict:
    v = s.get(TaskVersion, t.current_version_id) if t.current_version_id else None
    last_ev = s.scalar(select(func.max(StageEvent.id)).where(StageEvent.version_id == v.id)) if v else None
    last_msg = s.scalar(select(func.max(Message.id)).where(Message.task_id == t.id))
    return {"token": f"{t.status}:{v.id if v else 0}:{last_ev}:{last_msg}:{t.approved_version_id}", "status": t.status,
            "label": S.STATUS_LABEL.get(t.status, t.status), "processing": t.status in S.PROCESSING}


@app.get("/api/tasks/{task_id}/state")
def task_state(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        return _state_token(s, t)


# ---------------------------------------------------------------------------
# Task workspace
# ---------------------------------------------------------------------------
TRACK = [("received", "Files received", ("uploaded", "inspecting")), ("prepared", "Data prepared", ("mapping", "awaiting_clarification")),
         ("built", "Report built", ("calculating", "generating")), ("validation", "Validation", ("validating",)),
         ("review", "Review", ("ready_for_review",)), ("approved", "Approved", ())]
STAGE_ORDER = [k for k, _ in STAGES]
OUTPUT_KINDS = ("excel", "excel_consolidated", "deck_update", "deck_new", "pdf")
OUTPUT_TYPE = {"excel": "Excel workbook", "excel_consolidated": "Excel workbook", "deck_update": "PowerPoint deck", "deck_new": "PowerPoint deck", "pdf": "PDF"}
PREVIEW_FOR = {"excel": ("template", "excel"), "excel_consolidated": ("excel", "template"), "deck_update": ("deck_update",), "deck_new": ("deck_new",)}
PREVIEW_LABEL = {"deck_update": "Updated client deck", "deck_new": "New deck", "excel": "Excel report", "excel_consolidated": "Consolidated workbook", "template": "Populated template"}
CHECK_GROUP = {"excel": "workbook", "template": "workbook", "deck": "presentation", "crossfile": "presentation", "labels": "presentation",
               "deck_flags": "presentation", "deck_new": "presentation", "layout": "presentation"}
CHECK_NEXT = {
    "columns": "Check that the raw export includes the missing columns, then upload it with your feedback.",
    "coverage": "Make sure every uploaded file belongs in this report, or upload the missing source data with your feedback.",
    "period": "Check the reporting dates on the task and the dates in the exports.",
    "overlaps": "Check whether the same data was uploaded twice.",
    "currency": "Answer the currency question, or upload exports that state their currency.",
    "reconciliation": "Don't send this version. Review the technical detail and request a revision.",
    "rates": "Don't send this version. Review the technical detail and request a revision.",
    "excel": "Download the workbook and check the values listed in the detail.",
    "template": "Check the template cells listed in the detail.",
    "deck": "Open the updated deck and check the slides listed in the detail.",
    "crossfile": "Compare the workbook and the deck values listed in the detail before sharing.",
    "labels": "Check the date range and labels on the deck.",
    "deck_flags": "Review the flagged slides before sharing.",
    "layout": "Check the slides listed for text that overflows its box.",
    "commentary": "Review the commentary; untraceable statements were removed.",
}


def _check_group(c: dict) -> str:
    cid = c.get("id", "")
    if cid.startswith("open_"):
        t = (c.get("title") or "").lower()
        return "presentation" if any(w in t for w in ("deck", "powerpoint", "pptx", "slide")) else "workbook"
    return CHECK_GROUP.get(cid, "data")


def _track(v: TaskVersion | None, reached: set, t: Task) -> list[dict]:
    """Stage tracker for one version, from its real status, stage and events (never inferred from files)."""
    if not v:
        return [{"key": k, "label": lbl, "state": "pending", "note": ""} for k, lbl, _ in TRACK]
    V = (v.result or {}).get("validation") or {}
    idx = STAGE_ORDER.index(v.stage) if v.stage in STAGE_ORDER else 0
    out = []
    for k, label, stages in TRACK:
        st, note = "pending", ""
        pos = [STAGE_ORDER.index(x) for x in stages]
        here = v.stage in stages
        passed = bool(pos) and max(pos) < idx
        if k == "approved":
            if t.approved_version_id == v.id:
                st, note = "done", ("Approved " + _ago(v.approved_at)) if v.approved_at else "Approved"
        elif v.status in ("failed", "cancelled"):
            if here:
                st, note = ("failed", "Failed") if v.status == "failed" else ("attention", "Cancelled")
            elif passed:
                st = "done"
        elif v.status == "awaiting_clarification" and k == "prepared":
            st, note = "attention", "Needs your answer"
        elif k == "validation" and V:
            st = {"blocked": "attention", "warnings": "done", "passed": "done"}.get(V.get("overall"), "done")
            note = {"blocked": f"{V['counts'].get('fail', 0)} failed", "warnings": f"{V['counts'].get('warn', 0)} warning" + ("" if V["counts"].get("warn", 0) == 1 else "s"), "passed": "All passed"}.get(V.get("overall"), "")
            if v.status in S.PROCESSING and here:
                st, note = "active", "In progress"
        elif k == "review":
            if t.approved_version_id == v.id:
                st, note = "done", "Reviewed"
            elif v.status == "ready_for_review":
                st, note = "active", "Waiting for your review"
        elif here and v.status in S.PROCESSING:
            st, note = "active", "In progress"
        elif passed or (pos and any(x in reached for x in stages) and not here):
            st = "done"
        out.append({"key": k, "label": label, "state": st, "note": note})
    return out


def _attention(t: Task, v: TaskVersion | None, cur: TaskVersion | None, open_qs, job) -> dict | None:
    if open_qs and v and cur and v.id == cur.id:
        n = len(open_qs)
        return {"tone": "warn", "icon": "chat", "title": f"{n} question{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your answer",
                "text": "Processing is paused until you answer. Inspection and mapping are saved.", "action": "Answer question" + ("s" if n != 1 else ""), "href": "#questions"}
    if not v:
        return None
    V = (v.result or {}).get("validation") or {}
    cnt = V.get("counts") or {}
    if v.status == "failed":
        err = v.error or (job.error if job and job.version_id == v.id else "") or "No error detail was recorded."
        return {"tone": "error", "icon": "alert", "title": f"Processing failed for v{v.number}", "text": err[:400], "action": "View error", "tab": "diagnostics", "retry": cur is not None and v.id == cur.id}
    if v.status == "cancelled":
        return {"tone": "warn", "icon": "info", "title": f"Processing of v{v.number} was cancelled", "text": "Earlier versions are not affected. You can retry this version.", "retry": cur is not None and v.id == cur.id}
    if v.status == "validation_blocked" or V.get("overall") == "blocked":
        return {"tone": "error", "icon": "alert", "title": "Validation blocked this version",
                "text": f"{cnt.get('fail', 0)} check{'s' if cnt.get('fail', 0) != 1 else ''} failed. Outputs are kept for inspection but must not be sent to the client.", "action": "View checks", "tab": "validation"}
    if v.status == "ready_for_review" and t.approved_version_id != v.id:
        if cnt.get("warn"):
            return {"tone": "warn", "icon": "alert", "title": "Review validation before sharing",
                    "text": f"{cnt['warn']} warning{'s' if cnt['warn'] != 1 else ''} to review. Open the validation results to see issues and affected outputs.", "action": "View checks", "tab": "validation"}
        return {"tone": "info", "icon": "check-circle", "title": f"v{v.number} is ready for your review", "text": "All validation checks passed. Review the outputs, then approve or request changes.", "approve": v.id}
    if v.status in S.PROCESSING:
        return {"tone": "info", "icon": "clock", "busy": True, "title": f"Processing v{v.number}: {S.STATUS_LABEL.get(v.status, v.status)}",
                "text": "This page updates automatically. You can leave and come back."}
    return None


def _activity(s, t: Task, versions, users) -> list[dict]:
    """Every real event for the task, oldest first. actor: person or system."""
    vno = {v.id: v.number for v in versions}
    items = []
    for f in s.scalars(select(TaskFile).where(TaskFile.task_id == t.id).order_by(TaskFile.id)).all():
        items.append({"at": f.uploaded_at, "kind": "upload", "who": users.get(f.uploaded_by), "person": True, "v": f.added_in_version,
                      "text": f"Uploaded {f.original_name}" + (" (later removed)" if f.removed else "")})
    for v in versions:
        items.append({"at": v.created_at, "kind": "revision" if v.kind == "revision" else "submit", "who": users.get(v.created_by), "person": True, "v": v.number,
                      "text": (f"Requested revision v{v.number}" + (f" from v{vno.get(v.base_version_id)}" if v.base_version_id else "")) if v.kind == "revision" else "Submitted for processing"})
        for e in s.scalars(select(StageEvent).where(StageEvent.version_id == v.id).order_by(StageEvent.id)).all():
            items.append({"at": e.at, "kind": "validation" if e.stage == "validating" else "process", "who": "Workspace", "person": False, "v": v.number,
                          "text": dict(STAGES).get(e.stage, e.stage) + (f": {e.detail}" if e.detail else "")})
        if v.approved_at:
            items.append({"at": v.approved_at, "kind": "approval", "who": users.get(v.approved_by), "person": True, "v": v.number, "text": f"Approved v{v.number}"})
    for q in s.scalars(select(Question).where(Question.task_id == t.id, Question.answered_at.is_not(None))).all():
        items.append({"at": q.answered_at, "kind": "clarification", "who": users.get(q.answered_by), "person": True, "v": vno.get(q.version_id),
                      "text": f"Answered: {q.text[:140]} → {q.answer}"})
    for m in s.scalars(select(Message).where(Message.task_id == t.id, Message.kind.in_(["feedback", "assistant", "system"])).order_by(Message.id)).all():
        if m.kind == "feedback":
            items.append({"at": m.created_at, "kind": "feedback", "who": users.get(m.author_id), "person": True, "v": vno.get(m.version_id), "text": "Feedback: " + m.text[:300]})
        elif m.kind == "assistant":
            items.append({"at": m.created_at, "kind": "system", "who": "Workspace assistant", "person": False, "v": vno.get(m.version_id), "text": m.text[:300]})
    tz = dt.timezone.utc
    items.sort(key=lambda i: (i["at"].replace(tzinfo=tz) if i["at"] and i["at"].tzinfo is None else i["at"]) or now())
    return items


def workspace_ctx(s, user: User, t: Task, view_id: int | None = None) -> dict:
    versions = s.scalars(select(TaskVersion).where(TaskVersion.task_id == t.id).order_by(TaskVersion.number.desc())).all()
    cur = versions[0] if versions else None
    view = next((v for v in versions if v.id == view_id), None) or cur
    files = s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False)).order_by(TaskFile.id)).all()
    events = s.scalars(select(StageEvent).where(StageEvent.version_id == view.id).order_by(StageEvent.id)).all() if view else []
    questions = s.scalars(select(Question).where(Question.version_id == cur.id).order_by(Question.id)).all() if cur else []
    open_qs = [q for q in questions if q.status == "open"]
    msgs = s.scalars(select(Message).where(Message.task_id == t.id).order_by(Message.id)).all()
    users = {u.id: u.name for u in s.scalars(select(User)).all()}
    art_by_v = {v.id: s.scalars(select(Artifact).where(Artifact.version_id == v.id).order_by(Artifact.id)).all() for v in versions}
    job = s.scalar(select(Job).where(Job.version_id == cur.id).order_by(Job.id.desc())) if cur else None
    vjob = s.scalar(select(Job).where(Job.version_id == view.id).order_by(Job.id.desc())) if view else None
    R = (view.result or {}) if view else {}
    arts = art_by_v.get(view.id, []) if view else []
    previews: dict[str, list] = {}
    for a in arts:
        if a.kind == "preview":
            previews.setdefault(a.meta["artifact_kind"], []).append(a)
    for k in previews:
        previews[k].sort(key=lambda a: (a.meta or {}).get("page", 0))
    outputs = []
    for a in arts:
        if a.kind not in OUTPUT_KINDS:
            continue
        available = storage.path_ok(a.stored_path) and os.path.exists(a.stored_path)
        pkey = next((k for k in PREVIEW_FOR.get(a.kind, ()) if k in previews), None)
        outputs.append({"a": a, "type": OUTPUT_TYPE.get(a.kind, a.kind), "icon": "present" if a.kind.startswith("deck") else ("file" if a.kind == "pdf" else "table"),
                        "available": available, "preview": pkey, "ext": os.path.splitext(a.filename)[1].lstrip(".").upper()})
    records = [a for a in arts if a.kind == "record"]
    V = R.get("validation") or {}
    groups = {"data": [], "workbook": [], "presentation": []}
    for c in V.get("checks", []):
        groups[_check_group(c)].append(dict(c, next=CHECK_NEXT.get(c.get("id")) if c.get("status") in ("fail", "warn") else None))
    group_summary = {}
    for g, cs in groups.items():
        if cs:
            n = {st: sum(1 for c in cs if c["status"] == st) for st in ("pass", "warn", "fail", "not_run")}
            group_summary[g] = {"n": n, "total": len(cs), "state": "fail" if n["fail"] else "warn" if n["warn"] else "not_run" if n["not_run"] == len(cs) else "pass"}
    if view and view.approved_at and t.approved_version_id == view.id:
        review_state = ("ok", "Approved")
    elif view and (view.status == "validation_blocked" or V.get("overall") == "blocked"):
        review_state = ("bad", "Blocked: not for client use")
    elif view and view.status == "ready_for_review":
        review_state = ("warn", "Review required")
    else:
        review_state = ("neutral", S.STATUS_LABEL.get(view.status, view.status) if view else "")
    role_groups = [("raw", "Raw reports", "table"), ("template", "Excel templates", "table"), ("client_deck", "Client PowerPoint", "present"),
                   ("screenshots", "Screenshots and reference files", "image")]
    fsum = [S.file_summary(f) for f in files]
    files_by_role = {k: [f for f in fsum if (f["role"] == k or (k == "screenshots" and f["role"] == "creatives"))] for k, _, _ in role_groups}
    feedback_bases = [v for v in versions if v.result or v.status in ("ready_for_review", "approved", "validation_blocked")]
    can_feedback = bool(cur) and cur.status not in S.PROCESSING and t.status != "awaiting_clarification"
    return {"user": user, "t": t, "client": t.client.name, "owner": users.get(t.owner_id), "versions": versions, "cur": cur, "view": view,
            "files": fsum, "files_by_role": files_by_role, "role_groups": role_groups, "events": events,
            "track": _track(view, {e.stage for e in events}, t), "attention": _attention(t, view, cur, open_qs, vjob or job),
            "questions": questions, "open_questions": open_qs, "msgs": msgs, "users": users, "art_by_v": art_by_v, "job": job, "vjob": vjob,
            "R": R, "V": V, "groups": groups, "group_summary": group_summary, "outputs": outputs, "records": records, "previews": previews,
            "review_state": review_state, "activity": _activity(s, t, versions, users), "feedback_bases": feedback_bases, "can_feedback": can_feedback,
            "state": _state_token(s, t), "processing": t.status in S.PROCESSING, "profile_name": t.profile.name if t.profile else None,
            "PREVIEW_LABEL": PREVIEW_LABEL, "nav": "tasks", "crumbs": [("Reporting tasks", "/tasks"), (t.title or "Untitled task", None)]}


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def workspace(request: Request, task_id: int, v: int | None = None):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        if t.status == "draft":
            return RedirectResponse(f"/tasks/{t.id}/edit", status_code=303)
        ctx = workspace_ctx(s, user, t, v)
        return templates.TemplateResponse(request, "workspace.html", ctx)


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_draft(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        clients = s.scalars(select(Client).order_by(Client.id)).all()
        profiles = s.scalars(select(Profile).order_by(Profile.id)).all()
        files = [S.file_summary(f) for f in s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False))).all()]
        return templates.TemplateResponse(request, "new_task.html", _builder_ctx(user, clients, profiles, t.client, t, files, t.period_start, t.period_end))


@app.post("/api/tasks/{task_id}/answers")
async def answers(request: Request, task_id: int):
    user = require_user(request)
    body = await request.json()
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        S.answer_questions(s, t, user, body.get("answers") or {}, body.get("save") or {})
        s.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/feedback")
async def feedback(request: Request, task_id: int):
    user = require_user(request)
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        raise S.UserError("Describe what you would like to change")
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        s.refresh(t, with_for_update=True)  # row lock: a double click can't create two revisions
        fids = [int(x) for x in body.get("file_ids") or []]
        owned = {f.id for f in s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.id.in_(fids))).all()}
        v, summary = S.create_revision(s, t, user, text[:8000], int(body["base_version_id"]) if body.get("base_version_id") else None, [f for f in fids if f in owned])
        s.commit()
        return {"ok": bool(v), "version": v.number if v else None, "summary": summary}


@app.post("/api/tasks/{task_id}/approve")
async def approve(request: Request, task_id: int):
    user = require_user(request)
    body = await request.json()
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        S.approve(s, t, user, int(body["version_id"]))
        s.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
def cancel(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        S.cancel(s, t, user)
        s.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry")
def retry(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        v = s.get(TaskVersion, t.current_version_id)
        if v.status not in ("failed", "cancelled"):
            raise S.UserError("Only failed or cancelled versions can be retried")
        S.enqueue(s, v, f"retry-{now().timestamp()}")
        t.status = "queued"
        s.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/delete")
def delete(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        S.delete_task(s, t, user)
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Downloads (access check -> short-lived signed link) and previews
# ---------------------------------------------------------------------------
def _artifact_checked(s, user, artifact_id) -> Artifact:
    a = s.get(Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "Not found")
    get_task_checked(s, user, a.task_id)
    if not storage.path_ok(a.stored_path) or not os.path.exists(a.stored_path):
        raise HTTPException(410, "File no longer available (retention policy or deleted)")
    return a


@app.get("/artifacts/{artifact_id}/download")
def download(request: Request, artifact_id: int):
    user = require_user(request)
    with Session() as s:
        _artifact_checked(s, user, artifact_id)
    return RedirectResponse(f"/d/{sign_download(artifact_id, user.id)}", status_code=303)


@app.get("/d/{token}")
def signed_download(request: Request, token: str):
    data = verify_download(token)
    if not data:
        raise HTTPException(410, "Download link expired - go back to the task and click download again")
    user = require_user(request)
    if user.id != data["u"]:
        raise HTTPException(403, "This link belongs to another user")
    with Session() as s:
        a = _artifact_checked(s, user, data["a"])
        S.audit(s, user.id, "download", f"artifact {a.id}")
        s.commit()
        return FileResponse(a.stored_path, filename=a.filename, headers={"Cache-Control": "private, no-store"})


@app.get("/artifacts/{artifact_id}/preview")
def preview(request: Request, artifact_id: int):
    user = require_user(request)
    with Session() as s:
        a = _artifact_checked(s, user, artifact_id)
        if a.kind != "preview":
            raise HTTPException(400, "Not a preview")
        return FileResponse(a.stored_path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
ROLE_LABEL = {"admin": "Admin", "employee": "Employee"}
PROFILE_LOCKED = {"click_metric": ("link_clicks",)}  # agency rule: clicks are always link clicks


def _admin_users(s, me):
    users = s.scalars(select(User).order_by(User.active.desc(), func.lower(User.name))).all()
    last = dict(s.execute(select(AuditLog.user_id, func.max(AuditLog.at)).where(AuditLog.action == "login_zoho").group_by(AuditLog.user_id)).all())
    rows = []
    for u in users:
        st = S.sign_in_status(u, last.get(u.id), settings.zoho_allowed_domains)
        rows.append({"u": u, "me": u.id == me.id, "status": st, "last_login": last.get(u.id)})
    return rows


def _admin_ctx(s, user):
    rows = _admin_users(s, user)
    profiles = s.scalars(select(Profile).order_by(Profile.id)).all()
    clients = {c.id: c.name for c in s.scalars(select(Client)).all()}
    jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(20)).all()
    titles = dict(s.execute(select(Task.id, Task.title).where(Task.id.in_({j.task_id for j in jobs} or {0}))).all())
    active_admins = sum(1 for r in rows if r["u"].role == "admin" and r["u"].active)
    return {"user": user, "rows": rows, "profiles": profiles, "clients": clients, "jobs": jobs, "job_titles": titles,
            "retention": S.retention_days(s), "retention_min": S.RETENTION_MIN, "retention_max": S.RETENTION_MAX,
            "zoho_enabled": settings.zoho_enabled, "allowed_domains": settings.zoho_allowed_domains,
            "boot_admins": settings.zoho_admin_emails, "active_admins": active_admins, "ROLE_LABEL": ROLE_LABEL,
            "locked": PROFILE_LOCKED, "nav": "admin", "crumbs": [("Administration", None)]}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, partial: str = ""):
    user = require_admin(request)
    with Session() as s:
        ctx = _admin_ctx(s, user)
    if partial == "users":
        return templates.TemplateResponse(request, "partials/admin_users.html", ctx)
    return templates.TemplateResponse(request, "admin.html", ctx)


def _user_json(u):
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active}


@app.post("/api/admin/users")
async def api_admin_grant(request: Request):
    admin = require_admin(request)
    body = await request.json()
    with Session() as s:
        u, how = S.grant_access(s, admin, str(body.get("email") or ""), str(body.get("name") or ""), str(body.get("role") or "employee"))
        s.commit()
        return {"ok": True, "user": _user_json(u), "result": how,
                "message": ("Access restored for " if how == "restored" else "Access granted to ") + u.email + ". No email was sent."}


@app.patch("/api/admin/users/{uid}")
async def api_admin_update_user(request: Request, uid: int):
    admin = require_admin(request)
    body = await request.json()
    role = body.get("role")
    if role is not None and role not in ROLE_LABEL:
        raise S.UserError("Unknown role")
    active = body.get("active")
    if active is not None and not isinstance(active, bool):
        raise S.UserError("Access must be true or false")
    with Session() as s:
        u, changes = S.update_user_access(s, admin, uid, role, active)
        s.commit()
        return {"ok": True, "user": _user_json(u), "changes": changes,
                "message": (u.email + ": " + ", ".join(changes)) if changes else "No changes"}


@app.put("/api/admin/retention")
async def api_admin_retention(request: Request):
    admin = require_admin(request)
    body = await request.json()
    with Session() as s:
        d = S.set_retention_days(s, admin.id, body.get("days"))
        s.commit()
        p = S.retention_preview(s)
    return {"ok": True, "days": d, "message": f"Retention policy saved: {d} days. Nothing was deleted.", "eligible": p["count"]}


@app.get("/api/admin/retention/preview")
def api_admin_retention_preview(request: Request):
    require_admin(request)
    with Session() as s:
        p = S.retention_preview(s)
        d = S.retention_days(s)
    return {"days": d, "count": p["count"], "cutoff": p["cutoff"].isoformat(), "oldest": p["oldest"].isoformat() if p["oldest"] else None}


@app.post("/api/admin/retention/run")
async def api_admin_retention_run(request: Request):
    admin = require_admin(request)
    body = await request.json()
    if body.get("confirm") is not True:
        raise S.UserError("Confirm the cleanup before running it")
    with Session() as s:
        n = S.run_retention(s)
        S.audit(s, admin.id, "retention_run", f"{n} task(s) deleted")
        s.commit()
    return {"ok": True, "deleted": n, "message": f"Cleanup finished: {n} task{'s' if n != 1 else ''} deleted."}


def _validate_profile(old: dict, new) -> dict:
    import json as _json
    if not isinstance(new, dict):
        raise S.UserError("Rules must be an object of settings")
    if len(_json.dumps(new)) > 200_000:
        raise S.UserError("Rules are too large")
    for k in new:
        if not isinstance(k, str) or not k.strip():
            raise S.UserError("Every setting needs a name")
    for k, v in new.items():
        if k in old and old[k] is not None and v is not None:
            ot, nt = type(old[k]), type(v)
            if ot in (int, float) and nt in (int, float):
                continue
            if ot is not nt:
                raise S.UserError(f"“{k}” must stay a {dict(str='text', int='number', float='number', bool='yes/no', dict='group', list='list').get(ot.__name__, ot.__name__)}")
        if k in PROFILE_LOCKED and v not in PROFILE_LOCKED[k]:
            raise S.UserError(f"“{k}” is fixed by agency rules ({' / '.join(PROFILE_LOCKED[k])})")
    if "default_currency" in new and new["default_currency"] is not None:
        c = new["default_currency"]
        if not (isinstance(c, str) and len(c) == 3 and c.isalpha()):
            raise S.UserError("Default currency must be a 3-letter code such as AED")
        new["default_currency"] = c.upper()
    return new


@app.put("/api/admin/profiles/{pid}")
async def api_admin_profile(request: Request, pid: int):
    admin = require_admin(request)
    body = await request.json()
    with Session() as s:
        p = s.get(Profile, pid, with_for_update=True)
        if not p:
            raise HTTPException(404, "Not found")
        if body.get("updated_at") and p.updated_at and body["updated_at"] != p.updated_at.isoformat():
            raise S.UserError("These rules were changed by someone else. Reload to see the latest version before saving.")
        p.settings = _validate_profile(dict(p.settings or {}), body.get("settings"))
        p.updated_at = dt.datetime.now(dt.timezone.utc)
        S.audit(s, admin.id, "edit_profile", p.name)
        s.commit()
        return {"ok": True, "updated_at": p.updated_at.isoformat(), "message": f"Rules saved for {p.name}. They apply to new and revised reports."}


# ---------------------------------------------------------------------------
# Usage & costs (admin only; cost data never reaches non-admin browsers)
# ---------------------------------------------------------------------------
from . import usage as U  # noqa: E402

COST_TYPE_LABEL = {"tokens": "Tokens", "web_search": "Web search", "code_execution": "Code execution", "session_usage": "Session usage", "other": "Other"}
SOURCE_STATUS = {
    "ok": ("Synced", "ok"), "never": ("Not synced yet", "neutral"), "not_configured": ("Not connected", "neutral"),
    "error": ("Sync failed", "bad"), "auth_error": ("Key rejected", "bad"), "unsupported": ("Unsupported", "warn"), "manual": ("Entered by admin", "info"), "awaiting_entry": ("Awaiting entry", "warn"),
    "unmapped": ("Awaiting mapping", "warn"), "waiting": ("Backing off", "warn"),
}


def _month_label(m: str) -> str:
    try:
        return dt.date.fromisoformat(m + "-01").strftime("%b %Y")
    except (ValueError, TypeError):
        return m or ""


def _invoice_months() -> dict:
    from zoneinfo import ZoneInfo
    t = dt.datetime.now(ZoneInfo(DISPLAY_TZ)).date().replace(day=1)
    prev = (t - dt.timedelta(days=1)).replace(day=1)
    return {"this_month": t.strftime("%Y-%m"), "default_invoice_month": prev.strftime("%Y-%m")}


def _usage_ctx(s, user, rng, start, end, employee, client):
    r = U.resolve_range(s, rng, start, end, DISPLAY_TZ)
    emp_id = int(employee) if str(employee or "").isdigit() else None
    data = U.summary(s, r, emp_id, client or None)
    people = s.execute(select(U.AIRequest.user_id, func.max(U.AIRequest.user_name)).where(U.AIRequest.user_id.isnot(None)).group_by(U.AIRequest.user_id)).all()
    current = {u.id: u for u in s.scalars(select(User))}
    clients = [c for (c,) in s.execute(select(U.AIRequest.client_name).where(U.AIRequest.client_name.isnot(None)).distinct()).all()]
    return {"user": user, "r": r, "d": data, "people": sorted([(i, (current[i].name if i in current else n) or f"User {i}") for i, n in people], key=lambda x: x[1].lower()),
            "current_users": current, "clients": sorted(clients), "employee": emp_id, "client": client or "", "rng": r["key"],
            "COST_TYPE_LABEL": COST_TYPE_LABEL, "SOURCE_STATUS": SOURCE_STATUS, "to_inr": U.to_inr, "money": U.money,
            "month_label": _month_label, **_invoice_months(),
            "nav": "usage", "crumbs": [("Administration", "/admin"), ("Usage & costs", None)], "tz": DISPLAY_TZ}


@app.get("/admin/usage", response_class=HTMLResponse)
def usage_page(request: Request, range: str = "month", start: str = "", end: str = "", employee: str = "", client: str = "", partial: str = ""):
    user = require_admin(request)
    with Session() as s:
        ctx = _usage_ctx(s, user, range, start, end, employee, client)
        s.commit()  # tracking start is recorded on first view
    resp = templates.TemplateResponse(request, "partials/usage_activity.html" if partial == "activity" else "usage.html", ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/admin/usage/employees/{uid}", response_class=HTMLResponse)
def usage_employee(request: Request, uid: int, range: str = "month", start: str = "", end: str = ""):
    user = require_admin(request)
    with Session() as s:
        r = U.resolve_range(s, range, start, end, DISPLAY_TZ)
        det = U.employee_detail(s, uid, r)
        data = U.summary(s, r, uid)
        u = s.get(User, uid)
        snap = next(iter(det["requests"]), None)
        s.commit()
    if not u and not snap:
        raise HTTPException(404, "Not found")
    name = (u.name if u else snap.user_name) or f"User {uid}"
    resp = templates.TemplateResponse(request, "usage_employee.html", {
        "user": user, "r": r, "det": det, "d": data, "emp": data["employees"][0] if data["employees"] else None, "person": u, "name": name,
        "email": (u.email if u else snap.user_email), "uid": uid, "rng": r["key"], "SOURCE_STATUS": SOURCE_STATUS,
        "nav": "usage", "crumbs": [("Administration", "/admin"), ("Usage & costs", "/admin/usage"), (name, None)]})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/admin/usage/sync")
def usage_sync(request: Request):
    admin = require_admin(request)
    res = U.request_manual_sync()
    with Session() as s:
        S.audit(s, admin.id, "usage_sync", "manual" if res["started"] else "debounced")
        s.commit()
    return res


@app.put("/api/admin/usage/render-invoices")
async def usage_render_invoice_put(request: Request):
    admin = require_admin(request)
    body = await request.json()
    with Session() as s:
        try:
            inv = U.set_render_invoice(s, admin, str(body.get("month", "")), body.get("amount", ""), str(body.get("note", "")))
        except U.InvoiceError as e:
            raise S.UserError(str(e))
        S.audit(s, admin.id, "render_invoice_set", f"{inv.month} {inv.amount_usd}")
        s.commit()
        return {"month": inv.month, "amount": str(inv.amount_usd)}


@app.delete("/api/admin/usage/render-invoices/{month}")
def usage_render_invoice_delete(request: Request, month: str):
    admin = require_admin(request)
    with Session() as s:
        try:
            ok = U.delete_render_invoice(s, month)
        except U.InvoiceError as e:
            raise S.UserError(str(e))
        if ok:
            S.audit(s, admin.id, "render_invoice_delete", month)
        s.commit()
        return {"deleted": ok}


@app.get("/admin/usage/export.csv")
def usage_export(request: Request, range: str = "month", start: str = "", end: str = ""):
    require_admin(request)
    import csv
    import io
    with Session() as s:
        r = U.resolve_range(s, range, start, end, DISPLAY_TZ)
        rows = s.scalars(select(U.AIRequest).where(U.AIRequest.started_at >= r["start"], U.AIRequest.started_at < r["end"]).order_by(U.AIRequest.id)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["request_id", "started_at_utc", "completed_at_utc", "initiated_by", "employee_id", "employee", "task_id", "version", "job_id", "step",
                "model", "attempt", "first_attempt_id", "status", "provider_request_id", "credential_ref", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "web_search_requests"])
    for x in rows:
        w.writerow([x.id, x.started_at.isoformat(), x.completed_at.isoformat() if x.completed_at else "", x.initiated_by, x.user_id or "", x.user_name or "",
                    x.task_id or "", x.version_number or "", x.job_id or "", x.step, x.model or x.model_requested or "", x.attempt, x.first_attempt_id or "",
                    x.status, x.provider_request_id or "", x.credential_ref or "", x.input_tokens or 0, x.output_tokens or 0, x.cache_read_tokens or 0,
                    x.cache_creation_5m_tokens or 0, x.cache_creation_1h_tokens or 0, x.web_search_requests or 0])
    fn = f"app-ai-activity_{r['from']}_{r['to']}.csv"
    return HTMLResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-store"})


@app.get("/api/admin/usage/stream")
async def usage_stream(request: Request):
    """Server-sent events: one message per completed AI request (Postgres LISTEN). Carries no cost figures."""
    require_admin(request)
    import asyncio

    from fastapi.responses import StreamingResponse

    async def gen():
        yield "retry: 15000\n\n"
        conn = None
        try:
            import psycopg
            dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
            await conn.execute("LISTEN ai_usage")
            while not await request.is_disconnected():
                got = False
                async for n in conn.notifies(timeout=20, stop_after=1):
                    got = True
                    yield f"event: activity\ndata: {json.dumps({'request_id': int(n.payload)})}\n\n"
                if not got:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("usage stream failed")
            yield "event: unavailable\ndata: {}\n\n"
        finally:
            if conn is not None:
                await conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
