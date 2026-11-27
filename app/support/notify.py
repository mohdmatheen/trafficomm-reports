"""Portal notifications, with optional email through Zoho SMTP.

Routine notices go to the issue's employee and the client's reporting leads, not to the Managing
Director. Admins only get notices that need an administrator (admin-level approvals, limits hit)."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from sqlalchemy import select

from ..db import Client, User, UserClient, now
from . import controls
from .models import Notification, SupportIssue

log = logging.getLogger("trafficomm.support.notify")


def leads_for_client(s, client_id: int | None) -> list[User]:
    if not client_id:
        return []
    q = (select(User).join(UserClient, UserClient.user_id == User.id)
         .where(UserClient.client_id == client_id, User.is_lead.is_(True), User.active.is_(True)))
    return list(s.scalars(q).all())


def admins(s) -> list[User]:
    return list(s.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all())


def push(s, user_ids, kind: str, text: str, issue: SupportIssue | None = None, link: str = ""):
    email = controls.is_on(s, "email_notifications") and smtp_configured()
    seen = set()
    for uid in user_ids:
        if not uid or uid in seen:
            continue
        seen.add(uid)
        s.add(Notification(user_id=uid, kind=kind, text=text[:500], issue_id=issue.id if issue else None,
                           link=link or (f"/support/{issue.id}" if issue else ""), email_status="pending" if email else "none"))


def for_issue(s, issue: SupportIssue, kind: str, text: str, include_leads: bool = False, include_admins: bool = False):
    ids = [issue.assignee_id or issue.reporter_id, issue.reporter_id]
    if include_leads:
        ids += [u.id for u in leads_for_client(s, issue.client_id)]
    if include_admins or (include_leads and not leads_for_client(s, issue.client_id)):
        ids += [u.id for u in admins(s)]
    push(s, ids, kind, text, issue)


def unread_count(user_id: int) -> int:
    from sqlalchemy import func

    from ..db import Session
    with Session() as s:
        return s.scalar(select(func.count(Notification.id)).where(Notification.user_id == user_id, Notification.read_at.is_(None))) or 0


def smtp_configured() -> bool:
    return bool(os.environ.get("ZOHO_SMTP_USER") and os.environ.get("ZOHO_SMTP_PASSWORD"))


def send_pending_emails(s, limit: int = 20) -> int:
    """Called from the worker. Sends queued notification emails; failures are recorded, never retried in a loop."""
    if not smtp_configured():
        return 0
    rows = s.scalars(select(Notification).where(Notification.email_status == "pending").order_by(Notification.id).limit(limit)).all()
    if not rows:
        return 0
    host, port = os.environ.get("ZOHO_SMTP_HOST", "smtp.zoho.in"), int(os.environ.get("ZOHO_SMTP_PORT", "465"))
    sender = os.environ["ZOHO_SMTP_USER"]
    base = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    sent = 0
    try:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(sender, os.environ["ZOHO_SMTP_PASSWORD"])
            for n in rows:
                u = s.get(User, n.user_id)
                if not u or not u.active:
                    n.email_status = "none"
                    continue
                m = EmailMessage()
                m["From"], m["To"], m["Subject"] = sender, u.email, "Trafficomm Reporting: " + n.text[:90]
                m.set_content(f"{n.text}\n\n{base}{n.link}\n\nThis is an automatic message from the Trafficomm Reporting Workspace.")
                try:
                    smtp.send_message(m)
                    n.email_status, sent = "sent", sent + 1
                except Exception:  # noqa: BLE001
                    n.email_status = "failed"
    except Exception:  # noqa: BLE001
        log.exception("SMTP connection failed")
        for n in rows:
            if n.email_status == "pending":
                n.email_status = "failed"
    return sent


def mark_read(s, user_id: int, ids: list[int] | None = None):
    q = select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if ids:
        q = q.where(Notification.id.in_(ids))
    for n in s.scalars(q):
        n.read_at = now()


def client_name(s, client_id) -> str:
    c = s.get(Client, client_id) if client_id else None
    return c.name if c else ""
