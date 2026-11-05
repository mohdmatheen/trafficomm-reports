"""Usage & costs: application activity ledger, provider cost sync, exchange rates and reconciliation.

Three things are kept apart on purpose:
1. Application activity (ai_requests): recorded by this app the moment a model request finishes.
2. Provider-reported accrued cost (provider_cost_rows): read from the provider's reporting API. Delayed.
3. Billing (invoices, credits, taxes, payments): not available from any API integrated here.

Nothing in this module estimates a cost from token counts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, text

from .config import settings
from .db import (AIRequest, AppSetting, CredentialMapping, FxRate, ProviderCostRow, ProviderUsageRow, ReconciliationRun,
                 RenderInvoice, Session, SyncState, now)

log = logging.getLogger("usage")

CENT = Decimal("0.01")
ANTHROPIC_BETA = "analytics-usage-cost-2026-08-24"
SYNC_INTERVAL = dt.timedelta(minutes=int(os.environ.get("USAGE_SYNC_MINUTES", "60")))  # provider refreshes within ~4 h
MANUAL_SYNC_COOLDOWN = dt.timedelta(minutes=5)
FX_INTERVAL = dt.timedelta(hours=12)  # the source publishes once a day
RENDER_BILLING_URL = "https://dashboard.render.com/billing"
FX_SOURCE = "Frankfurter (central-bank reference rates, ECB-led)"
FX_URL = "https://api.frankfurter.dev/v2/rates?base=usd&quotes=inr"


# ---------------------------------------------------------------------------
# configuration (server-side only; never sent to browsers)
# ---------------------------------------------------------------------------
def _env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else d


def _csv(k):
    return [x.strip() for x in (_env(k, "") or "").split(",") if x.strip()]


def config() -> dict:
    return {
        "admin_key": bool(_env("ANTHROPIC_ADMIN_API_KEY")),
        "workspace_ids": _csv("ANTHROPIC_WORKSPACE_IDS"),
        "api_key_ids": _csv("ANTHROPIC_API_KEY_IDS"),
        "render_key": bool(_env("RENDER_API_KEY")),
        "render_service_ids": _csv("RENDER_SERVICE_IDS"),
        "fx_enabled": _env("USAGE_FX", "1") != "0",
    }


def credential_ref() -> str | None:
    """Identifier for the inference key: the configured provider key id, else a short non-reversible fingerprint."""
    ids = _csv("ANTHROPIC_API_KEY_IDS")
    if ids:
        return ids[0]
    k = settings.anthropic_api_key
    return ("fp:" + hashlib.sha256(k.encode()).hexdigest()[:10]) if k else None


def tracking_started(s) -> dt.datetime:
    r = s.get(AppSetting, "usage_tracking_started_at")
    if not r:
        r = AppSetting(key="usage_tracking_started_at", value=now().isoformat())
        s.add(r)
        s.flush()
    return dt.datetime.fromisoformat(r.value)


# ---------------------------------------------------------------------------
# 1. application activity ledger
# ---------------------------------------------------------------------------
@dataclass
class Attribution:
    initiated_by: str = "system"          # employee | system
    user_id: int | None = None
    user_name: str | None = None
    user_email: str | None = None
    task_id: int | None = None
    version_id: int | None = None
    version_number: int | None = None
    job_id: int | None = None
    client_name: str | None = None

    @classmethod
    def for_user(cls, user, **kw):
        return cls(initiated_by="employee", user_id=user.id, user_name=user.name, user_email=user.email, **kw)


def _notify(s, rid: int):
    try:
        if s.bind.dialect.name == "postgresql":
            s.execute(text("SELECT pg_notify('ai_usage', :p)"), {"p": str(rid)})
    except Exception:  # noqa: BLE001
        log.debug("notify failed", exc_info=True)


def record_start(attr: Attribution | None, step: str, model_requested: str, attempt: int, first_attempt_id: int | None) -> int | None:
    try:
        a = attr or Attribution()
        with Session() as s:
            r = AIRequest(step=step, model_requested=model_requested, attempt=attempt, first_attempt_id=first_attempt_id,
                          credential_ref=credential_ref(), workspace_ref=(_csv("ANTHROPIC_WORKSPACE_IDS") or [None])[0],
                          status="started", **asdict(a))
            s.add(r)
            s.flush()
            if first_attempt_id is None:
                r.first_attempt_id = r.id
            s.commit()
            return r.id
    except Exception:  # noqa: BLE001  - accounting must never break report processing
        log.exception("could not record AI request start")
        return None


def record_finish(rid: int | None, *, status: str, resp=None, error: Exception | None = None):
    if not rid:
        return
    try:
        with Session() as s:
            r = s.get(AIRequest, rid)
            r.completed_at, r.status = now(), status
            if error is not None:
                r.error_type = type(error).__name__[:80]
                r.provider_request_id = getattr(error, "request_id", None) or r.provider_request_id
            if resp is not None:
                r.provider_request_id = getattr(resp, "_request_id", None) or getattr(resp, "id", None)
                r.model = getattr(resp, "model", None)
                u = getattr(resp, "usage", None)
                if u is not None:
                    r.input_tokens = getattr(u, "input_tokens", None)
                    r.output_tokens = getattr(u, "output_tokens", None)
                    r.cache_read_tokens = getattr(u, "cache_read_input_tokens", None)
                    cc = getattr(u, "cache_creation", None)
                    if cc is not None:
                        r.cache_creation_5m_tokens = getattr(cc, "ephemeral_5m_input_tokens", None)
                        r.cache_creation_1h_tokens = getattr(cc, "ephemeral_1h_input_tokens", None)
                    elif getattr(u, "cache_creation_input_tokens", None) is not None:
                        r.cache_creation_5m_tokens = u.cache_creation_input_tokens
                    stu = getattr(u, "server_tool_use", None)
                    r.web_search_requests = getattr(stu, "web_search_requests", None) if stu is not None else None
                    r.service_tier = getattr(u, "service_tier", None)
            _notify(s, rid)
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not record AI request result")


def link_version(request_ids: list[int], version_id: int, version_number: int):
    """Feedback is interpreted before the revision exists; attach those requests to it afterwards."""
    if not request_ids:
        return
    with Session() as s:
        for r in s.scalars(select(AIRequest).where(AIRequest.id.in_(request_ids))):
            r.version_id, r.version_number = version_id, version_number
        s.commit()


# ---------------------------------------------------------------------------
# helpers: money and time
# ---------------------------------------------------------------------------
def cents_to_usd(v) -> Decimal:
    return (Decimal(str(v)) / Decimal(100)) if v is not None else Decimal(0)


def money(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_inr(usd: Decimal | None, rate: Decimal | None) -> Decimal | None:
    if usd is None or rate is None:
        return None
    return (usd * rate).quantize(CENT, rounding=ROUND_HALF_UP)  # convert the unrounded USD, round once


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _utc_day(d: dt.datetime) -> dt.datetime:
    d = d.astimezone(dt.timezone.utc)
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def rolling_window(at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Refetch the current month, and the previous month until the provider stops revising it (~7 days after month end)."""
    at = (at or now()).astimezone(dt.timezone.utc)
    month_start = dt.datetime(at.year, at.month, 1, tzinfo=dt.timezone.utc)
    if at.day <= 8:
        prev = month_start - dt.timedelta(days=1)
        start = dt.datetime(prev.year, prev.month, 1, tzinfo=dt.timezone.utc)
    else:
        start = month_start
    end = _utc_day(at) + dt.timedelta(days=1)
    return start, end


def _chunks(start, end, days=31):
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


# ---------------------------------------------------------------------------
# credential scope
# ---------------------------------------------------------------------------
def sync_env_mappings(s):
    """Mappings come from server env (ANTHROPIC_WORKSPACE_IDS / ANTHROPIC_API_KEY_IDS / RENDER_SERVICE_IDS)."""
    want = {("anthropic", "workspace", w) for w in _csv("ANTHROPIC_WORKSPACE_IDS")}
    want |= {("anthropic", "api_key", k) for k in _csv("ANTHROPIC_API_KEY_IDS")}
    want |= {("render", "render_service", k) for k in _csv("RENDER_SERVICE_IDS")}
    have = {(m.provider, m.kind, m.external_id): m for m in s.scalars(select(CredentialMapping).where(CredentialMapping.source == "env", CredentialMapping.effective_to.is_(None)))}
    for k in want - set(have):
        s.add(CredentialMapping(provider=k[0], kind=k[1], external_id=k[2], scope="app", source="env"))
    for k in set(have) - want:
        have[k].effective_to = now()


def _scope_for(group: dict, ws_ids: set, key_ids: set) -> str:
    """A row belongs to the app only via a verified mapping. A key filter is stricter than a workspace filter."""
    if key_ids:
        return "app" if group.get("api_key_id") in key_ids else ("excluded" if group.get("api_key_id") else "unmapped")
    if ws_ids:
        return "app" if group.get("workspace_id") in ws_ids else "excluded"
    return "unmapped"


# ---------------------------------------------------------------------------
# 2. provider sync (one coordinated process, advisory-locked)
# ---------------------------------------------------------------------------
LOCK_ID = 724117


def _state(s, source) -> SyncState:
    st = s.get(SyncState, source, with_for_update=True)
    if not st:
        st = SyncState(source=source, status="never", detail={})
        s.add(st)
        s.flush()
    return st


def _backoff(st: SyncState, err: Exception):
    st.failures = (st.failures or 0) + 1
    st.status = "error"
    name = type(err).__name__
    code = getattr(err, "status_code", None)
    if code in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        st.status, st.last_error = "auth_error", "The reporting key was rejected (expired, revoked or missing permission)."
    elif code == 429 or name == "RateLimitError":
        st.last_error = "Provider rate limit reached; backing off."
    else:
        st.last_error = f"{name}{f' ({code})' if code else ''}"[:300]
    retry_after = None
    try:
        retry_after = int(getattr(getattr(err, "response", None), "headers", {}).get("retry-after"))
    except (TypeError, ValueError):
        pass
    wait = retry_after or min(60 * 60 * 6, 60 * (2 ** min(st.failures, 8)))
    st.next_allowed_at = now() + dt.timedelta(seconds=wait)


def _admin_client():
    import anthropic
    return anthropic.Anthropic(api_key=_env("ANTHROPIC_ADMIN_API_KEY"), max_retries=2, timeout=60,
                               default_headers={"User-Agent": "TrafficommReportingWorkspace/1.0 (usage-sync)"})


def _replace_rows(s, model, provider, start, end, rows: list[dict]):
    """Rows in a fetched window replace what was stored for that window: corrections update, nothing accumulates."""
    keys = {r["dedup_key"] for r in rows}
    existing = {r.dedup_key: r for r in s.scalars(select(model).where(model.provider == provider, model.bucket_start >= start, model.bucket_start < end))}
    for k, obj in existing.items():
        if k not in keys:
            s.delete(obj)
    for r in rows:
        obj = existing.get(r["dedup_key"])
        if obj is None:
            s.add(model(provider=provider, **r))
        else:
            for f, v in r.items():
                setattr(obj, f, v)


def fetch_anthropic(start, end, client=None) -> dict:
    """Returns {'cost': [...], 'usage': [...], 'data_refreshed_at': dt|None}. Paginates every page."""
    c = client or _admin_client()
    an = c.beta.organization.analytics
    ws, keys = _csv("ANTHROPIC_WORKSPACE_IDS"), _csv("ANTHROPIC_API_KEY_IDS")
    out = {"cost": [], "usage": [], "data_refreshed_at": None}
    retrieved = now()
    for a, b in _chunks(start, end):
        common = dict(starting_at=a, ending_at=b, time_bucket_width="1d", limit=1000)
        page = an.cost.list(group_by=["workspace_id", "api_key_id", "model", "service_tier", "cost_type", "token_type"], **common)
        refreshed = getattr(page, "data_refreshed_at", None)
        for it in page:  # SDK auto-paginates with the same snapshot
            g = it.group.model_dump() if it.group else {}
            out["cost"].append({
                "dedup_key": _key("anthropic", "cost", it.starting_at, it.ending_at, g),
                "bucket_start": it.starting_at, "bucket_end": it.ending_at,
                "workspace_id": g.get("workspace_id"), "api_key_id": g.get("api_key_id"), "model": g.get("model"),
                "service_tier": g.get("service_tier"), "cost_type": g.get("cost_type"), "token_type": g.get("token_type"),
                "amount": Decimal(str(it.cost.amount)), "list_amount": Decimal(str(it.cost.list_amount)) if it.cost.list_amount is not None else None,
                "amount_unit": "USD_cents", "currency": it.cost.currency or "USD",
                "scope": _scope_for(g, set(ws), set(keys)), "data_refreshed_at": refreshed, "retrieved_at": retrieved,
                "raw_group": {k: v for k, v in g.items() if v is not None},
            })
        upage = an.usage.list(group_by=["workspace_id", "api_key_id", "model"], **common)
        refreshed_u = getattr(upage, "data_refreshed_at", None)
        for it in upage:
            g = it.group.model_dump() if it.group else {}
            u = it.usage
            out["usage"].append({
                "dedup_key": _key("anthropic", "usage", it.starting_at, it.ending_at, g),
                "bucket_start": it.starting_at, "bucket_end": it.ending_at,
                "workspace_id": g.get("workspace_id"), "api_key_id": g.get("api_key_id"), "model": g.get("model"),
                "scope": _scope_for(g, set(ws), set(keys)), "requests": u.requests,
                "uncached_input_tokens": u.uncached_input_tokens, "output_tokens": u.output_tokens,
                "cache_read_tokens": u.cache_read_input_tokens,
                "cache_creation_5m_tokens": u.cache_creation.ephemeral_5m_input_tokens if u.cache_creation else 0,
                "cache_creation_1h_tokens": u.cache_creation.ephemeral_1h_input_tokens if u.cache_creation else 0,
                "web_search_requests": u.server_tool_use.web_search_requests if u.server_tool_use else 0,
                "data_refreshed_at": refreshed_u, "retrieved_at": retrieved,
            })
        rts = [x for x in (refreshed, refreshed_u) if x]
        if rts:
            m = min(rts)
            out["data_refreshed_at"] = m if out["data_refreshed_at"] is None else min(out["data_refreshed_at"], m)
    return out


def fetch_fx():
    import httpx
    r = httpx.get(FX_URL, timeout=20, headers={"User-Agent": "TrafficommReportingWorkspace/1.0"})
    r.raise_for_status()
    j = r.json()
    # The v2 endpoint returns a list of {date, base, quote, rate}; older shapes use {"date", "rates": {"INR": x}}.
    rows = j if isinstance(j, list) else j.get("data") or j.get("rates") or j
    if isinstance(rows, list):
        row = next(x for x in rows if str(x.get("quote", "")).upper() == "INR")
        return Decimal(str(row["rate"])), str(row.get("date"))
    if isinstance(rows, dict) and "INR" in {k.upper() for k in rows}:
        return Decimal(str(rows.get("INR") or rows.get("inr"))), str(j.get("date"))
    raise ValueError("Unrecognised FX response shape")


def fetch_render_services():
    """Supported Render data: service metadata only. Render's public API has no billing endpoints."""
    import httpx
    out = []
    for sid in _csv("RENDER_SERVICE_IDS"):
        r = httpx.get(f"https://api.render.com/v1/services/{sid}", timeout=20,
                      headers={"Authorization": f"Bearer {_env('RENDER_API_KEY')}", "Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
        det = j.get("serviceDetails") or {}
        out.append({"id": j.get("id"), "name": j.get("name"), "type": j.get("type"), "suspended": j.get("suspended"),
                    "plan": det.get("plan"), "region": det.get("region")})
    return out


def sync_all(manual: bool = False, fetchers: dict | None = None) -> dict:
    """One coordinated sync. Concurrent callers return immediately while another sync holds the lock."""
    f = {"anthropic": fetch_anthropic, "fx": fetch_fx, "render": fetch_render_services, **(fetchers or {})}
    cfg = config()
    summary = {}
    with Session() as s:
        if s.bind.dialect.name == "postgresql":
            got = s.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID}).scalar()
            if not got:
                return {"skipped": "another sync is running"}
        try:
            tracking_started(s)
            sync_env_mappings(s)
            s.commit()
            # --- Claude
            st = _state(s, "anthropic")
            if not cfg["admin_key"]:
                st.status, st.last_error = "not_configured", None
                summary["anthropic"] = "not_configured"
            elif st.next_allowed_at and st.next_allowed_at > now() and (not manual or st.status in ("error", "auth_error")):
                summary["anthropic"] = "waiting"
            else:
                st.last_attempt_at = now()
                start, end = rolling_window()
                s.commit()
                try:
                    data = f["anthropic"](start, end)
                    st = _state(s, "anthropic")
                    _replace_rows(s, ProviderCostRow, "anthropic", start, end, data["cost"])
                    _replace_rows(s, ProviderUsageRow, "anthropic", start, end, data["usage"])
                    st.status, st.failures, st.last_error = "ok", 0, None
                    st.last_success_at = now()
                    st.data_refreshed_at = data["data_refreshed_at"]
                    first = s.scalar(select(func.min(ProviderCostRow.bucket_start)).where(ProviderCostRow.provider == "anthropic"))
                    st.coverage_from = min(x for x in (first, start) if x)
                    st.coverage_to = min(end, data["data_refreshed_at"]) if data["data_refreshed_at"] else None
                    st.next_allowed_at = now() + (MANUAL_SYNC_COOLDOWN if manual else SYNC_INTERVAL)
                    st.detail = {"window": [start.isoformat(), end.isoformat()], "cost_rows": len(data["cost"]), "usage_rows": len(data["usage"]),
                                 "scope_basis": "api_key" if cfg["api_key_ids"] else ("workspace" if cfg["workspace_ids"] else "none")}
                    summary["anthropic"] = "ok"
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    st = _state(s, "anthropic")
                    _backoff(st, e)
                    summary["anthropic"] = st.status
                    log.warning("anthropic usage sync failed: %s", st.last_error)
            s.commit()
            # --- FX
            st = _state(s, "fx")
            if not cfg["fx_enabled"]:
                st.status = "not_configured"
            elif st.last_success_at and now() - st.last_success_at < FX_INTERVAL and not manual:
                pass
            elif st.next_allowed_at and st.next_allowed_at > now():
                pass
            else:
                st.last_attempt_at = now()
                try:
                    rate, day = f["fx"]()
                    existing = s.scalar(select(FxRate).where(FxRate.base == "USD", FxRate.quote == "INR", FxRate.source == FX_SOURCE, FxRate.rate_date == day))
                    if existing:
                        existing.rate, existing.retrieved_at = rate, now()
                    else:
                        s.add(FxRate(base="USD", quote="INR", rate=rate, source=FX_SOURCE, rate_date=day))
                    st.status, st.failures, st.last_error, st.last_success_at = "ok", 0, None, now()
                    st.next_allowed_at = None
                except Exception as e:  # noqa: BLE001
                    _backoff(st, e)
            summary["fx"] = st.status
            s.commit()
            # --- Render (metadata only; billing unsupported by the public API)
            st = _state(s, "render")
            if not (cfg["render_key"] and cfg["render_service_ids"]):
                st.status = "not_configured"
            elif not (st.next_allowed_at and st.next_allowed_at > now()):
                st.last_attempt_at = now()
                try:
                    st.detail = {"services": f["render"]()}
                    st.status, st.failures, st.last_error, st.last_success_at = "ok", 0, None, now()
                    st.next_allowed_at = now() + dt.timedelta(hours=6)
                except Exception as e:  # noqa: BLE001
                    _backoff(st, e)
            summary["render"] = st.status
            s.commit()
            reconcile(s)
            s.commit()
        finally:
            if s.bind.dialect.name == "postgresql":
                s.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                s.commit()
    return summary


def request_manual_sync() -> dict:
    """Debounced: at most one manual sync per cooldown, run in the background."""
    with Session() as s:
        st = s.get(SyncState, "manual")
        if st and st.last_attempt_at and now() - st.last_attempt_at < MANUAL_SYNC_COOLDOWN:
            wait = int((st.last_attempt_at + MANUAL_SYNC_COOLDOWN - now()).total_seconds())
            return {"started": False, "message": f"A sync ran recently. Try again in {max(1, wait // 60 + (1 if wait % 60 else 0))} min."}
        if not st:
            st = SyncState(source="manual", status="ok", detail={})
            s.add(st)
        st.last_attempt_at = now()
        s.commit()
    threading.Thread(target=lambda: sync_all(manual=True), daemon=True, name="usage-sync").start()
    return {"started": True, "message": "Sync started. Provider data appears when the provider has published it."}


def maybe_scheduled_sync():
    """Called from the worker loop; the provider poll interval is enforced by SyncState.next_allowed_at."""
    with Session() as s:
        st = s.get(SyncState, "anthropic")
        due = not st or not st.next_allowed_at or st.next_allowed_at <= now()
        fx = s.get(SyncState, "fx")
        fx_due = not fx or not fx.last_success_at or now() - fx.last_success_at >= FX_INTERVAL
    if due or fx_due:
        sync_all()


# ---------------------------------------------------------------------------
# 3. reconciliation and dashboard aggregation
# ---------------------------------------------------------------------------
# ------------------------------------------------------------------ Render invoices (entered by an admin)
INVOICE_MAX = Decimal("1000000")


class InvoiceError(ValueError):
    pass


def _month(m: str) -> str:
    try:
        d = dt.date.fromisoformat((m or "").strip()[:7] + "-01")
    except ValueError:
        raise InvoiceError("Choose the invoice month.")
    if d < dt.date(2020, 1, 1) or d > now().date().replace(day=1):
        raise InvoiceError("The invoice month can't be in the future.")
    return d.strftime("%Y-%m")


def set_render_invoice(s, admin, month: str, amount, note: str = "") -> RenderInvoice:
    m = _month(month)
    try:
        amt = Decimal(str(amount).replace(",", "").replace("$", "").strip())
    except Exception:  # noqa: BLE001
        raise InvoiceError("Enter the invoice total in USD, for example 32.22.")
    if not amt.is_finite() or amt < 0 or amt >= INVOICE_MAX:
        raise InvoiceError("Enter the invoice total in USD, for example 32.22.")
    if amt != amt.quantize(CENT):
        raise InvoiceError("Use at most 2 decimal places.")
    inv = s.scalar(select(RenderInvoice).where(RenderInvoice.month == m)) or RenderInvoice(month=m)
    inv.amount_usd, inv.note = amt, (note or "").strip()[:300]
    inv.entered_by, inv.entered_by_name, inv.entered_at = admin.id, admin.name or admin.email, now()
    s.add(inv)
    return inv


def delete_render_invoice(s, month: str) -> bool:
    inv = s.scalar(select(RenderInvoice).where(RenderInvoice.month == _month(month)))
    if inv:
        s.delete(inv)
    return bool(inv)


def _months_between(a: dt.date, b: dt.date) -> list[str]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _inv(i) -> dict:
    return {"month": i.month, "amount_usd": Decimal(str(i.amount_usd)), "note": i.note or "", "entered_by_name": i.entered_by_name,
            "entered_at": i.entered_at}


def render_cost(s, r: dict) -> dict:
    """Invoice totals entered by an admin, for the calendar months the period touches. Never prorated."""
    months = _months_between(r["from"], r["to"])
    invs = {i.month: _inv(i) for i in s.scalars(select(RenderInvoice).where(RenderInvoice.month.in_(months)))}
    entered = [invs[m] for m in months if m in invs]
    missing = [m for m in months if m not in invs]
    last = r["to"].replace(day=28) + dt.timedelta(days=4)
    whole = r["from"].day == 1 and (last - dt.timedelta(days=last.day)) == r["to"]
    return {"months": months, "entered": entered, "missing": missing, "whole_months": whole,
            "usd": sum((i["amount_usd"] for i in entered), Decimal(0)) if entered else None}


def recent_render_invoices(s, limit=12):
    return [_inv(i) for i in s.scalars(select(RenderInvoice).order_by(RenderInvoice.month.desc()).limit(limit))]


def latest_fx(s):
    return s.scalar(select(FxRate).where(FxRate.base == "USD", FxRate.quote == "INR").order_by(FxRate.retrieved_at.desc()))


def reconcile(s, start=None, end=None) -> dict:
    """Compare app-recorded tokens with provider-reported in-scope tokens for days the provider has fully refreshed."""
    st = s.get(SyncState, "anthropic")
    if not st or not st.data_refreshed_at:
        return {}
    start = start or (st.coverage_from or tracking_started(s))
    end = end or _utc_day(st.data_refreshed_at)
    if end <= start:
        return {}
    prov = s.execute(select(func.coalesce(func.sum(ProviderUsageRow.uncached_input_tokens + ProviderUsageRow.cache_read_tokens + ProviderUsageRow.cache_creation_5m_tokens + ProviderUsageRow.cache_creation_1h_tokens), 0),
                            func.coalesce(func.sum(ProviderUsageRow.output_tokens), 0), func.coalesce(func.sum(ProviderUsageRow.requests), 0))
                     .where(ProviderUsageRow.scope == "app", ProviderUsageRow.bucket_start >= start, ProviderUsageRow.bucket_start < end)).one()
    app = s.execute(select(func.coalesce(func.sum(func.coalesce(AIRequest.input_tokens, 0) + func.coalesce(AIRequest.cache_read_tokens, 0) + func.coalesce(AIRequest.cache_creation_5m_tokens, 0) + func.coalesce(AIRequest.cache_creation_1h_tokens, 0)), 0),
                           func.coalesce(func.sum(AIRequest.output_tokens), 0), func.count(AIRequest.id))
                    .where(AIRequest.status == "succeeded", AIRequest.completed_at >= start, AIRequest.completed_at < end)).one()
    res = {"period": [start.isoformat(), end.isoformat()],
           "provider": {"input_tokens": int(prov[0]), "output_tokens": int(prov[1]), "requests": int(prov[2])},
           "app": {"input_tokens": int(app[0]), "output_tokens": int(app[1]), "requests": int(app[2])}}
    res["difference"] = {k: res["provider"][k] - res["app"][k] for k in res["app"]}
    s.add(ReconciliationRun(period_start=start, period_end=end, result=res))
    return res


RANGES = {"today": "Today", "month": "This month", "all": "Since tracking began", "custom": "Custom"}


def resolve_range(s, rng: str, start_s: str = "", end_s: str = "", tz="Asia/Kolkata"):
    """Display ranges are IST calendar days; provider buckets are UTC days, so provider figures cover the UTC days overlapping the range."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    today = now().astimezone(z).date()
    if rng == "today":
        a, b = today, today
    elif rng == "custom":
        try:
            a, b = dt.date.fromisoformat(start_s), dt.date.fromisoformat(end_s)
        except ValueError:
            a, b = today.replace(day=1), today
        if b < a:
            a, b = b, a
    elif rng == "all":
        a, b = tracking_started(s).astimezone(z).date(), today
    else:
        rng = "month"
        a, b = today.replace(day=1), today
    start = dt.datetime(a.year, a.month, a.day, tzinfo=z).astimezone(dt.timezone.utc)
    end = dt.datetime(b.year, b.month, b.day, tzinfo=z).astimezone(dt.timezone.utc) + dt.timedelta(days=1)
    pstart, pend = _utc_day(start), _utc_day(end - dt.timedelta(seconds=1)) + dt.timedelta(days=1)
    return {"key": rng, "label": RANGES[rng], "start": start, "end": end, "from": a, "to": b, "provider_start": pstart, "provider_end": pend, "provider_last": pend - dt.timedelta(days=1)}


def summary(s, r: dict, user_id: int | None = None, client: str | None = None) -> dict:
    cfg = config()
    sts = {x.source: x for x in s.scalars(select(SyncState))}
    fx = latest_fx(s)
    rate = Decimal(str(fx.rate)) if fx else None
    an = sts.get("anthropic")

    # --- provider-confirmed Claude cost (in-scope rows only), UTC day buckets overlapping the range
    claude = {"status": "not_configured", "usd": None, "unmapped_usd": None, "excluded_usd": None, "by_type": [], "by_model": []}
    if cfg["admin_key"] and an and an.last_success_at:
        q = select(ProviderCostRow.scope, ProviderCostRow.cost_type, ProviderCostRow.model, func.sum(ProviderCostRow.amount)).where(
            ProviderCostRow.provider == "anthropic", ProviderCostRow.bucket_start >= r["provider_start"], ProviderCostRow.bucket_start < r["provider_end"]
        ).group_by(ProviderCostRow.scope, ProviderCostRow.cost_type, ProviderCostRow.model)
        tot = {"app": Decimal(0), "excluded": Decimal(0), "unmapped": Decimal(0)}
        bt, bm = {}, {}
        for scope, ctype, model, amt in s.execute(q):
            usd = cents_to_usd(amt)
            tot[scope] = tot.get(scope, Decimal(0)) + usd
            if scope == "app":
                bt[ctype or "other"] = bt.get(ctype or "other", Decimal(0)) + usd
                bm[model or "No model (tool or service charge)"] = bm.get(model or "No model (tool or service charge)", Decimal(0)) + usd
        scoped = bool(cfg["workspace_ids"] or cfg["api_key_ids"])
        claude.update(status="ok" if scoped else "unmapped", usd=tot["app"] if scoped else None,
                      unmapped_usd=tot["unmapped"], excluded_usd=tot["excluded"],
                      by_type=sorted(bt.items(), key=lambda x: -x[1]), by_model=sorted(bm.items(), key=lambda x: -x[1]))
        stale = an.status != "ok" or (an.last_success_at and now() - an.last_success_at > SYNC_INTERVAL * 3)
        claude["stale"] = bool(stale)
        claude["data_through"] = an.data_refreshed_at
        incomplete_tail = an.data_refreshed_at is None or an.data_refreshed_at < r["provider_end"]
        claude["partial_period"] = bool(incomplete_tail)
    elif cfg["admin_key"]:
        claude["status"] = (an.status if an else "never")

    # --- Render: no billing API
    rc = render_cost(s, r)
    render = {"status": "manual" if rc["usd"] is not None else "awaiting_entry", **rc, "invoices": recent_render_invoices(s), "services": (sts.get("render").detail or {}).get("services", []) if sts.get("render") else [],
              "metadata_status": sts.get("render").status if sts.get("render") else "not_configured", "billing_url": RENDER_BILLING_URL}

    # --- combined: only complete when every cost source is present, scoped and on the same basis
    parts = [p for p in (claude["usd"], render["usd"]) if p is not None]
    confirmed = sum(parts, Decimal(0)) if parts else None
    # complete only when Claude is confirmed for the whole period and every month has a Render invoice
    # covering exactly the period (invoices are monthly and never prorated)
    complete = bool(claude["usd"] is not None and not claude.get("partial_period") and not claude.get("stale")
                    and render["usd"] is not None and not render["missing"] and render["whole_months"])
    combined = {"usd": confirmed, "complete": complete,
                "label": ("Confirmed total" if complete else "Confirmed subtotal — incomplete coverage") if confirmed is not None else "Awaiting source",
                "has_claude": claude["usd"] is not None, "has_render": render["usd"] is not None}

    # --- application activity (exact, recorded by this app)
    rq = select(AIRequest).where(AIRequest.started_at >= r["start"], AIRequest.started_at < r["end"])
    if user_id:
        rq = rq.where(AIRequest.user_id == user_id)
    if client:
        rq = rq.where(AIRequest.client_name == client)
    reqs = s.scalars(rq).all()
    emp: dict = {}
    system = {"attempts": 0, "succeeded": 0, "tokens": 0}
    for x in reqs:
        tok = sum(v or 0 for v in (x.input_tokens, x.output_tokens, x.cache_read_tokens, x.cache_creation_5m_tokens, x.cache_creation_1h_tokens))
        if x.initiated_by != "employee" or not x.user_id:
            system["attempts"] += 1
            system["succeeded"] += x.status == "succeeded"
            system["tokens"] += tok
            continue
        e = emp.setdefault(x.user_id, {"user_id": x.user_id, "name": x.user_name, "email": x.user_email, "tasks": set(), "versions": set(),
                                       "attempts": 0, "succeeded": 0, "failed": 0, "retries": 0, "input": 0, "output": 0, "cache_read": 0,
                                       "cache_write": 0, "web_search": 0, "pending": 0, "last": None, "models": set()})
        e["name"] = x.user_name or e["name"]
        e["attempts"] += 1
        e["retries"] += x.attempt > 1
        if x.task_id:
            e["tasks"].add(x.task_id)
        if x.version_id:
            e["versions"].add(x.version_id)
        if x.status == "succeeded":
            e["succeeded"] += 1
            if x.model:
                e["models"].add(x.model)
        elif x.status in ("failed", "refused"):
            e["failed"] += 1
        e["input"] += x.input_tokens or 0
        e["output"] += x.output_tokens or 0
        e["cache_read"] += x.cache_read_tokens or 0
        e["cache_write"] += (x.cache_creation_5m_tokens or 0) + (x.cache_creation_1h_tokens or 0)
        e["web_search"] += x.web_search_requests or 0
        if x.status == "succeeded" and (not an or not an.data_refreshed_at or x.completed_at > an.data_refreshed_at):
            e["pending"] += 1
        e["last"] = max(filter(None, [e["last"], x.completed_at or x.started_at]))
    rows = sorted(emp.values(), key=lambda e: (-e["attempts"], e["name"] or ""))
    for e in rows:
        e["tasks"], e["versions"], e["models"] = len(e["tasks"]), len(e["versions"]), sorted(e["models"])
        e["tokens"] = e["input"] + e["output"] + e["cache_read"] + e["cache_write"]
    totals = {k: sum(e[k] for e in rows) for k in ("attempts", "succeeded", "failed", "tokens", "pending", "input", "output", "cache_read", "cache_write", "web_search")}
    totals["tasks"] = len({x.task_id for x in reqs if x.task_id and x.initiated_by == "employee"})
    last_activity = s.scalar(select(func.max(AIRequest.completed_at)))
    last_recon = s.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()))
    return {"cfg": cfg, "states": sts, "fx": fx, "rate": rate, "claude": claude, "render": render, "combined": combined,
            "employees": rows, "totals": totals, "system": system, "last_activity": last_activity, "recon": last_recon,
            "tracking_started": tracking_started(s), "attribution": "aggregate_only"}


def employee_detail(s, uid: int, r: dict) -> dict:
    reqs = s.scalars(select(AIRequest).where(AIRequest.user_id == uid, AIRequest.started_at >= r["start"], AIRequest.started_at < r["end"])
                     .order_by(AIRequest.started_at.desc()).limit(500)).all()
    tasks: dict = {}
    for x in reqs:
        t = tasks.setdefault(x.task_id, {"task_id": x.task_id, "client": x.client_name, "versions": set(), "attempts": 0, "succeeded": 0, "tokens": 0, "steps": set()})
        t["attempts"] += 1
        t["succeeded"] += x.status == "succeeded"
        t["tokens"] += sum(v or 0 for v in (x.input_tokens, x.output_tokens, x.cache_read_tokens, x.cache_creation_5m_tokens, x.cache_creation_1h_tokens))
        if x.version_number:
            t["versions"].add(x.version_number)
        t["steps"].add(x.step)
    for t in tasks.values():
        t["versions"], t["steps"] = sorted(t["versions"]), sorted(t["steps"])
    return {"requests": reqs, "tasks": sorted(tasks.values(), key=lambda t: -(t["task_id"] or 0))}
