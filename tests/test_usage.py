"""Usage & costs (INTEGRATION FIXTURES: provider responses are faked; no live Anthropic, Render or FX calls).

Live-provider verification is separate and documented in docs/usage-costs.md.
"""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app import usage as U
from app.db import (AIRequest, Base, Client, ProviderCostRow, Session, SyncState, Task, TaskVersion, User, engine, now,
                    reset_engine)
from tests import zoho_fake

pytestmark = pytest.mark.web
H = {"X-Requested-With": "fetch"}


@pytest.fixture(scope="module")
def mp():
    m = pytest.MonkeyPatch()
    zoho_fake.configure(m)
    yield m
    m.undo()


@pytest.fixture(scope="module")
def app(mp):
    reset_engine()
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    from scripts.seed import main as seed
    seed()
    from app.web import app as a
    return a


def login(app, mp, email):
    c, r, _ = zoho_fake.sign_in(app, mp, email)
    assert r.status_code == 303
    return c


@pytest.fixture(scope="module")
def people(app, mp):
    boss = login(app, mp, "matheen.m@trafficomm.com")
    for e in ("owner.one@trafficomm.com", "reviser.two@trafficomm.com"):
        assert boss.post("/api/admin/users", headers=H, json={"email": e}).status_code == 200
    with Session() as s:
        ids = {u.email: u.id for u in s.scalars(select(User))}
    return boss, ids


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeMessages:
    def __init__(self, script):
        self.script = list(script)

    def parse(self, **kw):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def fake_resp(model="claude-opus-5-5", inp=1000, out=200, cread=500, c5=0, c1=0, rid="req_x"):
    return NS(model=model, _request_id=rid, stop_reason="end_turn", parsed_output="ok",
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cread,
                       cache_creation=NS(ephemeral_5m_input_tokens=c5, ephemeral_1h_input_tokens=c1),
                       server_tool_use=None, service_tier="standard"))


def provider_with(script):
    from app.engine import ai
    p = ai.AnthropicProvider.__new__(ai.AnthropicProvider)
    p.client = NS(messages=FakeMessages(script))
    p.model, p.name, p.attribution, p.request_ids, p.max_attempts = "claude-opus-5-5", "fake", None, [], 3
    return p


def conn_error():
    import anthropic
    import httpx
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


# ---------------------------------------------------------------------------
def test_admin_only(app, mp, people):
    boss, ids = people
    emp = login(app, mp, "owner.one@trafficomm.com")
    for url in ("/admin/usage", f"/admin/usage/employees/{ids['owner.one@trafficomm.com']}", "/admin/usage/export.csv", "/api/admin/usage/stream",
                "/admin/usage?partial=activity"):
        assert emp.get(url).status_code == 403, url
    assert emp.post("/api/admin/usage/sync", headers=H).status_code == 403
    assert boss.get("/admin/usage").status_code == 200
    # employees never receive cost markup
    assert "Confirmed Claude cost" not in emp.get("/").text


def test_retries_are_linked_and_counted_once(app, people, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _, ids = people
    p = provider_with([conn_error(), fake_resp(rid="req_retry_ok")])
    with Session() as s:
        u = s.get(User, ids["owner.one@trafficomm.com"])
    p.attribution = U.Attribution.for_user(u, task_id=None)
    assert p._parse(str, "x", step="instructions") == "ok"
    with Session() as s:
        rows = s.scalars(select(AIRequest).where(AIRequest.id.in_(p.request_ids)).order_by(AIRequest.id)).all()
    assert [r.status for r in rows] == ["failed", "succeeded"]
    assert rows[1].attempt == 2 and rows[1].first_attempt_id == rows[0].id == rows[0].first_attempt_id
    assert rows[1].provider_request_id == "req_retry_ok" and rows[1].model == "claude-opus-5-5"
    assert rows[1].input_tokens == 1000 and rows[1].cache_read_tokens == 500
    assert rows[0].input_tokens is None  # a failed attempt carries no usage
    with Session() as s:
        r = U.resolve_range(s, "month")
        e = next(x for x in U.summary(s, r)["employees"] if x["user_id"] == u.id)
    assert e["attempts"] == 2 and e["succeeded"] == 1 and e["retries"] == 1
    assert e["tokens"] == 1000 + 200 + 500  # cache reads counted once, as returned


def test_revision_attributed_to_reviser_not_owner(app, people, monkeypatch):
    from app import service as S
    from app.engine import ai
    _, ids = people
    owner, reviser = ids["owner.one@trafficomm.com"], ids["reviser.two@trafficomm.com"]
    with Session() as s:
        c = s.scalar(select(Client))
        t = Task(client_id=c.id, owner_id=owner, title="Attribution test", status="ready_for_review", deliverables=["excel_consolidated"])
        s.add(t)
        s.flush()
        v1 = TaskVersion(task_id=t.id, number=1, created_by=owner, status="ready_for_review", stage="done")
        s.add(v1)
        s.flush()
        t.current_version_id = v1.id
        s.commit()
        tid = t.id

    class Fake(ai.RuleProvider):
        def interpret_feedback(self, feedback, has_new_files):
            rid = U.record_start(self.attribution, "feedback", "claude-opus-5-5", 1, None)
            self.request_ids = [rid]
            U.record_finish(rid, status="succeeded", resp=fake_resp(rid="req_rev"))
            return super().interpret_feedback(feedback, has_new_files)

    monkeypatch.setattr(ai, "get_provider", lambda: Fake())
    with Session() as s:
        t = s.get(Task, tid)
        v, _ = S.create_revision(s, t, s.get(User, reviser), "Split tabs by country", None, [])
        s.commit()
        vid, vnum = v.id, v.number
    with Session() as s:
        r = s.scalar(select(AIRequest).where(AIRequest.provider_request_id == "req_rev"))
    assert r.user_id == reviser and r.user_id != owner and r.initiated_by == "employee"
    assert r.task_id == tid and r.version_id == vid and r.version_number == vnum == 2

    # the background job for that revision carries the reviser too (worker side)
    seen = {}

    def fake_run(inp, stage, check_cancel=lambda: None, provider=None):
        seen["attr"] = provider.attribution
        return {"status": "ready_for_review", "questions": [], "artifacts": []}

    monkeypatch.setattr(S, "run_version", fake_run)
    monkeypatch.setattr(S, "persist_result", lambda *a, **k: None)
    from app.db import Job
    with Session() as s:
        j = s.scalar(select(Job).where(Job.version_id == vid))
        jid = j.id
    S.process_job(jid)
    a = seen["attr"]
    assert a.user_id == reviser and a.job_id == jid and a.version_id == vid and a.initiated_by == "employee"

    # history survives renaming the account and deleting the task under retention
    with Session() as s:
        s.get(User, reviser).name = "Renamed Person"
        s.get(Task, tid).deleted_at = now()
        s.commit()
    with Session() as s:
        r = s.scalar(select(AIRequest).where(AIRequest.provider_request_id == "req_rev"))
        assert r.user_name != "Renamed Person" and r.task_id == tid  # snapshot kept
        e = [x for x in U.summary(s, U.resolve_range(s, "month"))["employees"] if x["user_id"] == reviser]
    assert e and e[0]["attempts"] >= 1


# ---------------------------------------------------------------------------
def _cost(day, amount_cents, ws="wrkspc_app", key="apikey_app", model="claude-opus-5-5", ctype="tokens", ttype="output_tokens"):
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    g = {"workspace_id": ws, "api_key_id": key, "model": model, "service_tier": "standard", "cost_type": ctype, "token_type": ttype}
    return {"dedup_key": U._key("anthropic", "cost", start, g), "bucket_start": start, "bucket_end": start + dt.timedelta(days=1),
            "workspace_id": ws, "api_key_id": key, "model": model, "service_tier": "standard", "cost_type": ctype, "token_type": ttype,
            "amount": Decimal(amount_cents), "list_amount": None, "amount_unit": "USD_cents", "currency": "USD",
            "scope": U._scope_for(g, {"wrkspc_app"}, set()), "data_refreshed_at": now(), "retrieved_at": now(), "raw_group": g}


def test_sync_is_idempotent_corrects_late_rows_and_separates_other_workspaces(app, people, monkeypatch):
    boss, _ = people
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_IDS", "wrkspc_app")
    today = now().date()
    batch = {"rows": [_cost(today, "3000.000000"), _cost(today, "1680.000000", ttype="uncached_input_tokens"),
                      _cost(today, "120.000000", model=None, ctype="web_search", ttype=None),
                      _cost(today, "99900.000000", ws="wrkspc_other_app")]}

    def fake_anthropic(start, end):
        return {"cost": [dict(r) for r in batch["rows"]], "usage": [], "data_refreshed_at": now()}

    fx = lambda: (Decimal("85.1234"), str(today))  # noqa: E731
    f = {"anthropic": fake_anthropic, "fx": fx}
    monkeypatch.setenv("USAGE_FX", "1")
    U.sync_all(manual=True, fetchers=f)

    def claude():
        with Session() as s:
            return U.summary(s, U.resolve_range(s, "month"))["claude"]

    c1 = claude()
    assert c1["usd"] == Decimal("48.00")  # 3000 + 1680 + 120 cents; other workspace excluded
    assert c1["excluded_usd"] == Decimal("999.00")
    assert dict(c1["by_type"])["web_search"] == Decimal("1.20")  # tool charges kept, not assumed to be tokens
    # repeated syncs leave totals unchanged
    with Session() as s:
        s.get(SyncState, "anthropic").next_allowed_at = None
        s.commit()
    U.sync_all(manual=True, fetchers=f)
    assert claude()["usd"] == Decimal("48.00")
    with Session() as s:
        assert s.query(ProviderCostRow).count() == 4
    # a late provider correction replaces the stored value; a row the provider no longer reports disappears
    batch["rows"][0] = _cost(today, "3500.000000")
    batch["rows"].pop(2)
    with Session() as s:
        s.get(SyncState, "anthropic").next_allowed_at = None
        s.commit()
    U.sync_all(manual=True, fetchers=f)
    assert claude()["usd"] == Decimal("51.80")
    # INR is the recorded rate times unrounded USD, rounded once
    page = boss.get("/admin/usage").text
    assert "$51.80" in page and "₹4,409.39" in page and "85.1234" in page  # 51.80 * 85.1234 = 4409.39212
    assert U.to_inr(Decimal("51.80"), Decimal("85.1234")) == Decimal("4409.39")
    # missing Render billing keeps the combined figure incomplete
    assert "Confirmed subtotal — incomplete coverage" in page and "Render excluded" in page
    # employee money stays unavailable: one shared key can't be split per person
    assert "Why employee cost is unavailable" in page


def test_backoff_and_stale_data_are_visible(app, people, monkeypatch):
    boss, _ = people
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_IDS", "wrkspc_app")

    class Limited(Exception):
        status_code = 429

    with Session() as s:
        st = s.get(SyncState, "anthropic")
        st.next_allowed_at = None
        st.last_success_at = now() - dt.timedelta(days=2)
        s.commit()
    U.sync_all(manual=True, fetchers={"anthropic": lambda a, b: (_ for _ in ()).throw(Limited()), "fx": lambda: (Decimal("85"), "2026-11-05")})
    with Session() as s:
        st = s.get(SyncState, "anthropic")
        assert st.status == "error" and st.next_allowed_at > now() and "rate limit" in st.last_error
    page = boss.get("/admin/usage").text
    assert "Stale" in page and "$51.80" in page  # last good data kept and labelled


def test_unmapped_scope_never_confirms_cost(app, people, monkeypatch):
    boss, _ = people
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "fixture-not-a-real-key")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_IDS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_IDS", raising=False)
    page = boss.get("/admin/usage").text
    assert "Awaiting mapping" in page


def test_manual_sync_is_debounced(app, people, monkeypatch):
    boss, _ = people
    monkeypatch.setattr(U, "sync_all", lambda manual=False, fetchers=None: {})
    r1 = boss.post("/api/admin/usage/sync", headers=H).json()
    r2 = boss.post("/api/admin/usage/sync", headers=H).json()
    assert r1["started"] is True and r2["started"] is False and "Try again" in r2["message"]


def test_fetch_anthropic_paginates_and_parses_cents(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_IDS", "wrkspc_app")
    start = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    class Page(list):
        data_refreshed_at = dt.datetime(2026, 11, 5, 10, tzinfo=dt.timezone.utc)

    def item(i):
        return NS(starting_at=start, ending_at=start + dt.timedelta(days=1),
                  group=NS(model_dump=lambda i=i: {"workspace_id": "wrkspc_app", "api_key_id": f"k{i}", "model": "m", "cost_type": "tokens", "token_type": "output_tokens", "service_tier": "standard"}),
                  cost=NS(amount="41280.000000", list_amount="51600.000000", currency="USD"))

    calls = []
    cost = NS(list=lambda **kw: calls.append(kw) or Page([item(i) for i in range(1500)]))  # SDK pages transparently
    usage = NS(list=lambda **kw: Page([]))
    client = NS(beta=NS(organization=NS(analytics=NS(cost=cost, usage=usage))))
    out = U.fetch_anthropic(start, start + dt.timedelta(days=40), client=client)
    assert len(calls) == 2  # 40 days split into <=31-day requests
    assert all(c["time_bucket_width"] == "1d" and c["limit"] == 1000 for c in calls)
    assert out["cost"][0]["amount"] == Decimal("41280.000000") and U.cents_to_usd(out["cost"][0]["amount"]) == Decimal("412.8")
    assert len({r["dedup_key"] for r in out["cost"]}) == 1500
    assert out["data_refreshed_at"] == Page.data_refreshed_at


def test_live_activity_notifies_without_touching_costs(app, people):
    import psycopg
    from app.config import settings
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("LISTEN ai_usage")
        rid = U.record_start(U.Attribution(initiated_by="system"), "commentary", "claude-opus-5-5", 1, None)
        U.record_finish(rid, status="succeeded", resp=fake_resp(rid="req_live"))
        got = [n.payload for n in conn.notifies(timeout=5, stop_after=1)]
    assert got == [str(rid)]
    with Session() as s:
        d = U.summary(s, U.resolve_range(s, "month"))
    assert d["system"]["succeeded"] >= 1  # system usage kept separate from employees
