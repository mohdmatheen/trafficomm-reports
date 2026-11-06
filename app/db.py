"""Database models (PostgreSQL in production; SQLite works for quick local tests)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint,
                        create_engine, event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="employee")  # employee | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    short: Mapped[str] = mapped_column(String(30), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)  # hidden from new tasks; existing tasks stay visible


class UserClient(Base):
    """Which clients an employee may work on. Admins see every client and need no rows here."""
    __tablename__ = "user_clients"
    __table_args__ = (UniqueConstraint("user_id", "client_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    granted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    brand: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    period_start: Mapped[dt.date | None]
    period_end: Mapped[dt.date | None]
    comparison_start: Mapped[dt.date | None]
    comparison_end: Mapped[dt.date | None]
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Dubai")
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    fx_usd_aed: Mapped[float | None] = mapped_column(Float, default=None)
    deliverables: Mapped[list] = mapped_column(JSON, default=list)
    instructions: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    current_version_id: Mapped[int | None] = mapped_column(Integer)
    approved_version_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, index=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    client: Mapped[Client] = relationship()
    owner: Mapped[User] = relationship()
    profile: Mapped[Profile | None] = relationship()


class TaskFile(Base):
    __tablename__ = "task_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # raw | template | client_deck | creatives
    original_name: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(600))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    added_in_version: Mapped[int] = mapped_column(Integer, default=1)
    inspection: Mapped[dict | None] = mapped_column(JSON)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


class TaskVersion(Base):
    __tablename__ = "task_versions"
    __table_args__ = (UniqueConstraint("task_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    base_version_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="initial")  # initial | revision
    feedback: Mapped[str | None] = mapped_column(Text)
    revision_ops: Mapped[list | None] = mapped_column(JSON)
    plan_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    file_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    stage: Mapped[str] = mapped_column(String(40), default="uploaded")
    result: Mapped[dict | None] = mapped_column(JSON)
    change_summary: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(Integer)


class StageEvent(Base):
    __tablename__ = "stage_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("task_versions.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(300), default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("task_versions.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # excel | excel_consolidated | deck_update | deck_new | pdf | preview | record
    label: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(600))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    fingerprint: Mapped[str] = mapped_column(String(40), default="")
    reused_from_id: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("task_versions.id"), index=True)
    key: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    default: Mapped[str | None] = mapped_column(String(300))
    allow_free_text: Mapped[bool] = mapped_column(Boolean, default=False)
    context: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")
    answer: Mapped[str | None] = mapped_column(String(300))
    answered_by: Mapped[int | None] = mapped_column(Integer)
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    saved_to_profile: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    version_id: Mapped[int | None] = mapped_column(Integer)
    author_id: Mapped[int | None] = mapped_column(Integer)  # None = system/assistant
    kind: Mapped[str] = mapped_column(String(20))  # instructions | feedback | assistant | system | answer
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    version_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="process_version")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|succeeded|failed|cancelled
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(200))
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


# ---------------------------------------------------------------------------
# Usage & costs ledger. No foreign keys to tasks/users on purpose: rows must survive retention
# deletes and account changes, so names are snapshotted at request time.
# ---------------------------------------------------------------------------
class AIRequest(Base):
    """One attempt at one application-initiated model request (retries are separate rows)."""
    __tablename__ = "ai_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    initiated_by: Mapped[str] = mapped_column(String(10), default="employee")  # employee | system
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_name: Mapped[str | None] = mapped_column(String(120))
    user_email: Mapped[str | None] = mapped_column(String(200))
    task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    version_id: Mapped[int | None] = mapped_column(Integer)
    version_number: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(Integer)
    client_name: Mapped[str | None] = mapped_column(String(200))
    step: Mapped[str] = mapped_column(String(40))  # instructions | feedback | commentary
    provider: Mapped[str] = mapped_column(String(20), default="anthropic")
    model_requested: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))  # as returned by the provider
    credential_ref: Mapped[str | None] = mapped_column(String(80))  # configured key id or a non-reversible fingerprint
    workspace_ref: Mapped[str | None] = mapped_column(String(80))
    provider_request_id: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    first_attempt_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="started")  # started | succeeded | failed | refused
    error_type: Mapped[str | None] = mapped_column(String(80))
    service_tier: Mapped[str | None] = mapped_column(String(30))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_creation_5m_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_creation_1h_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    web_search_requests: Mapped[int | None] = mapped_column(Integer)


class ProviderCostRow(Base):
    """Provider-reported cost for one bucket and dimension combination. Replaced, never summed, on resync."""
    __tablename__ = "provider_cost_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    bucket_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    bucket_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    workspace_id: Mapped[str | None] = mapped_column(String(80))
    api_key_id: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    service_tier: Mapped[str | None] = mapped_column(String(30))
    cost_type: Mapped[str | None] = mapped_column(String(40))
    token_type: Mapped[str | None] = mapped_column(String(60))
    amount: Mapped[object] = mapped_column(Numeric(20, 6))  # in the provider's unit (Anthropic: cents)
    list_amount: Mapped[object | None] = mapped_column(Numeric(20, 6))
    amount_unit: Mapped[str] = mapped_column(String(20), default="USD_cents")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    scope: Mapped[str] = mapped_column(String(20), default="unmapped")  # app | excluded | unmapped
    data_refreshed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    raw_group: Mapped[dict] = mapped_column(JSON, default=dict)


class ProviderUsageRow(Base):
    __tablename__ = "provider_usage_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    bucket_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    bucket_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    workspace_id: Mapped[str | None] = mapped_column(String(80))
    api_key_id: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(20), default="unmapped")
    requests: Mapped[int | None] = mapped_column(BigInteger)
    uncached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_5m_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_1h_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    web_search_requests: Mapped[int] = mapped_column(BigInteger, default=0)
    data_refreshed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class CredentialMapping(Base):
    """Which provider workspaces / API keys belong to this reporting app, with effective dates."""
    __tablename__ = "credential_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))  # workspace | api_key | render_service
    external_id: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(120), default="")
    scope: Mapped[str] = mapped_column(String(20), default="app")  # app | excluded
    source: Mapped[str] = mapped_column(String(20), default="env")  # env | admin
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class SyncState(Base):
    __tablename__ = "sync_state"
    source: Mapped[str] = mapped_column(String(40), primary_key=True)  # anthropic_cost | anthropic_usage | render | fx
    status: Mapped[str] = mapped_column(String(30), default="never")  # never | ok | error | not_configured | unsupported | backoff
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    data_refreshed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_allowed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[object] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(120))
    rate_date: Mapped[str] = mapped_column(String(10))  # the source's own date for the rate
    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    __table_args__ = (UniqueConstraint("base", "quote", "source", "rate_date", name="uq_fx_day"),)


class RenderInvoice(Base):
    """A Render invoice total entered by an admin. Render has no billing API, so this is the only source."""
    __tablename__ = "render_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)  # YYYY-MM, the invoice's billing month
    amount_usd: Mapped[object] = mapped_column(Numeric(14, 2))
    note: Mapped[str] = mapped_column(String(300), default="")
    entered_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entered_by_name: Mapped[str] = mapped_column(String(200), default="")
    entered_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    period_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(300))


_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings.database_url
        kw = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kw)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def Session():
    engine()
    return _Session()


def reset_engine():
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def init_db():
    Base.metadata.create_all(engine())
    # lightweight additive migrations for databases created by earlier versions
    from sqlalchemy import text
    with engine().begin() as c:
        c.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS fx_usd_aed DOUBLE PRECISION"))
        c.execute(text("ALTER TABLE clients ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE"))
    migrate_access()


LEGACY_DOMAIN = "trafficomm.local"


def reassign_user_records(s, from_id: int, to_id: int) -> int:
    """Move everything a user owns or created to another user, so the user row can be deleted.
    Plain integer references (answered_by, approved_by, audit rows, usage ledger) are left as history."""
    from sqlalchemy import update
    n = s.execute(update(Task).where(Task.owner_id == from_id).values(owner_id=to_id)).rowcount or 0
    s.execute(update(TaskFile).where(TaskFile.uploaded_by == from_id).values(uploaded_by=to_id))
    s.execute(update(TaskVersion).where(TaskVersion.created_by == from_id).values(created_by=to_id))
    return n


def migrate_access():
    """One-time data steps for per-client access, safe to run from several processes at once.

    1. When per-client access is first introduced, every existing employee keeps access to every
       existing client, so nobody loses their work on deploy. Admins then narrow it on the Admin page.
    2. Accounts from the removed password sign-in (@trafficomm.local) are deleted. Their tasks move
       to the first Zoho admin so no report is lost. Skipped until a Zoho admin exists."""
    from sqlalchemy import delete, func, text
    from .config import settings as cfg
    with Session() as s:
        if s.bind.dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(724002)"))
        if not s.get(AppSetting, "client_access_v1"):
            clients = s.scalars(select(Client.id)).all()
            for uid in s.scalars(select(User.id).where(User.role != "admin")).all():
                for cid in clients:
                    s.add(UserClient(user_id=uid, client_id=cid))
            s.add(AppSetting(key="client_access_v1", value=now().isoformat()))
        legacy = s.scalars(select(User).where(func.lower(User.email).like("%@" + LEGACY_DOMAIN))).all()
        if legacy:
            admins = s.scalars(select(User).where(User.role == "admin", User.active.is_(True),
                                                  func.lower(User.email).notlike("%@" + LEGACY_DOMAIN)).order_by(User.id)).all()
            boot = [a for a in admins if a.email.lower() in cfg.zoho_admin_emails]
            heir = (boot or admins or [None])[0]
            if heir is not None:
                for u in legacy:
                    moved = reassign_user_records(s, u.id, heir.id)
                    s.execute(delete(UserClient).where(UserClient.user_id == u.id))
                    s.add(AuditLog(user_id=None, action="delete_legacy_user", target=f"{u.email} ({moved} task(s) moved to {heir.email})"))
                    s.delete(u)
        s.commit()
