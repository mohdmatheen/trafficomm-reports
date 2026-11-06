"""Business logic shared by the web app and the worker."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil

from sqlalchemy import select

from .config import settings
from .db import (AppSetting, Artifact, AuditLog, Client, Job, Message, Profile, Question, Session, StageEvent, Task, TaskFile,
                 TaskVersion, User, UserClient, now)
from .engine import ai as AI
from .engine.inspect import inspect_file
from .pipeline import Cancelled, VersionInput, run_version
from .storage import safe_name, sha256, storage

log = logging.getLogger("trafficomm.service")

ALLOWED_EXT = {".csv", ".xlsx", ".pptx", ".zip", ".png", ".jpg", ".jpeg"}
ROLE_EXT = {"raw": {".csv", ".xlsx"}, "template": {".xlsx"}, "client_deck": {".pptx"}, "screenshots": {".png", ".jpg", ".jpeg", ".zip"}, "creatives": {".zip"}}
STAGE_TO_STATUS = {"inspecting": "inspecting", "mapping": "mapping", "awaiting_clarification": "awaiting_clarification",
                   "calculating": "calculating", "generating": "generating", "validating": "validating",
                   "ready_for_review": "ready_for_review", "validation_blocked": "validation_blocked", "blocked": "validation_blocked"}
STATUS_LABEL = {"draft": "Draft", "queued": "Uploaded - queued", "inspecting": "Inspecting files", "mapping": "Mapping data",
                "awaiting_clarification": "Awaiting clarification", "calculating": "Calculating", "generating": "Generating files",
                "validating": "Validating", "ready_for_review": "Ready for review", "validation_blocked": "Validation blocked",
                "approved": "Approved by employee", "failed": "Failed", "cancelled": "Cancelled"}
PROCESSING = {"queued", "inspecting", "mapping", "calculating", "generating", "validating"}


class UserError(Exception):
    pass


def audit(s, user_id, action, target):
    s.add(AuditLog(user_id=user_id, action=action, target=str(target)[:200]))


def profile_settings(task: Task) -> dict:
    return dict(task.profile.settings) if task.profile else {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def add_file(s, task: Task, user: User, tmp_path: str, original_name: str, role: str | None, version_no: int) -> TaskFile:
    ext = os.path.splitext(original_name)[1].lower()
    size = os.path.getsize(tmp_path)
    if size > settings.max_upload_bytes:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' is {size / 1e6:.1f} MB; the limit is {settings.max_upload_bytes / 1e6:.0f} MB.")
    if size == 0:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' is empty.")
    if ext not in ALLOWED_EXT:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}': only CSV, XLSX, PPTX, PNG/JPG screenshots and ZIP files of screenshots are accepted. Macro-enabled files (.xlsm/.pptm) are rejected.")
    insp = inspect_file(tmp_path, original_name, profile_settings(task))
    if not insp.get("ok", True):
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' was rejected: {insp.get('error')}")
    role = role or insp.get("role_suggested") or "raw"
    if ext not in ROLE_EXT.get(role, set()):
        role = insp.get("role_suggested") or "raw"
    f = TaskFile(task_id=task.id, role=role, original_name=safe_name(original_name), stored_path="", size=size,
                 sha256=insp["sha256"], uploaded_by=user.id, added_in_version=version_no, inspection=_json_safe(insp))
    s.add(f)
    s.flush()
    f.stored_path = storage.save_upload(task.id, f.id, original_name, tmp_path)
    audit(s, user.id, "upload", f"task {task.id} file {f.id}")
    return f


def file_summary(f: TaskFile) -> dict:
    insp = f.inspection or {}
    sheets = []
    for sh in insp.get("sheets", []):
        sheets.append({"name": sh["name"], "rows": sh.get("row_count"), "platform": sh.get("platform_label") or sh.get("platform"),
                       "header_row": sh.get("header_row"), "data_sheet": sh.get("is_data_sheet"),
                       "currency": (sh.get("currency") or {}).get("value"), "dates": sh.get("dates", {}),
                       "summary_blocks": len(sh.get("summary_blocks", [])), "total_rows": len(sh.get("total_rows", [])),
                       "unresolved": sh.get("unresolved", []), "formula_cells": sh.get("formula_cells", 0)})
    deck = insp.get("deck")
    return {"id": f.id, "name": f.original_name, "size": f.size, "role": f.role, "sheets": sheets, "warnings": insp.get("warnings", []),
            "deck": deck and {"slides": deck["slide_count"], "summary": deck["summary"], "aspect": deck["aspect"], "unsupported": deck["unsupported_total"]},
            "images": len(insp.get("images", [])) if insp.get("images") else None, "added_in_version": f.added_in_version, "excluded": f.excluded}


def _json_safe(o):
    return json.loads(json.dumps(o, default=str))


# ---------------------------------------------------------------------------
# Versions / jobs
# ---------------------------------------------------------------------------
def enqueue(s, version: TaskVersion, reason: str) -> Job:
    key = f"v{version.id}:" + hashlib.sha256(json.dumps([version.answers, version.plan_overrides, version.file_ids, reason], sort_keys=True, default=str).encode()).hexdigest()[:24]
    existing = s.scalar(select(Job).where(Job.idempotency_key == key))
    if existing and existing.status in ("queued", "running", "succeeded"):
        return existing  # idempotent: same inputs never create a duplicate job/outputs
    if existing:
        existing.status, existing.attempts, existing.error, existing.run_after, existing.cancel_requested = "queued", 0, None, now(), False
        return existing
    j = Job(task_id=version.task_id, version_id=version.id, idempotency_key=key, max_attempts=settings.job_max_attempts)
    s.add(j)
    version.status = "queued"
    version.stage = "uploaded"
    s.add(StageEvent(version_id=version.id, stage="uploaded", detail=f"Queued ({reason})"))
    return j


def submit_task(s, task: Task, user: User) -> TaskVersion:
    if task.status not in ("draft",):
        raise UserError("Task was already submitted")
    if not task.title.strip():
        raise UserError("Task title is required")
    if not task.deliverables:
        raise UserError("Choose at least one deliverable")
    files = s.scalars(select(TaskFile).where(TaskFile.task_id == task.id, TaskFile.removed.is_(False))).all()
    if not any(f.role == "raw" for f in files):
        raise UserError("Upload at least one raw report (CSV/XLSX)")
    v = TaskVersion(task_id=task.id, number=1, kind="initial", file_ids=[f.id for f in files], created_by=user.id, answers={}, plan_overrides={})
    s.add(v)
    s.flush()
    task.current_version_id = v.id
    task.status = "queued"
    if task.instructions.strip():
        s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="instructions", text=task.instructions))
    enqueue(s, v, "submitted")
    audit(s, user.id, "submit", f"task {task.id}")
    return v


def answer_questions(s, task: Task, user: User, answers: dict, save_profile: dict) -> TaskVersion:
    v = s.get(TaskVersion, task.current_version_id)
    qs = {q.id: q for q in s.scalars(select(Question).where(Question.version_id == v.id, Question.status == "open")).all()}
    new_answers = dict(v.answers or {})
    texts = []
    for qid, val in answers.items():
        q = qs.get(int(qid))
        if not q or val in (None, ""):
            continue
        val = str(val).strip()[:300]
        if q.allow_free_text:
            try:
                if float(val) <= 0:
                    raise ValueError
            except ValueError:
                raise UserError(f"'{val}' is not a valid positive number for: {q.text[:80]}")
        elif q.options and val not in [o["value"] for o in q.options]:
            raise UserError("Invalid option selected")
        q.status, q.answer, q.answered_by, q.answered_at = "answered", val, user.id, now()
        new_answers[q.key] = val
        label = next((o["label"] for o in q.options if o["value"] == val), val)
        texts.append(f"Q: {q.text}\nA: {label}")
        if save_profile.get(str(qid)) and task.profile:
            _save_answer_to_profile(s, task, q, val)
            q.saved_to_profile = True
    if not texts:
        raise UserError("No answers given")
    v.answers = new_answers
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="answer", text="\n\n".join(texts)))
    if not [q for q in qs.values() if q.status == "open"] or True:
        enqueue(s, v, "answers")
        task.status = "queued"
    return v


def _save_answer_to_profile(s, task, q: Question, val: str):
    p = s.get(Profile, task.profile_id)
    st = dict(p.settings or {})
    if q.key == "conversion_metric":
        st["conversion_metric"] = None if val == "none" else val
    elif q.key.startswith("equiv:") and val == "yes":
        st["approved_equivalences"] = st.get("approved_equivalences", []) + [["snapchat", "swipe_ups", "link_clicks"]]
    elif q.key.startswith("clicks_def:"):
        st.setdefault("clicks_definitions", {})[q.key.split(":", 1)[1]] = val
    elif q.key.startswith("deckslide:"):
        m = re.search(r"Slide \d+ '(.*)' matches", q.text)
        title = m.group(1) if m else q.text.split("'")[1]
        st.setdefault("deck_slide_answers", {})[title + "|" + q.key.split(":")[1]] = val
    p.settings = st


def create_revision(s, task: Task, user: User, feedback: str, base_version_id: int | None, new_file_ids: list[int]) -> tuple[TaskVersion | None, str]:
    versions = s.scalars(select(TaskVersion).where(TaskVersion.task_id == task.id).order_by(TaskVersion.number)).all()
    if not versions:
        raise UserError("Task has no version yet")
    base = next((v for v in versions if v.id == base_version_id), None) if base_version_id else versions[-1]
    if not base:
        raise UserError("Unknown base version")
    if versions[-1].status in PROCESSING:
        raise UserError("A version is still processing - wait for it to finish or cancel it")
    provider = AI.get_provider()
    from . import usage
    provider.attribution = usage.Attribution.for_user(user, task_id=task.id, client_name=task.client.name if getattr(task, "client", None) else None)
    rp = provider.interpret_feedback(feedback, bool(new_file_ids))
    s.add(Message(task_id=task.id, version_id=None, author_id=user.id, kind="feedback", text=feedback + (f"\n[{len(new_file_ids)} new file(s) attached]" if new_file_ids else "")))
    supported = [o for o in rp.ops if o.op != "unsupported"]
    if not supported:
        msg = ("I couldn't map that to a change I can make safely. Supported changes include: switching clicks definition (link vs all clicks), "
               "choosing the CPA conversion, splitting tabs by country/language/platform, adding a breakdown, removing a slide or section, "
               "a shorter summary, removing commentary, or regenerating with newly uploaded files. Please rephrase or add detail.")
        s.add(Message(task_id=task.id, author_id=None, kind="assistant", text=msg))
        return None, msg
    overrides = json.loads(json.dumps(base.plan_overrides or {}))
    answers = dict(base.answers or {})
    for o in supported:
        if o.op == "set_click_metric":
            overrides["click_metric"] = o.value
        elif o.op == "set_conversion_metric":
            answers["conversion_metric"] = o.value
            overrides["conversion_metric"] = o.value
        elif o.op == "split_tabs_by":
            overrides["split_tabs_by"] = o.value
        elif o.op == "add_breakdown":
            overrides.setdefault("breakdowns", []).append([o.value])
        elif o.op == "remove_section":
            overrides.setdefault("remove_sections", []).append(o.value)
            if o.value == "recommendations":
                overrides["include_recommendations"] = False
        elif o.op == "remove_slide":
            overrides.setdefault("remove_slides", []).append(int(o.value))
        elif o.op == "short_summary":
            overrides["short_summary"] = True
        elif o.op == "include_commentary":
            overrides["include_commentary"] = o.value not in ("0", "false")
    base_files = [fid for fid in (base.file_ids or [])]
    file_ids = base_files + [fid for fid in new_file_ids if fid not in base_files]
    v = TaskVersion(task_id=task.id, number=versions[-1].number + 1, base_version_id=base.id, kind="revision", feedback=feedback,
                    revision_ops=[o.model_dump() for o in rp.ops], plan_overrides=overrides, answers=answers, file_ids=file_ids, created_by=user.id)
    s.add(v)
    s.flush()
    usage.link_version(getattr(provider, "request_ids", []), v.id, v.number)
    task.current_version_id = v.id
    task.status = "queued"
    task.approved_version_id = None if task.approved_version_id == base.id and False else task.approved_version_id
    s.add(Message(task_id=task.id, version_id=v.id, author_id=None, kind="assistant",
                  text=f"Creating v{v.number} from v{base.number}: {rp.summary}." + (" (" + provider.name + ")" if provider.mocked else
                       (" (read by " + ("Claude" if provider.steps and provider.steps[-1]["engine"] == "claude" else "built-in rules") + ")" if getattr(provider, "steps", None) else ""))))
    enqueue(s, v, "revision")
    audit(s, user.id, "revision", f"task {task.id} v{v.number}")
    return v, rp.summary


def approve(s, task: Task, user: User, version_id: int):
    v = s.get(TaskVersion, version_id)
    if not v or v.task_id != task.id:
        raise UserError("Unknown version")
    if v.status != "ready_for_review":
        raise UserError("Only a version that is ready for review can be approved")
    v.approved_at, v.approved_by = now(), user.id
    task.approved_version_id = v.id
    task.status = "approved"
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="system", text=f"v{v.number} approved by {user.name}. Files are not sent to the client automatically."))
    audit(s, user.id, "approve", f"task {task.id} v{v.number}")


def cancel(s, task: Task, user: User):
    v = s.get(TaskVersion, task.current_version_id)
    jobs = s.scalars(select(Job).where(Job.version_id == v.id, Job.status.in_(["queued", "running"]))).all()
    if not jobs:
        raise UserError("Nothing is processing")
    for j in jobs:
        if j.status == "queued":
            j.status, j.finished_at = "cancelled", now()
            v.status = "cancelled"
            task.status = "cancelled"
            s.add(StageEvent(version_id=v.id, stage="cancelled", detail="Cancelled before start"))
        else:
            j.cancel_requested = True
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="system", text="Cancellation requested."))


def delete_task(s, task: Task, user: User):
    task.deleted_at = now()
    storage.delete_task(task.id)
    audit(s, user.id, "delete", f"task {task.id}")


def retention_days(s) -> int:
    r = s.get(AppSetting, "retention_days")
    return int(r.value) if r else settings.retention_days_default


RETENTION_MIN, RETENTION_MAX = 7, 3650


def set_retention_days(s, admin_id, days) -> int:
    try:
        d = int(str(days).strip())
    except (TypeError, ValueError):
        raise UserError(f"Enter a whole number of days between {RETENTION_MIN} and {RETENTION_MAX}")
    if not RETENTION_MIN <= d <= RETENTION_MAX:
        raise UserError(f"Enter a whole number of days between {RETENTION_MIN} and {RETENTION_MAX}")
    r = s.get(AppSetting, "retention_days")
    old = r.value if r else str(settings.retention_days_default)
    if r:
        r.value = str(d)
    else:
        s.add(AppSetting(key="retention_days", value=str(d)))
    audit(s, admin_id, "set_retention", f"{old} -> {d} days")
    return d


def retention_preview(s) -> dict:
    """What a cleanup run would delete right now (same query as run_retention)."""
    cutoff = now() - dt.timedelta(days=retention_days(s))
    q = select(Task).where(Task.updated_at < cutoff, Task.deleted_at.is_(None))
    old = s.scalars(q.order_by(Task.updated_at)).all()
    return {"count": len(old), "cutoff": cutoff, "oldest": old[0].updated_at if old else None}


# ---------------------------------------------------------------------------
# Admin: access management (Zoho-only; no passwords)
# ---------------------------------------------------------------------------
def _lock_admin_changes(s):
    """Serialise role/access changes so two admins can't demote each other at the same time."""
    from sqlalchemy import text
    s.execute(text("SELECT pg_advisory_xact_lock(724001)"))


def grant_access(s, admin, email: str, name: str = "", role: str = "employee", client_ids=None):
    email = (email or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise UserError("Enter a valid company email address")
    domain = email.rsplit("@", 1)[-1]
    if settings.zoho_allowed_domains and domain not in settings.zoho_allowed_domains:
        raise UserError("Only @" + " / @".join(settings.zoho_allowed_domains) + " emails can be granted access")
    role = "admin" if role == "admin" else "employee"
    _lock_admin_changes(s)
    from sqlalchemy import func
    u = s.scalar(select(User).where(func.lower(User.email) == email))
    if u:
        if u.active:
            raise UserError(f"{email} already has access")
        u.active, u.role = True, role
        audit(s, admin.id, "grant_access", email)
        if client_ids is not None:
            set_user_clients(s, admin, u, client_ids)
        return u, "restored"
    u = User(email=email, name=(name.strip() or email.split("@")[0])[:120], role=role, password_hash="!")
    s.add(u)
    s.flush()
    audit(s, admin.id, "grant_access", email)
    if client_ids is not None:
        set_user_clients(s, admin, u, client_ids)
    return u, "granted"


# ---------------------------------------------------------------------------
# Clients and per-client access
# ---------------------------------------------------------------------------
def _ids(values) -> set[int]:
    if values is None:
        return set()
    if not isinstance(values, (list, tuple, set)):
        raise UserError("Clients must be a list")
    out = set()
    for v in values:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            raise UserError("Unknown client")
    return out


def set_user_clients(s, admin, u: User, client_ids) -> list[str]:
    """Replace the clients an employee can work on. Returns human-readable changes."""
    want = _ids(client_ids)
    names = dict(s.execute(select(Client.id, Client.name)).all())
    if want - set(names):
        raise UserError("Unknown client")
    have = set(s.scalars(select(UserClient.client_id).where(UserClient.user_id == u.id)).all())
    changes = []
    for cid in sorted(want - have, key=lambda c: names[c].lower()):
        s.add(UserClient(user_id=u.id, client_id=cid))
        audit(s, admin.id, "grant_client", f"{u.email}: {names[cid]}")
        changes.append("added " + names[cid])
    gone = have - want
    if gone:
        from sqlalchemy import delete
        s.execute(delete(UserClient).where(UserClient.user_id == u.id, UserClient.client_id.in_(gone)))
        for cid in sorted(gone, key=lambda c: names.get(c, "").lower()):
            audit(s, admin.id, "revoke_client", f"{u.email}: {names.get(cid, cid)}")
            changes.append("removed " + names.get(cid, str(cid)))
    return changes


def set_client_users(s, admin, c: Client, user_ids) -> list[str]:
    """Replace which employees can work on one client (admins always can and are ignored)."""
    want = _ids(user_ids)
    emps = {u.id: u for u in s.scalars(select(User).where(User.role != "admin"))}
    want &= set(emps)
    have = set(s.scalars(select(UserClient.user_id).where(UserClient.client_id == c.id)).all())
    changes = []
    for uid in want - have:
        s.add(UserClient(user_id=uid, client_id=c.id))
        audit(s, admin.id, "grant_client", f"{emps[uid].email}: {c.name}")
        changes.append("added " + emps[uid].name)
    gone = (have - want) & set(emps)
    if gone:
        from sqlalchemy import delete
        s.execute(delete(UserClient).where(UserClient.client_id == c.id, UserClient.user_id.in_(gone)))
        for uid in gone:
            audit(s, admin.id, "revoke_client", f"{emps[uid].email}: {c.name}")
            changes.append("removed " + emps[uid].name)
    return changes


def _client_fields(name, short) -> tuple[str, str]:
    name = " ".join(str(name or "").split())[:200]
    short = " ".join(str(short or "").split())[:30]
    if not name:
        raise UserError("Enter the client’s name")
    return name, short


def create_client(s, admin, name: str, short: str = "", user_ids=None) -> Client:
    from copy import deepcopy
    from sqlalchemy import func
    from .profiles import GENERIC_PROFILE
    name, short = _client_fields(name, short)
    if s.scalar(select(Client.id).where(func.lower(Client.name) == name.lower())):
        raise UserError(f"A client called “{name}” already exists")
    c = Client(name=name, short=short, archived=False)
    s.add(c)
    s.flush()
    # every client gets its own editable reporting rules, starting from the generic multi-platform rules
    rules = deepcopy(GENERIC_PROFILE)
    rules["name"] = f"{name} rules"
    s.add(Profile(client_id=c.id, name=f"{name} rules", settings=rules))
    audit(s, admin.id, "create_client", name)
    if user_ids:
        set_client_users(s, admin, c, user_ids)
    return c


def update_client(s, admin, cid: int, name=None, short=None, archived=None, user_ids=None) -> tuple[Client, list[str]]:
    from sqlalchemy import func
    c = s.get(Client, cid, with_for_update=True)
    if not c:
        raise UserError("Unknown client")
    changes = []
    if name is not None or short is not None:
        n, sh = _client_fields(c.name if name is None else name, c.short if short is None else short)
        if n != c.name:
            if s.scalar(select(Client.id).where(func.lower(Client.name) == n.lower(), Client.id != c.id)):
                raise UserError(f"A client called “{n}” already exists")
            audit(s, admin.id, "rename_client", f"{c.name} -> {n}")
            changes.append(f"renamed to {n}")
            c.name = n
        if sh != c.short:
            changes.append("short name updated")
            c.short = sh
    if archived is not None and bool(archived) != c.archived:
        c.archived = bool(archived)
        audit(s, admin.id, "archive_client" if c.archived else "restore_client", c.name)
        changes.append("archived" if c.archived else "restored")
    if user_ids is not None:
        changes += set_client_users(s, admin, c, user_ids)
    return c, changes


def delete_user(s, admin, uid: int) -> tuple[str, int]:
    """Delete an account. Their tasks move to the admin who deletes them, so no report is lost."""
    from sqlalchemy import delete
    from .db import reassign_user_records
    _lock_admin_changes(s)
    u = s.get(User, uid, with_for_update=True)
    if not u:
        raise UserError("Unknown user")
    if u.id == admin.id:
        raise UserError("You can't delete your own account. Ask another admin.")
    if u.role == "admin" and u.active:
        others = s.scalars(select(User.id).where(User.role == "admin", User.active.is_(True), User.id != u.id).with_for_update()).all()
        if not others:
            raise UserError("The workspace must keep at least one active admin")
    moved = reassign_user_records(s, u.id, admin.id)
    s.execute(delete(UserClient).where(UserClient.user_id == u.id))
    email = u.email
    s.delete(u)
    audit(s, admin.id, "delete_user", f"{email} ({moved} task(s) moved to {admin.email})")
    return email, moved


def update_user_access(s, admin, uid: int, role: str | None = None, active: bool | None = None):
    _lock_admin_changes(s)
    u = s.get(User, uid, with_for_update=True)
    if not u:
        raise UserError("Unknown user")
    if u.id == admin.id:
        raise UserError("You can't change your own role or access. Ask another admin.")
    new_role = u.role if role is None else ("admin" if role == "admin" else "employee")
    new_active = u.active if active is None else bool(active)
    losing_admin = u.role == "admin" and u.active and not (new_role == "admin" and new_active)
    if losing_admin:
        others = s.scalars(select(User.id).where(User.role == "admin", User.active.is_(True), User.id != u.id).with_for_update()).all()
        if not others:
            raise UserError("The workspace must keep at least one active admin")
    changes = []
    if new_role != u.role:
        changes.append(f"role {u.role} -> {new_role}")
        audit(s, admin.id, "set_role:" + new_role, u.email)
    if new_active != u.active:
        changes.append("access restored" if new_active else "access revoked")
        audit(s, admin.id, "grant_access" if new_active else "revoke_access", u.email)
    u.role, u.active = new_role, new_active
    return u, changes


def sign_in_status(u, last_login, allowed_domains) -> dict:
    """Only claims Zoho when the audit log records a completed Zoho sign-in for this account."""
    domain = u.email.rsplit("@", 1)[-1].lower()
    if allowed_domains and domain not in allowed_domains:
        return {"key": "legacy", "label": "Legacy account", "tone": "warn",
                "detail": f"@{domain} can't sign in with Zoho. Grant the person's company email instead."}
    if not u.active:
        return {"key": "revoked", "label": "Can't sign in", "tone": "neutral", "detail": "Access revoked"}
    if last_login:
        return {"key": "zoho", "label": "Signed in with Zoho", "tone": "ok", "detail": "Last sign-in", "at": last_login}
    return {"key": "eligible", "label": "Eligible", "tone": "info", "detail": "Hasn't signed in yet"}


def run_retention(s) -> int:
    cutoff = now() - dt.timedelta(days=retention_days(s))
    old = s.scalars(select(Task).where(Task.updated_at < cutoff, Task.deleted_at.is_(None))).all()
    for t in old:
        t.deleted_at = now()
        storage.delete_task(t.id)
        audit(s, None, "retention_delete", f"task {t.id}")
    return len(old)


# ---------------------------------------------------------------------------
# Worker-side processing
# ---------------------------------------------------------------------------
def process_job(job_id: int, worker_id: str = "worker"):
    """Runs inside an isolated subprocess with resource limits (see jobs.py)."""
    with Session() as s:
        job = s.get(Job, job_id)
        v = s.get(TaskVersion, job.version_id)
        task = s.get(Task, v.task_id)
        if task.deleted_at:
            return {"status": "cancelled"}
        prof = profile_settings(task)
        files = s.scalars(select(TaskFile).where(TaskFile.id.in_(v.file_ids or []), TaskFile.removed.is_(False))).all()
        base_artifacts = {}
        if v.base_version_id:
            for a in s.scalars(select(Artifact).where(Artifact.version_id == v.base_version_id)).all():
                base_artifacts[a.kind] = {"path": a.stored_path, "fingerprint": a.fingerprint, "id": a.id}
        answers = dict(v.answers or {})
        for k, val in (prof.get("deck_slide_answers") or {}).items():
            title, idx = k.rsplit("|", 1)
            answers.setdefault(f"deckslide:{idx}", val)
        tdict = {"title": task.title, "client": task.client.name, "brand": task.brand, "period_start": task.period_start, "period_end": task.period_end,
                 "comparison_start": task.comparison_start, "comparison_end": task.comparison_end, "timezone": task.timezone,
                 "currency": task.currency, "fx_usd_aed": task.fx_usd_aed, "deliverables": task.deliverables, "instructions": task.instructions}
        finputs = [{"id": f.id, "name": f.original_name, "path": f.stored_path, "ext": os.path.splitext(f.original_name)[1].lower(),
                    "role": f.role, "inspection": f.inspection, "uploaded_order": f.id, "excluded": f.excluded} for f in files]
        workdir = os.path.join(storage.version_dir(task.id, v.number), "work")
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)  # retry of the same version replaces partial outputs (no duplicates)
        inp = VersionInput(task=tdict, files=finputs, profile=prof, answers=answers, plan_overrides=v.plan_overrides or {},
                           workdir=workdir, version_number=v.number, base_artifacts=base_artifacts, feedback=v.feedback)
        vid, tid = v.id, task.id

    def stage(name, detail):
        with Session() as s2:
            vv = s2.get(TaskVersion, vid)
            tt = s2.get(Task, tid)
            vv.stage = name
            vv.status = STAGE_TO_STATUS.get(name, name)
            tt.status = vv.status
            s2.add(StageEvent(version_id=vid, stage=name, detail=str(detail)[:300]))
            jj = s2.get(Job, job_id)
            jj.heartbeat_at = now()
            s2.commit()

    def check_cancel():
        with Session() as s2:
            if s2.get(Job, job_id).cancel_requested:
                raise Cancelled()

    provider = AI.get_provider()
    from . import usage
    with Session() as s3:
        vv, tt = s3.get(TaskVersion, vid), s3.get(Task, tid)
        creator = s3.get(User, vv.created_by) if vv.created_by else None
        attr = dict(task_id=tid, version_id=vid, version_number=vv.number, job_id=job_id, client_name=tt.client.name if tt.client else None)
        provider.attribution = usage.Attribution.for_user(creator, **attr) if creator else usage.Attribution(initiated_by="system", **attr)
    result = run_version(inp, stage, check_cancel, provider=provider)
    persist_result(job_id, vid, tid, result)
    return {"status": result["status"]}


def persist_result(job_id: int, vid: int, tid: int, result: dict):
    with Session() as s:
        v = s.get(TaskVersion, vid)
        task = s.get(Task, tid)
        # replace artifacts of this version (idempotent on retry)
        for a in s.scalars(select(Artifact).where(Artifact.version_id == vid)).all():
            s.delete(a)
        for a in result.get("artifacts", []):
            s.add(Artifact(version_id=vid, task_id=tid, kind=a["kind"], label=a["label"], filename=os.path.basename(a["path"]),
                           stored_path=a["path"], size=os.path.getsize(a["path"]), sha256=sha256(a["path"]),
                           fingerprint=a.get("fingerprint") or "", reused_from_id=a.get("reused_from")))
        for p in result.get("previews", []):
            s.add(Artifact(version_id=vid, task_id=tid, kind="preview", label=f"{p['artifact_kind']} page {p['page']}", filename=os.path.basename(p["path"]),
                           stored_path=p["path"], size=os.path.getsize(p["path"]), meta={"artifact_kind": p["artifact_kind"], "page": p["page"]}))
        # questions
        existing = {q.key: q for q in s.scalars(select(Question).where(Question.version_id == vid)).all()}
        for q in result.get("questions", []):
            if q["key"] in (v.answers or {}):
                continue
            if q["key"] in existing and existing[q["key"]].status == "open":
                continue
            s.add(Question(task_id=tid, version_id=vid, key=q["key"], text=q["text"], options=q.get("options") or [], default=q.get("default"),
                           allow_free_text=bool(q.get("allow_free_text")), context=q.get("context", "")))
        slim = {k: val for k, val in result.items() if k not in ("artifacts", "previews")}
        v.result = _json_safe(slim)
        v.status = result["status"]
        task.status = result["status"]
        v.error = result.get("error")
        st_info = result.get("structure") or {}
        if (result["status"] == "ready_for_review" and st_info.get("fingerprint") and not st_info.get("known") and task.profile_id
                and not result.get("ai_mocked")):  # only reports Claude has handled count as known structures
            prof = s.get(Profile, task.profile_id)
            stt = dict(prof.settings or {})
            ks = [k for k in (stt.get("known_structures") or []) if k != st_info["fingerprint"]] + [st_info["fingerprint"]]
            stt["known_structures"] = ks[-50:]
            prof.settings = stt
        if result["status"] in ("ready_for_review", "validation_blocked"):
            v.completed_at = now()
            if v.base_version_id:
                base = s.get(TaskVersion, v.base_version_id)
                v.change_summary = _json_safe(change_summary(base, v, s))
            n_ok = result["validation"]["counts"]
            msg = (f"v{v.number} is {'ready for review' if result['status'] == 'ready_for_review' else 'BLOCKED by validation'}: "
                   f"{n_ok['pass']} checks passed, {n_ok['warn']} warnings, {n_ok['fail']} failed.")
            fb = result.get("definition_fallback")
            if fb:
                lbl = {"link_clicks": "link clicks", "clicks_all": "all clicks"}
                msg += (f" Note: you asked for {lbl.get(fb['requested'], fb['requested'])}, but none of the uploaded exports contain that column, "
                        f"so {lbl.get(fb['used'], fb['used'])} were kept. Upload an export with that column to switch.")
            s.add(Message(task_id=tid, version_id=vid, author_id=None, kind="assistant", text=msg))
        elif result["status"] == "awaiting_clarification":
            s.add(Message(task_id=tid, version_id=vid, author_id=None, kind="assistant",
                          text=f"I need {len(result['questions'])} answer(s) before generating files. Inspection and mapping are saved."))
        job = s.get(Job, job_id)
        job.heartbeat_at = now()
        s.commit()


def change_summary(base: TaskVersion, v: TaskVersion, s) -> dict:
    out = {"ops": v.revision_ops or [], "figures": [], "artifacts": [], "sheets": {}}
    b = (base.result or {}).get("key_figures", {}).get("total", {})
    n = (v.result or {}).get("key_figures", {}).get("total", {})
    from .engine.calc import METRIC_LABELS
    from .engine import fmt as FM
    cur = (v.result or {}).get("summary", {}).get("currency") or ""
    for m in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa"):
        if b.get(m) != n.get(m):
            out["figures"].append(f"{METRIC_LABELS.get(m, m)}: {FM.fmt_metric(m, b.get(m), cur)} → {FM.fmt_metric(m, n.get(m), cur)}")
    arts = s.scalars(select(Artifact).where(Artifact.version_id == v.id, Artifact.kind.notin_(["preview", "record"]))).all()
    for a in arts:
        out["artifacts"].append(f"{a.label}: " + ("unchanged - same inputs as the previous version" if a.reused_from_id else "regenerated with changes"))
    bfiles = set(base.file_ids or [])
    newf = [fid for fid in (v.file_ids or []) if fid not in bfiles]
    if newf:
        names = [f.original_name for f in s.scalars(select(TaskFile).where(TaskFile.id.in_(newf))).all()]
        out["new_files"] = names
    return out
