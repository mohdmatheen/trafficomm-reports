"""Tables for AI Support, Client Intelligence, the engineering workflow and notifications.

Imported by app.db after Base is defined so create_all() picks them up. New tables only: existing
tables get no destructive changes."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, now

ISSUE_STATUSES = {
    "open": "Open",
    "awaiting_employee": "Waiting for your answer",
    "investigating": "Investigating",
    "engineering_queued": "Queued for engineering",
    "engineering_running": "Engineering investigation running",
    "fix_proposed": "Fix proposed (pull request)",
    "awaiting_approval": "Awaiting approval",
    "awaiting_deploy": "Approved, waiting for deployment",
    "awaiting_validation": "Corrected report ready for review",
    "resolved": "Resolved",
    "reopened": "Reopened",
    "unresolved": "Could not be resolved automatically",
}
OPEN_STATUSES = {k for k in ISSUE_STATUSES if k not in ("resolved",)}

CATEGORIES = {
    "discrepancy": "Reporting discrepancy",
    "missing_data": "Missing data",
    "incorrect_data": "Incorrect data",
    "procedure": "Manual reporting procedure",
    "structure_change": "Report structure change",
    "calculation": "Calculation issue",
    "portal_bug": "Portal functionality issue",
    "question": "Question",
}


class SupportIssue(Base):
    __tablename__ = "support_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(30), default="question", index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    risk: Mapped[str] = mapped_column(String(20), default="low")  # low | medium | high
    source: Mapped[str] = mapped_column(String(20), default="employee")  # employee | validation
    reporter_id: Mapped[int | None] = mapped_column(Integer, index=True)
    assignee_id: Mapped[int | None] = mapped_column(Integer, index=True)  # employee or reporting lead who gets routine notices
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    version_id: Mapped[int | None] = mapped_column(Integer)
    version_number: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)  # duplicate-submission guard
    summary: Mapped[str] = mapped_column(Text, default="")  # agent's diagnostic summary (sanitised)
    resolution: Mapped[str] = mapped_column(Text, default="")
    pilot: Mapped[str | None] = mapped_column(String(40))  # e.g. fab_ios_all_events
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def ref(self) -> str:
        return f"AIS-{self.id:04d}"


class SupportMessage(Base):
    __tablename__ = "support_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("support_issues.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int | None] = mapped_column(Integer)  # None = assistant / system
    kind: Mapped[str] = mapped_column(String(20))  # employee | assistant | system | engineering
    text: Mapped[str] = mapped_column(Text)
    engine: Mapped[str | None] = mapped_column(String(20))  # rules | claude | saved (assistant messages)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # questions asked, rule ids cited, ...
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class SupportAttachment(Base):
    __tablename__ = "support_attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("support_issues.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str] = mapped_column(String(40), default="other")  # pilot upload slot or 'other'
    original_name: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(600))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    task_file_id: Mapped[int | None] = mapped_column(Integer)  # linked report file, when it came from the task
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class SupportEvent(Base):
    """Append-only audit trail for an issue: status changes, approvals, dispatches, sync results."""
    __tablename__ = "support_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_id: Mapped[int | None] = mapped_column(Integer)  # None = system
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class EngineeringRun(Base):
    """One request for code-level investigation. Durable: dispatch, retries and status sync run from the worker."""
    __tablename__ = "engineering_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("support_issues.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    # queued | dispatched | pr_open | checks_failed | checks_passed | awaiting_approval | approved | merged | deployed
    # | closed | failed | cancelled | blocked
    backend: Mapped[str] = mapped_column(String(20), default="")  # routine | github_actions
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    branch: Mapped[str] = mapped_column(String(200), default="")
    session_url: Mapped[str | None] = mapped_column(String(300))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(300))
    pr_state: Mapped[str | None] = mapped_column(String(20))
    checks_state: Mapped[str | None] = mapped_column(String(20))  # pending | success | failure
    merge_sha: Mapped[str | None] = mapped_column(String(64))
    approval_level: Mapped[str | None] = mapped_column(String(20))  # lead | admin
    approved_by: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    agent_report: Mapped[dict] = mapped_column(JSON, default=dict)  # root cause, summary, questions (from callback/PR body)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload_chars: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(30))  # needs_input | resolved | corrected_report | validation_failed | approval_required | usage_limit
    text: Mapped[str] = mapped_column(String(500))
    link: Mapped[str] = mapped_column(String(300), default="")
    issue_id: Mapped[int | None] = mapped_column(Integer)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    email_status: Mapped[str] = mapped_column(String(20), default="none")  # none | pending | sent | failed
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


RULE_CATEGORIES = {
    "profile": "Client reporting profile",
    "data_source": "Data sources & file schemas",
    "template": "Report templates",
    "metric": "Metrics & event definitions",
    "campaign_mapping": "Campaign mapping",
    "exclusion": "Exclusions",
    "calculation": "Calculation & deduplication",
    "currency_timezone": "Currency & time zone",
    "validation": "Validation rules",
    "correction": "Approved historical corrections",
}


class ClientRule(Base):
    """Client Intelligence: one version of one client reporting rule. Only 'approved' versions count."""
    __tablename__ = "client_rules"
    __table_args__ = (UniqueConstraint("client_id", "key", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))  # stable slug across versions
    version: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(30), default="profile")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict] = mapped_column(JSON, default=dict)  # machine-readable part, if any
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)  # proposed | approved | rejected | superseded
    source: Mapped[str] = mapped_column(String(30), default="employee")  # code | employee | engineering | validation
    issue_id: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(300))
    proposed_by: Mapped[int | None] = mapped_column(Integer)
    decided_by: Mapped[int | None] = mapped_column(Integer)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(String(500), default="")
    validated: Mapped[bool] = mapped_column(Boolean, default=False)  # validated against real report data
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
