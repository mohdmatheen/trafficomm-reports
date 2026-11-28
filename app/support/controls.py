"""Administrator controls for AI automation: emergency stop, budgets, per-employee limits, concurrency.

Stored in app_settings so they change without a deploy. Every check returns a reason the UI can show."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select

from ..db import AIRequest, AppSetting, now

DEFAULTS = {
    "ai_support_enabled": "1",          # employee-facing Claude replies (rules always work)
    "engineering_enabled": "1",         # dispatching code-level investigations
    "emergency_stop": "0",              # stops ALL automation (Claude calls from support + engineering dispatch)
    "monthly_ai_budget_usd": "150",     # estimated AI spend for support + engineering steps this month
    "employee_monthly_requests": "60",  # Claude support replies per employee per month (0 = no Claude)
    "engineering_max_concurrent": "2",  # investigations running at once
    "engineering_daily_cap": "10",      # dispatches per UTC day (the scheduled task itself allows 30/hour)
    "email_notifications": "0",         # also email notices through Zoho SMTP when configured
}
LABELS = {
    "ai_support_enabled": "Claude replies in AI Support",
    "engineering_enabled": "Engineering investigations",
    "emergency_stop": "Emergency stop (all automation)",
    "monthly_ai_budget_usd": "Monthly AI budget (USD, estimated)",
    "employee_monthly_requests": "Claude replies per employee per month",
    "engineering_max_concurrent": "Investigations at the same time",
    "engineering_daily_cap": "Investigations per day",
    "email_notifications": "Also send notifications by email",
}
BOOL_KEYS = {"ai_support_enabled", "engineering_enabled", "emergency_stop", "email_notifications"}
SUPPORT_STEPS = ("support_chat", "support_classify", "support_summary")

# Estimated list prices (USD per million tokens) used only for the budget guard and the dashboard;
# the Usage & costs page remains the source of truth for billed cost.
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0), "fable": (5.0, 25.0)}


def _key(k: str) -> str:
    return "aisupport." + k


def get(s, k: str) -> str:
    row = s.get(AppSetting, _key(k))
    return row.value if row else DEFAULTS[k]


def get_user_limit(s, uid: int) -> int:
    row = s.get(AppSetting, _key(f"user_limit.{uid}"))
    return int(row.value) if row else int(get(s, "employee_monthly_requests"))


def all_values(s) -> dict:
    return {k: get(s, k) for k in DEFAULTS}


def set_value(s, k: str, v) -> str:
    if k.startswith("user_limit."):
        val = str(max(0, int(v)))
    elif k not in DEFAULTS:
        raise ValueError(f"Unknown setting {k}")
    elif k in BOOL_KEYS:
        val = "1" if str(v).lower() in ("1", "true", "on", "yes") else "0"
    else:
        try:
            num = Decimal(str(v))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"{LABELS.get(k, k)} must be a number") from e
        if num < 0:
            raise ValueError(f"{LABELS.get(k, k)} can't be negative")
        val = str(num if k == "monthly_ai_budget_usd" else int(num))
    row = s.get(AppSetting, _key(k))
    if row:
        row.value = val
    else:
        s.add(AppSetting(key=_key(k), value=val))
    return val


def is_on(s, k: str) -> bool:
    return get(s, k) == "1"


def month_start(at: dt.datetime | None = None) -> dt.datetime:
    at = at or now()
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def estimate_cost(model: str | None, inp: int | None, out: int | None, cache_read: int | None = 0, cache_write: int | None = 0) -> Decimal:
    fam = next((f for f in PRICES if f in (model or "").lower()), "opus")
    pi, po = PRICES[fam]
    usd = ((inp or 0) * pi + (out or 0) * po + (cache_read or 0) * pi * 0.1 + (cache_write or 0) * pi * 1.25) / 1_000_000
    return Decimal(str(round(usd, 6)))


def ai_spend(s, since: dt.datetime, steps: tuple | None = None, user_id: int | None = None) -> Decimal:
    q = select(AIRequest).where(AIRequest.started_at >= since)
    if steps:
        q = q.where(AIRequest.step.in_(steps))
    if user_id:
        q = q.where(AIRequest.user_id == user_id)
    total = Decimal(0)
    for r in s.scalars(q):
        total += estimate_cost(r.model or r.model_requested, r.input_tokens, r.output_tokens, r.cache_read_tokens,
                               (r.cache_creation_5m_tokens or 0) + (r.cache_creation_1h_tokens or 0))
    return total


def user_requests(s, uid: int, since: dt.datetime) -> int:
    return s.scalar(select(func.count(AIRequest.id)).where(AIRequest.user_id == uid, AIRequest.started_at >= since,
                                                          AIRequest.step.in_(SUPPORT_STEPS), AIRequest.attempt == 1)) or 0


@dataclass
class Gate:
    ok: bool
    reason: str = ""
    limit_hit: str | None = None  # which configured limit stopped it (for notifications)


def claude_gate(s, user, client) -> Gate:
    """May the support agent call Claude for this employee and client right now?"""
    from ..config import settings
    if not settings.ai_enabled:
        return Gate(False, "Claude isn't configured on the server, so built-in rules answered.")
    if is_on(s, "emergency_stop"):
        return Gate(False, "AI automation is paused by an administrator (emergency stop). Built-in rules answered.", "emergency_stop")
    if not is_on(s, "ai_support_enabled"):
        return Gate(False, "Claude replies are switched off by an administrator. Built-in rules answered.")
    if client is not None and not client.ai_processing_allowed:
        return Gate(False, f"{client.name} hasn't been authorised for external AI processing, so nothing was sent to Claude. "
                           "Built-in rules answered; an admin can enable it in AI Support administration.")
    budget = Decimal(get(s, "monthly_ai_budget_usd"))
    if ai_spend(s, month_start()) >= budget:
        return Gate(False, "This month's AI budget has been reached. Built-in rules answered.", "monthly_budget")
    if user is not None and user.role != "admin":
        lim = get_user_limit(s, user.id)
        if user_requests(s, user.id, month_start()) >= lim:
            return Gate(False, f"You've reached your monthly limit of {lim} Claude replies. Built-in rules answered.", "employee_limit")
    return Gate(True)


def engineering_gate(s) -> Gate:
    if is_on(s, "emergency_stop"):
        return Gate(False, "Emergency stop is on.", "emergency_stop")
    if not is_on(s, "engineering_enabled"):
        return Gate(False, "Engineering investigations are switched off.")
    if ai_spend(s, month_start()) >= Decimal(get(s, "monthly_ai_budget_usd")):
        return Gate(False, "Monthly AI budget reached.", "monthly_budget")
    return Gate(True)
