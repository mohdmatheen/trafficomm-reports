"""Authentication (Zoho sign-in -> signed session cookie) and server-side access checks.

Password sign-in was removed: only sessions created by the Zoho callback are accepted."""
from __future__ import annotations

from fastapi import HTTPException, Request

from sqlalchemy import select

from .db import Client, Session, Task, User, UserClient


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    # sessions from the removed password sign-in (no "via" marker) are not accepted
    if not uid or request.session.get("via") != "zoho":
        return None
    with Session() as s:
        u = s.get(User, uid)
        if not u or not u.active:
            return None
        s.expunge(u)
        return u


def require_user(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Login required")
    return u


def require_admin(request: Request) -> User:
    u = require_user(request)
    if u.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return u


def allowed_client_ids(s, user: User) -> set[int] | None:
    """Clients the user may see and work on. None means every client (admins)."""
    if user.role == "admin":
        return None
    return set(s.scalars(select(UserClient.client_id).where(UserClient.user_id == user.id)).all())


def visible_clients(s, user: User, include_archived: bool = True) -> list[Client]:
    stmt = select(Client).order_by(Client.name)
    if not include_archived:
        stmt = stmt.where(Client.archived.is_(False))
    allowed = allowed_client_ids(s, user)
    if allowed is not None:
        stmt = stmt.where(Client.id.in_(allowed or {0}))
    return list(s.scalars(stmt).all())


def can_access_task(user: User, task: Task | None, allowed: set[int] | None) -> bool:
    """Admins see every task. Employees see every task of the clients they are assigned to."""
    if task is None or task.deleted_at is not None:
        return False
    return user.role == "admin" or (allowed is not None and task.client_id in allowed)


def get_task_checked(s, user: User, task_id: int) -> Task:
    t = s.get(Task, task_id)
    if not can_access_task(user, t, allowed_client_ids(s, user)):
        # 404 (not 403) so task existence isn't revealed to other users
        raise HTTPException(status_code=404, detail="Task not found")
    return t
