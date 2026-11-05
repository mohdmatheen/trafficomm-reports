"""FastAPI web application: pages + JSON API. Every task/file/artifact route performs a
server-side access check (owner or admin)."""
from __future__ import annotations

import datetime as dt
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
from .auth import check_password, current_user, get_task_checked, hash_password, require_admin, require_user
from .config import BASE_DIR, settings
from .db import (AppSetting, Artifact, Client, Job, Message, Profile, Question, Session, StageEvent, Task, TaskFile, TaskVersion,
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
        return RedirectResponse("/login?next=" + request.url.path, status_code=303)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/d/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"user": current_user(request), "code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)


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
# Auth
# ---------------------------------------------------------------------------
def _login_ctx(next: str = "/", error: str | None = None) -> dict:
    return {"user": None, "next": next, "error": error, "zoho_enabled": settings.zoho_enabled,
            "password_login": settings.password_login or not settings.zoho_enabled}


def _safe_next(next: str | None) -> str:
    return next if next and next.startswith("/") and not next.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", _login_ctx(next, error or None))


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if not _login_ctx()["password_login"]:
        return templates.TemplateResponse(request, "login.html", _login_ctx(next, "Password sign-in is turned off. Please sign in with Zoho."), status_code=403)
    with Session() as s:
        u = s.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        if not u or not u.active or not check_password(password, u.password_hash):
            return templates.TemplateResponse(request, "login.html", _login_ctx(next, "Invalid email or password"), status_code=401)
        request.session.clear()
        request.session["uid"] = u.id
        S.audit(s, u.id, "login", u.email)
        s.commit()
    return RedirectResponse(_safe_next(next), status_code=303)


@app.get("/auth/zoho")
def zoho_start(request: Request, next: str = "/"):
    if not settings.zoho_enabled:
        raise HTTPException(status_code=404, detail="Zoho sign-in isn't set up")
    request.session.clear()
    request.session["zoho_next"] = _safe_next(next)
    return RedirectResponse(Z.authorize_url(request.session, str(request.base_url)), status_code=303)


def _zoho_fail(msg: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse("/login?error=" + quote(msg), status_code=303)


@app.get("/auth/zoho/callback")
def zoho_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not settings.zoho_enabled:
        raise HTTPException(status_code=404, detail="Zoho sign-in isn't set up")
    expected, nonce = request.session.pop("zoho_state", None), request.session.pop("zoho_nonce", None)
    nxt = request.session.pop("zoho_next", "/")
    if error:
        return _zoho_fail("Zoho sign-in was cancelled.")
    if not code or not expected or not secrets.compare_digest(state, expected):
        return _zoho_fail("The sign-in session expired. Please try again.")
    try:
        server = Z.accounts_server(request.query_params.get("accounts-server"))
        who = Z.identity(Z.exchange_code(code, server, str(request.base_url)), nonce, server)
    except Z.ZohoError as e:
        log.info("zoho sign-in rejected: %s", type(e).__name__)
        return _zoho_fail(str(e))
    email, is_boot_admin = who["email"], who["email"] in settings.zoho_admin_emails
    with Session() as s:
        u = s.scalar(select(User).where(func.lower(User.email) == email))
        if u is None:
            if not (is_boot_admin or settings.zoho_auto_provision):
                S.audit(s, None, "login_denied", email)
                s.commit()
                return _zoho_fail("You don't have access yet. Ask your Trafficomm admin to grant access to " + email + ".")
            u = User(email=email, name=who["name"], role="admin" if is_boot_admin else "employee", password_hash="!")
            s.add(u)
            s.flush()
            S.audit(s, u.id, "create_user_sso", email)
        if not u.active:
            S.audit(s, u.id, "login_denied", email)
            s.commit()
            return _zoho_fail("Your access to the workspace has been revoked. Ask your Trafficomm admin.")
        if is_boot_admin and u.role != "admin":
            u.role = "admin"
        if u.name in ("", u.email.split("@")[0]) and who["name"]:
            u.name = who["name"]
        request.session.clear()
        request.session["uid"] = u.id
        S.audit(s, u.id, "login_zoho", u.email)
        s.commit()
    return RedirectResponse(_safe_next(nxt), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, msg: str = "", error: str = ""):
    user = require_user(request)
    return templates.TemplateResponse(request, "account.html", {"user": user, "msg": msg, "error": error})


@app.post("/account/password")
def account_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    user = require_user(request)
    with Session() as s:
        u = s.get(User, user.id)
        if not check_password(current, u.password_hash):
            return RedirectResponse("/account?error=Current password is incorrect", status_code=303)
        if len(new) < 10:
            return RedirectResponse("/account?error=New password must be at least 10 characters", status_code=303)
        if new != confirm:
            return RedirectResponse("/account?error=The new passwords don't match", status_code=303)
        u.password_hash = hash_password(new)
        S.audit(s, u.id, "change_password", u.email)
        s.commit()
    return RedirectResponse("/account?msg=Password changed", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", status: str = "", client: str = "", owner: str = ""):
    user = require_user(request)
    with Session() as s:
        stmt = select(Task).where(Task.deleted_at.is_(None))
        if user.role != "admin":
            stmt = stmt.where(Task.owner_id == user.id)
        elif owner:
            stmt = stmt.where(Task.owner_id == int(owner))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.join(Client, Client.id == Task.client_id).where(or_(Task.title.ilike(like), Task.brand.ilike(like), Client.name.ilike(like)))
        if status == "processing":
            stmt = stmt.where(Task.status.in_(list(S.PROCESSING)))
        elif status:
            stmt = stmt.where(Task.status == status)
        if client:
            stmt = stmt.where(Task.client_id == int(client))
        tasks = s.scalars(stmt.order_by(Task.updated_at.desc()).limit(200)).all()
        rows = []
        for t in tasks:
            latest_ready = s.scalar(select(TaskVersion).where(TaskVersion.task_id == t.id, TaskVersion.status.in_(["ready_for_review"]))
                                    .order_by(TaskVersion.number.desc()).limit(1))
            arts = []
            if latest_ready:
                arts = s.scalars(select(Artifact).where(Artifact.version_id == latest_ready.id, Artifact.kind.in_(["excel", "excel_consolidated", "deck_update", "deck_new", "pdf"]))).all()
            cur = s.get(TaskVersion, t.current_version_id) if t.current_version_id else None
            rows.append({"t": t, "client": t.client.name, "owner": t.owner.name, "latest_ready": latest_ready, "arts": arts, "version": cur})
        counts = dict(s.execute(select(Task.status, func.count()).where(Task.deleted_at.is_(None), *( [Task.owner_id == user.id] if user.role != "admin" else [])).group_by(Task.status)).all())
        clients = s.scalars(select(Client).order_by(Client.name)).all()
        owners = s.scalars(select(User).order_by(User.name)).all() if user.role == "admin" else []
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "rows": rows, "q": q, "status": status, "client": client, "owner": owner,
                                                                   "clients": clients, "owners": owners, "counts": counts})


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
    return templates.TemplateResponse(request, "new_task.html", {"user": user, "clients": clients, "profiles": profiles, "default_client": default_client,
                                                                  "p_start": last_prev.replace(day=1), "p_end": last_prev})


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
        files = s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False))).all()
        if "deck_update" in (t.deliverables or []) and not any(f.role == "client_deck" for f in files):
            raise S.UserError("You selected 'Updated client PowerPoint' but no client PPTX is uploaded. Upload the deck in area C, or untick that deliverable.")
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


def workspace_ctx(s, user: User, t: Task) -> dict:
    versions = s.scalars(select(TaskVersion).where(TaskVersion.task_id == t.id).order_by(TaskVersion.number.desc())).all()
    cur = versions[0] if versions else None
    files = s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False)).order_by(TaskFile.id)).all()
    events = s.scalars(select(StageEvent).where(StageEvent.version_id == cur.id).order_by(StageEvent.id)).all() if cur else []
    questions = s.scalars(select(Question).where(Question.version_id == cur.id).order_by(Question.id)).all() if cur else []
    msgs = s.scalars(select(Message).where(Message.task_id == t.id).order_by(Message.id)).all()
    users = {u.id: u.name for u in s.scalars(select(User)).all()}
    art_by_v = {}
    for v in versions:
        art_by_v[v.id] = s.scalars(select(Artifact).where(Artifact.version_id == v.id).order_by(Artifact.id)).all()
    job = s.scalar(select(Job).where(Job.version_id == cur.id).order_by(Job.id.desc())) if cur else None
    reached = {e.stage for e in events}
    stage_rows = []
    for key, label in STAGES:
        ev = next((e for e in reversed(events) if e.stage == key), None)
        state = "done" if ev else "todo"
        if cur and cur.stage == key and t.status in S.PROCESSING | {"awaiting_clarification"}:
            state = "active"
        if key == "awaiting_clarification" and not ev:
            state = "skipped" if cur and cur.stage in ("calculating", "generating", "validating", "ready_for_review") else "todo"
        stage_rows.append({"key": key, "label": label, "state": state, "at": ev.at if ev else None, "detail": ev.detail if ev else ""})
    result = (cur.result or {}) if cur else {}
    # if current version is waiting, show the last completed version's outputs too
    shown = cur
    if cur and not art_by_v.get(cur.id):
        shown = next((v for v in versions if art_by_v.get(v.id)), cur)
    shown_result = (shown.result or {}) if shown else {}
    arts = art_by_v.get(shown.id, []) if shown else []
    deliverables = [a for a in arts if a.kind not in ("preview", "record")]
    records = [a for a in arts if a.kind == "record"]
    previews = {}
    for a in arts:
        if a.kind == "preview":
            previews.setdefault(a.meta["artifact_kind"], []).append(a)
    return {"user": user, "t": t, "client": t.client.name, "owner": users.get(t.owner_id), "versions": versions, "cur": cur, "files": [S.file_summary(f) for f in files],
            "events": events, "stage_rows": stage_rows, "questions": questions, "open_questions": [q for q in questions if q.status == "open"],
            "msgs": msgs, "users": users, "art_by_v": art_by_v, "job": job, "result": result, "shown": shown, "shown_result": shown_result,
            "deliverables": deliverables, "records": records, "previews": previews, "state": _state_token(s, t),
            "processing": t.status in S.PROCESSING, "profile_name": t.profile.name if t.profile else None}


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def workspace(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        if t.status == "draft":
            return RedirectResponse(f"/tasks/{t.id}/edit", status_code=303)
        ctx = workspace_ctx(s, user, t)
        return templates.TemplateResponse(request, "workspace.html", ctx)


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_draft(request: Request, task_id: int):
    user = require_user(request)
    with Session() as s:
        t = get_task_checked(s, user, task_id)
        clients = s.scalars(select(Client).order_by(Client.id)).all()
        profiles = s.scalars(select(Profile).order_by(Profile.id)).all()
        files = [S.file_summary(f) for f in s.scalars(select(TaskFile).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False))).all()]
        return templates.TemplateResponse(request, "new_task.html", {"user": user, "clients": clients, "profiles": profiles, "default_client": t.client,
                                                                      "draft": t, "draft_files": files, "p_start": t.period_start, "p_end": t.period_end})


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
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, msg: str = ""):
    user = require_admin(request)
    with Session() as s:
        users = s.scalars(select(User).order_by(User.id)).all()
        profiles = s.scalars(select(Profile).order_by(Profile.id)).all()
        clients = {c.id: c.name for c in s.scalars(select(Client)).all()}
        jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(15)).all()
        rd = S.retention_days(s)
    import json as _json
    return templates.TemplateResponse(request, "admin.html", {"user": user, "users": users, "profiles": profiles, "clients": clients, "jobs": jobs,
                                                               "retention": rd, "msg": msg, "zoho_enabled": settings.zoho_enabled,
                                                               "allowed_domains": settings.zoho_allowed_domains, "boot_admins": settings.zoho_admin_emails, "dumps": lambda o: _json.dumps(o, indent=2)})


@app.post("/admin/users")
def admin_add_user(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form("employee"), password: str = Form("")):
    """Grant access. With Zoho sign-in, no password is needed: the person signs in with their Zoho account."""
    admin = require_admin(request)
    email = email.strip().lower()
    if "@" not in email:
        return RedirectResponse("/admin?msg=Enter a valid email", status_code=303)
    domain = email.rsplit("@", 1)[-1]
    if password and len(password) < 10:
        return RedirectResponse("/admin?msg=Password must be at least 10 characters", status_code=303)
    if not password:
        if not settings.zoho_enabled:
            return RedirectResponse("/admin?msg=Set an initial password (Zoho sign-in isn't set up yet)", status_code=303)
        if settings.zoho_allowed_domains and domain not in settings.zoho_allowed_domains:
            return RedirectResponse("/admin?msg=Only @" + " / @".join(settings.zoho_allowed_domains) + " emails can be granted Zoho access", status_code=303)
    with Session() as s:
        u = s.scalar(select(User).where(func.lower(User.email) == email))
        if u:
            if u.active:
                return RedirectResponse("/admin?msg=" + email + " already has access", status_code=303)
            u.active = True
            u.role = "admin" if role == "admin" else "employee"
            S.audit(s, admin.id, "grant_access", email)
            s.commit()
            return RedirectResponse("/admin?msg=Access restored for " + email, status_code=303)
        s.add(User(email=email, name=(name.strip() or email.split("@")[0])[:120], role="admin" if role == "admin" else "employee",
                   password_hash=hash_password(password) if password else "!"))
        S.audit(s, admin.id, "grant_access", email)
        s.commit()
    return RedirectResponse("/admin?msg=Access granted to " + email, status_code=303)


@app.post("/admin/users/{uid}/role")
def admin_set_role(request: Request, uid: int, role: str = Form(...)):
    admin = require_admin(request)
    with Session() as s:
        u = s.get(User, uid)
        if not u or u.id == admin.id:
            return RedirectResponse("/admin?msg=You can't change your own role", status_code=303)
        u.role = "admin" if role == "admin" else "employee"
        S.audit(s, admin.id, "set_role:" + u.role, u.email)
        s.commit()
    return RedirectResponse("/admin?msg=Role updated for " + u.email, status_code=303)


@app.post("/admin/users/{uid}/password")
def admin_set_password(request: Request, uid: int, password: str = Form(...)):
    admin = require_admin(request)
    if len(password) < 10:
        return RedirectResponse("/admin?msg=Password must be at least 10 characters", status_code=303)
    with Session() as s:
        u = s.get(User, uid)
        if not u:
            return RedirectResponse("/admin?msg=User not found", status_code=303)
        u.password_hash = hash_password(password)
        S.audit(s, admin.id, "reset_password", u.email)
        s.commit()
    return RedirectResponse("/admin?msg=Password updated", status_code=303)


@app.post("/admin/users/{uid}/toggle")
def admin_toggle_user(request: Request, uid: int):
    admin = require_admin(request)
    with Session() as s:
        u = s.get(User, uid)
        if u and u.id != admin.id:
            u.active = not u.active
            S.audit(s, admin.id, "grant_access" if u.active else "revoke_access", u.email)
            s.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/profiles/{pid}")
def admin_profile(request: Request, pid: int, settings_json: str = Form(...)):
    admin = require_admin(request)
    import json as _json
    try:
        data = _json.loads(settings_json)
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/admin?msg=Profile JSON is invalid", status_code=303)
    with Session() as s:
        p = s.get(Profile, pid)
        p.settings = data
        S.audit(s, admin.id, "edit_profile", p.name)
        s.commit()
    return RedirectResponse("/admin?msg=Profile saved", status_code=303)


@app.post("/admin/retention")
def admin_retention(request: Request, days: int = Form(...), run: str = Form("")):
    require_admin(request)
    with Session() as s:
        r = s.get(AppSetting, "retention_days") or AppSetting(key="retention_days", value=str(days))
        r.value = str(max(1, days))
        s.merge(r)
        n = S.run_retention(s) if run else None
        s.commit()
    return RedirectResponse(f"/admin?msg=Retention set to {days} days" + (f"; {n} task(s) deleted" if n is not None else ""), status_code=303)
