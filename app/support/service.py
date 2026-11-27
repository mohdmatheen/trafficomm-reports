"""Issue workflow: create (with duplicate guard), converse, attach evidence, resolve, reopen.

Every state change writes a SupportEvent (audit trail). Employees never need a Claude or GitHub account:
everything happens through authenticated portal endpoints."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import datetime as dt

from sqlalchemy import select

from ..db import Client, Task, TaskVersion, User, UserClient, now
from ..storage import safe_name, sha256, storage
from . import agent, notify
from .models import OPEN_STATUSES, SupportAttachment, SupportEvent, SupportIssue, SupportMessage

ATTACH_EXT = {".csv", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".pdf", ".txt", ".docx", ".zip"}
MAX_ATTACH = 50 * 1024 * 1024
DUPLICATE_WINDOW = dt.timedelta(days=7)


class SupportError(ValueError):
    pass


def event(s, issue_id, actor_id, action, detail=""):
    s.add(SupportEvent(issue_id=issue_id, actor_id=actor_id, action=action, detail=str(detail)[:2000]))


def set_status(s, issue: SupportIssue, status: str, actor_id=None, detail=""):
    if issue.status != status:
        event(s, issue.id, actor_id, "status", f"{issue.status} → {status}" + (f": {detail}" if detail else ""))
        issue.status = status
        if status == "resolved":
            issue.resolved_at = now()


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------
def allowed_clients(s, user: User) -> set[int] | None:
    if user.role == "admin":
        return None
    return set(s.scalars(select(UserClient.client_id).where(UserClient.user_id == user.id)).all())


def can_view(s, user: User, issue: SupportIssue | None) -> bool:
    if issue is None:
        return False
    if user.role == "admin" or issue.reporter_id == user.id or issue.assignee_id == user.id:
        return True
    allowed = allowed_clients(s, user)
    return issue.client_id is not None and allowed is not None and issue.client_id in allowed


def get_checked(s, user: User, issue_id: int) -> SupportIssue:
    i = s.get(SupportIssue, issue_id)
    if not can_view(s, user, i):
        raise SupportError("Issue not found")
    return i


def visible_issues_query(s, user: User):
    q = select(SupportIssue)
    allowed = allowed_clients(s, user)
    if allowed is not None:
        from sqlalchemy import or_
        q = q.where(or_(SupportIssue.reporter_id == user.id, SupportIssue.assignee_id == user.id,
                        SupportIssue.client_id.in_(allowed or {0})))
    return q


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())).strip()[:600]


def fingerprint(reporter_id, task_id, text: str) -> str:
    return hashlib.sha256(f"{reporter_id}|{task_id}|{_norm(text)}".encode()).hexdigest()


def create_issue(s, user: User, title: str, text: str, task_id: int | None = None, version_id: int | None = None,
                 client_id: int | None = None, pilot: str | None = None, assignee_id: int | None = None) -> tuple[SupportIssue, bool]:
    """Returns (issue, created). A repeat of the same report by the same person within 7 days returns the open issue."""
    title, text = (title or "").strip(), (text or "").strip()
    if not title and not text:
        raise SupportError("Describe the problem first.")
    title = title or (text[:90] + ("…" if len(text) > 90 else ""))
    task = None
    if task_id:
        from ..auth import can_access_task
        task = s.get(Task, task_id)
        if not can_access_task(user, task, allowed_clients(s, user)):
            raise SupportError("Task not found")
        client_id = task.client_id
        if version_id:
            v = s.get(TaskVersion, version_id)
            if not v or v.task_id != task.id:
                version_id = None
        version_id = version_id or task.current_version_id
    if client_id:
        allowed = allowed_clients(s, user)
        if allowed is not None and client_id not in allowed:
            raise SupportError("You don't have access to that client.")
        if not s.get(Client, client_id):
            raise SupportError("Unknown client")
    fp = fingerprint(user.id, task_id, title + " " + text) if not pilot else fingerprint(user.id, f"pilot:{pilot}:{client_id}", "")
    dup = s.scalar(select(SupportIssue).where(SupportIssue.fingerprint == fp, SupportIssue.status.in_(OPEN_STATUSES),
                                              SupportIssue.created_at >= now() - DUPLICATE_WINDOW).order_by(SupportIssue.id.desc()).limit(1))
    if dup:
        if text:
            s.add(SupportMessage(issue_id=dup.id, author_id=None, kind="system",
                                 text=f"{user.name} submitted the same report again; it was added to this issue instead of opening a new one."))
        return dup, False
    v = s.get(TaskVersion, version_id) if version_id else None
    issue = SupportIssue(title=title[:300], reporter_id=user.id, assignee_id=assignee_id or (task.owner_id if task and pilot else user.id),
                         client_id=client_id, task_id=task_id, version_id=version_id, version_number=v.number if v else None,
                         fingerprint=fp, pilot=pilot, meta={})
    s.add(issue)
    s.flush()
    event(s, issue.id, user.id, "created", f"by {user.name}" + (f" on task {task_id} v{issue.version_number}" if task_id else ""))
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="employee", text=text or title))
    agent.respond(s, issue, user, first=True)
    _notify_status(s, issue)
    return issue, True


def from_validation(s, task: Task, version: TaskVersion, lines: list[str] | None = None) -> SupportIssue | None:
    """Report validation found a blocking discrepancy: open (or reuse) an issue for the task owner. Rules only."""
    fp = hashlib.sha256(f"validation|{task.id}|{version.id}|{'sheet' if lines else 'checks'}".encode()).hexdigest()
    if s.scalar(select(SupportIssue.id).where(SupportIssue.fingerprint == fp)):
        return None
    res = version.result or {}
    fails = [c for c in ((res.get("validation") or {}).get("checks") or []) if c.get("status") == "fail"]
    from .sanitize import clean
    lines = [clean(x, 300) for x in lines] if lines else [clean(f"{c.get('title') or c.get('name') or c.get('id')}: {c.get('detail') or c.get('text') or ''}", 300) for c in fails[:8]]
    issue = SupportIssue(title=f"Validation failed: {task.title or 'task ' + str(task.id)} v{version.number}"[:300], category="discrepancy",
                         risk="medium", source="validation", reporter_id=task.owner_id, assignee_id=task.owner_id, client_id=task.client_id,
                         task_id=task.id, version_id=version.id, version_number=version.number, fingerprint=fp, status="awaiting_employee",
                         summary="Validation failed checks:\n" + "\n".join(lines), meta={"open_questions": [
                             "Is the failed check a real problem with the data, or is the check wrong for this report?"]})
    s.add(issue)
    s.flush()
    event(s, issue.id, None, "created", "automatically from report validation")
    s.add(SupportMessage(issue_id=issue.id, kind="assistant", engine="rules", text=(
        f"Report validation blocked v{version.number} of this task. Failed checks:\n" + "\n".join("• " + x for x in lines)
        + "\n\nIs the failed check a real problem with the data, or is the check wrong for this report? Reply here and I'll take it from there.")))
    notify.for_issue(s, issue, "validation_failed", f"Validation failed on “{task.title}” v{version.number} ({issue.ref})", include_leads=True)
    return issue


# ---------------------------------------------------------------------------
# conversation
# ---------------------------------------------------------------------------
def post_message(s, issue: SupportIssue, user: User, text: str) -> SupportMessage:
    text = (text or "").strip()
    if not text:
        raise SupportError("Write a message first.")
    if len(text) > 8000:
        raise SupportError("That message is too long (8,000 characters max). Attach a file instead.")
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="employee", text=text))
    if issue.status == "resolved":
        reopen(s, issue, user, "New message after resolution")
    s.flush()
    m = agent.respond(s, issue, user)
    _notify_status(s, issue)
    return m


def _notify_status(s, issue: SupportIssue):
    if issue.status == "awaiting_employee":
        notify.for_issue(s, issue, "needs_input", f"{issue.ref}: Claude needs more information")


def add_attachment(s, issue: SupportIssue, user: User, tmp_path: str, name: str, slot: str = "other") -> SupportAttachment:
    from .fab_pilot import SLOT_KEYS
    ext = os.path.splitext(name or "")[1].lower()
    size = os.path.getsize(tmp_path)
    try:
        if ext not in ATTACH_EXT:
            raise SupportError(f"'{name}': attach CSV, XLSX, PPTX, PDF, DOCX, TXT, PNG/JPG or ZIP files.")
        if size == 0:
            raise SupportError(f"'{name}' is empty.")
        if size > MAX_ATTACH:
            raise SupportError(f"'{name}' is larger than 50 MB.")
        if slot != "other" and slot not in SLOT_KEYS:
            slot = "other"
    except SupportError:
        os.remove(tmp_path)
        raise
    a = SupportAttachment(issue_id=issue.id, slot=slot, original_name=safe_name(name), stored_path="", size=size, sha256=sha256(tmp_path),
                          uploaded_by=user.id)
    s.add(a)
    s.flush()
    d = os.path.join(storage.root, "support", str(int(issue.id)))
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"{a.id}_{safe_name(name)}")
    shutil.move(tmp_path, dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP)
    a.stored_path = dest
    event(s, issue.id, user.id, "attachment", f"{a.original_name} ({slot})")
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="system", text=f"{user.name} attached {a.original_name}."))
    return a


def resolve(s, issue: SupportIssue, user: User, note: str = ""):
    issue.resolution = (note or "").strip()[:4000] or issue.resolution
    set_status(s, issue, "resolved", user.id, note)
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="system", text=f"Marked resolved by {user.name}." + (f" {note}" if note else "")))
    notify.for_issue(s, issue, "resolved", f"{issue.ref} was resolved")


def reopen(s, issue: SupportIssue, user: User, reason: str = ""):
    if issue.status != "resolved" and issue.status != "unresolved":
        raise SupportError("Only a resolved or unresolved issue can be reopened.")
    issue.reopen_count += 1
    set_status(s, issue, "reopened", user.id, reason)
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="system", text=f"Reopened by {user.name}." + (f" Reason: {reason}" if reason else "")))


def confirm_corrected(s, issue: SupportIssue, user: User, ok: bool, note: str = ""):
    """The employee reviewed the regenerated report."""
    if issue.status != "awaiting_validation":
        raise SupportError("There's no corrected report waiting for review on this issue.")
    if ok:
        resolve(s, issue, user, note or "Corrected report reviewed and accepted.")
        from .engineering import record_solution
        record_solution(s, issue, user)
    else:
        set_status(s, issue, "reopened", user.id, note or "Corrected report not accepted")
        issue.reopen_count += 1
        s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="employee", text="The corrected report is still wrong. " + (note or "")))
        agent.respond(s, issue, user)
