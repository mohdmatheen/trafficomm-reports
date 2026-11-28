"""AI Support: issues, access isolation, deduplication, sanitising, Claude gating, Client Intelligence rule approval,
the engineering-agent workflow (dispatch, retries, GitHub status sync, approval levels, deploy verification,
signed callbacks), administrator controls and the validation hook. External services are mocked: nothing here
calls Anthropic, GitHub or the Claude cloud trigger."""
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import select

from app.db import Base, Client, Session, Task, TaskVersion, User, engine, reset_engine
from app.support import controls, engineering as ENG, knowledge, sanitize
from app.support.models import ClientRule, EngineeringRun, Notification, SupportEvent, SupportIssue, SupportMessage
from tests import zoho_fake

pytestmark = pytest.mark.web
H = {"X-Requested-With": "fetch"}
BOSS = "matheen.m@trafficomm.com"
FAB = "FAB"


# ---------------------------------------------------------------------------
# sanitiser (pure)
# ---------------------------------------------------------------------------
def test_sanitizer_removes_identifiers_but_keeps_report_figures():
    raw = ("Device 6F9619FF-8B86-D011-B42D-00C04FC964FF appsflyer 1700000000000-1234567 mail jane.doe@fab.ae "
           "card 4111 1111 1111 1111 phone +971 50 123 4567 key sk-ant-api03-abcdefghijklmnopqrstuv token ghp_abcdefghijklmnopqrstuvwxyz0123 "
           'value {"af_customer_user_id":"X1"} ip 10.1.2.3 IBAN AE070331234567890123456 '
           "On 2026-11-26 the verified total was 4191 and the portal showed 4203 (difference 12).")
    out, counts = sanitize.redact(raw)
    for leaked in ("6F9619FF", "1700000000000-1234567", "jane.doe", "4111 1111", "123 4567", "sk-ant-api03", "ghp_abc", "af_customer_user_id",
                   "10.1.2.3", "AE0703312345"):
        assert leaked not in out, leaked
    for kept in ("2026-11-26", "4191", "4203", "difference 12"):
        assert kept in out, kept
    assert sum(counts.values()) >= 8
    cols = sanitize.safe_columns(["Event Time", "Customer User ID", "IDFA", "Campaign"])
    assert cols[0] == "Event Time" and cols[3] == "Campaign" and all("values never shared" in c for c in cols[1:3])


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mp():
    m = pytest.MonkeyPatch()
    zoho_fake.configure(m)
    for k in ("ENGINEERING_BACKEND", "CLAUDE_ROUTINE_FIRE_URL", "CLAUDE_ROUTINE_TOKEN", "GITHUB_TOKEN", "GITHUB_DISPATCH_TOKEN",
              "ENGINEERING_CALLBACK_SECRET", "RENDER_GIT_COMMIT", "ENGINEERING_MERGE_ON_APPROVAL"):
        m.delenv(k, raising=False)
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
    assert r.status_code == 303 and not r.headers["location"].startswith("/login"), r.headers["location"]
    return c


def uid(email):
    with Session() as s:
        return s.scalar(select(User.id).where(User.email == email))


def cid(name):
    with Session() as s:
        return s.scalar(select(Client.id).where(Client.name == name))


@pytest.fixture(scope="module")
def boss(app, mp):
    return login(app, mp, BOSS)


@pytest.fixture(scope="module")
def people(app, mp, boss):
    atc = cid("Around The Clock Media")
    for email, name, clients in (("janani@trafficomm.com", "Janani", [atc]), ("ravi@trafficomm.com", "Ravi", [atc]),
                                 ("lead@trafficomm.com", "Lena", []), ("omar@trafficomm.com", "Omar", [])):
        r = boss.post("/api/admin/users", headers=H, json={"email": email, "name": name, "client_ids": clients})
        assert r.status_code == 200, r.text
    r = boss.post("/api/admin/clients", headers=H, json={"name": FAB, "short": "FAB",
                                                          "user_ids": [uid("janani@trafficomm.com"), uid("lead@trafficomm.com"), uid("omar@trafficomm.com")]})
    assert r.status_code == 200, r.text
    assert boss.patch(f"/api/admin/support/users/{uid('lead@trafficomm.com')}", headers=H, json={"is_lead": True}).json()["ok"]
    return {e.split("@")[0]: login(app, mp, e) for e in ("janani@trafficomm.com", "ravi@trafficomm.com", "lead@trafficomm.com", "omar@trafficomm.com")}


def new_issue(client, **kw):
    r = client.post("/api/support/issues", headers=H, json=kw)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# issues, dedup, access
# ---------------------------------------------------------------------------
def test_issue_is_created_deduplicated_and_isolated_by_client(app, mp, boss, people):
    jan, ravi = people["janani"], people["ravi"]
    fab = cid(FAB)
    text = "The FAB report total on 26 Nov is 4203 but the verified report says 4191, a difference of 12."
    a = new_issue(jan, title="FAB total too high", text=text, client_id=fab)
    assert a["created"] and a["ref"].startswith("AIS-")
    b = new_issue(jan, title="FAB total too high", text=text, client_id=fab)
    assert b["id"] == a["id"] and not b["created"]
    with Session() as s:
        i = s.get(SupportIssue, a["id"])
        assert i.category == "discrepancy" and i.reporter_id == uid("janani@trafficomm.com") and i.client_id == fab
        kinds = [m.kind for m in s.scalars(select(SupportMessage).where(SupportMessage.issue_id == i.id))]
        assert kinds.count("employee") == 1 and "assistant" in kinds and "system" in kinds  # rules reply + duplicate note
        assert s.scalar(select(SupportEvent.id).where(SupportEvent.issue_id == i.id, SupportEvent.action == "created"))
    # Ravi has no FAB access: can't see, read state, post, or open issues for FAB
    assert ravi.get(f"/support/{a['id']}").status_code == 404
    assert ravi.get(f"/api/support/issues/{a['id']}/state").status_code == 404
    assert ravi.post(f"/api/support/issues/{a['id']}/messages", headers=H, json={"text": "hi"}).status_code == 404
    assert ravi.post("/api/support/issues", headers=H, json={"title": "x", "text": "y", "client_id": fab}).status_code == 400
    assert a["ref"] not in ravi.get("/support?f=all").text
    # Omar shares the FAB client, so he can see and help on it; the admin sees everything
    assert people["omar"].get(f"/support/{a['id']}").status_code == 200
    assert a["ref"] in boss.get("/support?f=all").text
    # CSRF header required
    assert jan.post("/api/support/issues", json={"title": "x", "text": "y"}).status_code in (400, 403)


def test_claude_is_not_called_for_an_unauthorised_client(app, mp, people):
    from app.config import Settings
    from app.support import agent
    calls = []
    mp.setattr(Settings, "ai_enabled", property(lambda self: True))
    mp.setattr(agent, "call_claude", lambda *a, **k: calls.append(1) or None)
    try:
        r = new_issue(people["janani"], title="Totals unclear for an unusual campaign mapping",
                      text="Something unusual: the campaign mapping looks ambiguous for the new structure request we discussed.", client_id=cid(FAB))
        with Session() as s:
            i = s.get(SupportIssue, r["id"])
            texts = " ".join(m.text for m in s.scalars(select(SupportMessage).where(SupportMessage.issue_id == i.id)))
        assert calls == [] and "authorised for external AI processing" in texts
    finally:
        mp.setattr(Settings, "ai_enabled", property(lambda self: False))


def test_employee_limit_and_emergency_stop_gate_claude(app, mp, boss, people):
    with Session() as s:
        jan = s.get(User, uid("janani@trafficomm.com"))
        atc = s.get(Client, cid("Around The Clock Media"))
        atc.ai_processing_allowed = True
        s.commit()
        assert controls.claude_gate(s, jan, atc).reason.startswith("Claude isn't configured")
    # employees can't change controls; admins can, and the change is audited
    assert people["janani"].put("/api/admin/support/settings", headers=H, json={"emergency_stop": 1}).status_code in (401, 403)
    assert boss.put("/api/admin/support/settings", headers=H, json={"monthly_ai_budget_usd": "-5"}).status_code == 400
    r = boss.put("/api/admin/support/settings", headers=H, json={"emergency_stop": 1})
    assert r.status_code == 200 and "emergency_stop=1" in r.json()["changed"]
    with Session() as s:
        assert not controls.engineering_gate(s).ok
        assert controls.engineering_gate(s).limit_hit == "emergency_stop"
    boss.put("/api/admin/support/settings", headers=H, json={"emergency_stop": 0})
    assert boss.patch(f"/api/admin/support/users/{uid('janani@trafficomm.com')}", headers=H, json={"monthly_limit": 0}).status_code == 200
    from app.config import Settings
    mp.setattr(Settings, "ai_enabled", property(lambda self: True))
    try:
        with Session() as s:
            g = controls.claude_gate(s, s.get(User, uid("janani@trafficomm.com")), s.get(Client, cid("Around The Clock Media")))
            assert not g.ok and g.limit_hit == "employee_limit"
            assert controls.claude_gate(s, s.get(User, uid(BOSS)), s.get(Client, cid("Around The Clock Media"))).ok  # admins exempt
    finally:
        mp.setattr(Settings, "ai_enabled", property(lambda self: False))
        boss.patch(f"/api/admin/support/users/{uid('janani@trafficomm.com')}", headers=H, json={"monthly_limit": 60})


# ---------------------------------------------------------------------------
# Client Intelligence
# ---------------------------------------------------------------------------
def test_rules_need_a_lead_or_admin_and_no_self_approval(app, mp, boss, people):
    fab = cid(FAB)
    r = people["janani"].post(f"/api/intelligence/{fab}/rules", headers=H, json={
        "title": "Exclude Retargeting campaigns from iOS All Events", "body": "Campaigns whose name contains 'RTG' are excluded.", "category": "exclusion"})
    assert r.status_code == 200, r.text
    rid, key = r.json()["id"], r.json()["key"]
    # an employee who isn't a reporting lead can't approve; Ravi can't even see FAB
    assert people["omar"].post(f"/api/intelligence/rules/{rid}/decide", headers=H, json={"approve": True}).status_code == 403
    assert people["ravi"].post(f"/api/intelligence/rules/{rid}/decide", headers=H, json={"approve": True}).status_code == 404
    with Session() as s:
        assert s.scalar(select(Notification.id).where(Notification.user_id == uid("lead@trafficomm.com"), Notification.kind == "approval_required"))
    assert people["lead"].post(f"/api/intelligence/rules/{rid}/decide", headers=H, json={"approve": True, "validated": True}).json()["status"] == "approved"
    # the lead proposes v2 and can't approve their own; the admin can, and v1 is superseded
    r2 = people["lead"].post(f"/api/intelligence/{fab}/rules", headers=H, json={
        "title": "Exclude Retargeting campaigns", "body": "Names containing 'RTG' or 'Retarget'.", "category": "exclusion", "key": key}).json()
    assert r2["version"] == 2
    assert people["lead"].post(f"/api/intelligence/rules/{r2['id']}/decide", headers=H, json={"approve": True}).status_code == 403
    assert boss.post(f"/api/intelligence/rules/{r2['id']}/decide", headers=H, json={"approve": True}).json()["status"] == "approved"
    with Session() as s:
        assert s.get(ClientRule, rid).status == "superseded"
        assert [x.version for x in knowledge.approved(s, fab) if x.key == key] == [2]
        hits = knowledge.lookup(s, fab, "should retargeting RTG campaigns be excluded?")
        assert hits and hits[0][0].key == key
    page = people["janani"].get(f"/intelligence/{fab}").text
    assert "Exclude Retargeting campaigns" in page


def test_seeded_fab_recipe_rules_are_approved():
    with Session() as s:
        knowledge.seed_from_code(s)
        s.commit()
        fab = s.scalar(select(Client.id).where(Client.name == FAB))
        keys = {r.key for r in knowledge.approved(s, fab)} if fab else set()
    # seeding attaches recipes to FAB clients only when such a client exists
    assert fab is None or keys


# ---------------------------------------------------------------------------
# engineering agent
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data if data is not None else {}, headers or {}
        self.content = json.dumps(self._data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


@pytest.fixture
def eng_env(mp):
    m = pytest.MonkeyPatch()
    m.setenv("ENGINEERING_BACKEND", "routine")
    m.setenv("CLAUDE_ROUTINE_FIRE_URL", "https://api.anthropic.com/v1/claude_code/routines/trig_test/fire")
    m.setenv("CLAUDE_ROUTINE_TOKEN", "sk-ant-oat01-test")
    m.setenv("GITHUB_TOKEN", "ghp_test")
    m.setenv("ENGINEERING_CALLBACK_SECRET", "cb-secret")
    yield m
    m.undo()


def _portal_bug(client, fab):
    return new_issue(client, title="Export button breaks for FAB report", client_id=fab,
                     text="When I click export on the FAB report the portal shows an error page instead of downloading the file. jane@fab.ae saw it too.")


def test_engineering_full_cycle_with_lead_approval_and_verified_deploy(app, mp, boss, people, eng_env):
    fab = cid(FAB)
    i = _portal_bug(people["janani"], fab)
    r = people["janani"].post(f"/api/support/issues/{i['id']}/engineering", headers=H, json={})
    assert r.status_code == 400 and "follow-up question" in r.json()["detail"]  # rules asked follow-ups first
    with Session() as s:  # the employee answered them
        iss = s.get(SupportIssue, i["id"])
        iss.meta = {**(iss.meta or {}), "open_questions": []}
        s.commit()
    # not authorised for external AI processing yet
    r = people["janani"].post(f"/api/support/issues/{i['id']}/engineering", headers=H, json={})
    assert r.status_code == 400 and "authorised" in r.json()["detail"]
    assert boss.patch(f"/api/admin/support/clients/{fab}", headers=H, json={"ai_processing_allowed": True}).json()["ai_processing_allowed"]
    run_id = people["janani"].post(f"/api/support/issues/{i['id']}/engineering", headers=H, json={}).json()["run"]
    assert people["omar"].post(f"/api/support/issues/{i['id']}/engineering", headers=H, json={}).json()["run"] == run_id  # no duplicate
    sent = []
    eng_env.setattr(ENG.httpx, "post", lambda url, **kw: sent.append((url, kw)) or FakeResp(200, {
        "type": "routine_fire", "claude_code_session_id": "session_x", "claude_code_session_url": "https://claude.ai/code/session_x"}))
    with Session() as s:
        assert ENG.dispatch_due(s) == 1
        s.commit()
        run = s.get(EngineeringRun, run_id)
        assert run.status == "dispatched" and run.session_url == "https://claude.ai/code/session_x"
        assert s.get(SupportIssue, i["id"]).status == "engineering_running"
    url, kw = sent[0]
    assert url.endswith("/trig_test/fire") and kw["headers"]["Authorization"] == "Bearer sk-ant-oat01-test"
    assert kw["headers"]["anthropic-beta"] == "experimental-cc-routine-2026-04-01"
    text = kw["json"]["text"]
    assert f"Trafficomm-Issue: {i['ref']}" in text and "jane@fab.ae" not in text and "agent-report" in text and len(text) <= 60000
    # the agent opens a PR touching calculation code; checks pass -> awaiting lead approval
    ref = i["ref"]
    pr = {"number": 41, "html_url": "https://github.com/x/y/pull/41", "state": "open", "merged_at": None, "created_at": "2099-01-01T00:00:00Z",
          "head": {"ref": f"claude/{ref.lower()}-export", "sha": "abc123"}, "title": f"[{ref}] Fix export",
          "body": f"Trafficomm-Issue: {ref}\n```agent-report\n" + json.dumps({"root_cause": "Export used a stale path", "fix": "Use the version path",
                                                                              "validated_dates": ["2026-11-25", "2026-11-26"]}) + "\n```"}

    def gh(method, path, token=None, **kw):
        if path.endswith("/pulls"):
            return FakeResp(200, [pr])
        if path.endswith("/pulls/41/files"):
            return FakeResp(200, [{"filename": "app/engine/event_report.py"}, {"filename": "tests/test_x.py"}])
        if "/check-runs" in path:
            return FakeResp(200, {"check_runs": [{"status": "completed", "conclusion": "success"}]})
        if path.endswith("/pulls/41"):
            return FakeResp(200, pr)
        if "/compare/" in path:
            return FakeResp(200, {"status": "ahead"})
        raise AssertionError(path)
    eng_env.setattr(ENG, "_gh", gh)
    with Session() as s:
        assert ENG.sync_all(s) >= 1
        s.commit()
        run = s.get(EngineeringRun, run_id)
        assert (run.status, run.approval_level, run.pr_number) == ("awaiting_approval", "lead", 41)
        assert run.agent_report["root_cause"] == "Export used a stale path"
        assert s.get(SupportIssue, i["id"]).status == "awaiting_approval"
    # an employee without lead rights can't approve; the reporting lead can
    assert people["omar"].post(f"/api/support/runs/{run_id}/approve", headers=H).status_code == 403
    r = people["lead"].post(f"/api/support/runs/{run_id}/approve", headers=H)
    assert r.status_code == 200 and "can now be merged" in r.json()["message"]
    # merged on GitHub, but the running service is still the old commit: not deployed
    pr.update(merged_at="2099-01-01T01:00:00Z", merge_commit_sha="feedbeef1234", state="closed")
    eng_env.setenv("RENDER_GIT_COMMIT", "0ldc0mm1t")
    eng_env.setattr(ENG, "_gh", lambda m, p, token=None, **kw: FakeResp(404, {}) if "/compare/" in p else gh(m, p, token, **kw))
    with Session() as s:
        ENG.sync_all(s)
        ENG.sync_all(s)
        s.commit()
        assert s.get(EngineeringRun, run_id).status == "merged"
        assert s.get(SupportIssue, i["id"]).status == "awaiting_deploy"
    # the new commit is live -> deployed -> employee asked to review (no task to regenerate here)
    eng_env.setenv("RENDER_GIT_COMMIT", "feedbeef1234")
    with Session() as s:
        ENG.sync_all(s)
        s.commit()
        assert s.get(EngineeringRun, run_id).status == "deployed"
        assert s.get(SupportIssue, i["id"]).status == "awaiting_validation"
    r = people["janani"].post(f"/api/support/issues/{i['id']}/confirm", headers=H, json={"ok": True})
    assert r.status_code == 200
    with Session() as s:
        assert s.get(SupportIssue, i["id"]).status == "resolved"
        rule = s.scalar(select(ClientRule).where(ClientRule.key == f"fix-{ref.lower()}"))
        assert rule.status == "approved" and rule.validated and "stale path" in rule.body
    # only an admin can roll back; rollback opens a draft revert PR
    assert people["lead"].post(f"/api/support/runs/{run_id}/rollback", headers=H).status_code == 403
    eng_env.setattr(ENG, "_gh", lambda m, p, token=None, **kw: FakeResp(200, {"data": {
        "repository": {"pullRequest": {"id": "PR_1"}}, "revertPullRequest": {"revertPullRequest": {"url": "https://github.com/x/y/pull/42", "number": 42}}}}))
    assert boss.post(f"/api/support/runs/{run_id}/rollback", headers=H).json()["url"].endswith("/pull/42")


def test_security_changes_need_an_administrator(app, mp, boss, people, eng_env):
    fab = cid(FAB)
    with Session() as s:
        jan = s.get(User, uid("janani@trafficomm.com"))
        iss, _ = __import__("app.support.service", fromlist=["x"]).create_issue(
            s, jan, "Login redirect loop on the FAB workspace", "After signing in with Zoho the FAB workspace keeps redirecting back to login. Every time.", client_id=fab)
        run = EngineeringRun(issue_id=iss.id, status="pr_open", idempotency_key=f"t{iss.id}", branch="claude/x", pr_number=77, dispatched_at=iss.created_at)
        s.add(run)
        s.commit()
        rid = run.id
    pr = {"number": 77, "html_url": "u", "state": "open", "merged_at": None, "head": {"ref": "claude/x", "sha": "s"}, "body": ""}
    eng_env.setattr(ENG, "_gh", lambda m, p, token=None, **kw: FakeResp(200, [{"filename": "app/auth.py"}]) if p.endswith("/files") else FakeResp(200, pr))
    eng_env.setattr(ENG, "checks_state", lambda sha: "success")
    with Session() as s:
        ENG.sync_run(s, s.get(EngineeringRun, rid))
        s.commit()
        assert s.get(EngineeringRun, rid).approval_level == "admin"
    assert people["lead"].post(f"/api/support/runs/{rid}/approve", headers=H).status_code == 403
    assert boss.post(f"/api/support/runs/{rid}/approve", headers=H).status_code == 200
    assert ENG.approval_level(["app/engine/x.py", "docs/a.md"]) == "lead"
    assert ENG.approval_level(["app/engine/x.py", ".github/workflows/a.yml"]) == "admin"
    assert ENG.approval_level(["app/db.py"]) == "admin"


def test_dispatch_retries_transient_errors_and_never_refires_unknown_outcomes(app, mp, boss, people, eng_env):
    fab = cid(FAB)
    ids = []
    for n in range(3):
        i = new_issue(people["omar"], title=f"Portal error number {n} on the FAB pages", client_id=fab,
                      text=f"The FAB task page shows an internal error {n} whenever I open the versions tab; it started this morning.")
        with Session() as s:
            iss = s.get(SupportIssue, i["id"])
            iss.meta = {**(iss.meta or {}), "open_questions": []}
            s.commit()
        ids.append(people["omar"].post(f"/api/support/issues/{i['id']}/engineering", headers=H, json={}).json()["run"])
    with Session() as s:  # only these three are due
        for r in s.scalars(select(EngineeringRun).where(EngineeringRun.status.in_(ENG.RUNNING))):
            r.status = "closed"
        s.commit()
    outcomes = iter([FakeResp(429, {}, {"retry-after": "120"}), ENG.httpx.ReadTimeout("t"), FakeResp(403, {"error": {"message": "token revoked"}})])

    def post(url, **kw):
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o
    eng_env.setattr(ENG.httpx, "post", post)
    controls_set = {"engineering_max_concurrent": 5}
    with Session() as s:
        for k, v in controls_set.items():
            controls.set_value(s, k, v)
        ENG.dispatch_due(s)
        s.commit()
        a, b, c = (s.get(EngineeringRun, x) for x in ids)
        assert a.status == "queued" and a.attempts == 1 and "429" in a.last_error and a.next_attempt_at > a.created_at
        assert b.status == "dispatched" and "unknown" in b.last_error.lower()
        assert c.status == "failed" and "token revoked" in c.last_error
        assert s.get(SupportIssue, c.issue_id).status == "unresolved"


def test_signed_callback_can_ask_questions_but_never_mark_fixed(app, mp, people, eng_env):
    i = new_issue(people["janani"], title="Callback test issue for the FAB report", client_id=cid(FAB),
                  text="The FAB daily report misses some events from yesterday and I can't see why; numbers look low by 30.")
    body = json.dumps({"issue": i["ref"], "status": "needs_info", "message": "Need the timezone.",
                       "questions": ["Is the verified report in Dubai time or UTC?"]}).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(b"cb-secret", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    c = people["janani"]
    assert c.post("/api/engineering/callback", content=body, headers={"x-trafficomm-timestamp": ts, "x-trafficomm-signature": "sha256=bad"}).status_code == 401
    old = str(int(time.time()) - 3600)
    old_sig = "sha256=" + hmac.new(b"cb-secret", old.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert c.post("/api/engineering/callback", content=body, headers={"x-trafficomm-timestamp": old, "x-trafficomm-signature": old_sig}).status_code == 401
    r = c.post("/api/engineering/callback", content=body, headers={"x-trafficomm-timestamp": ts, "x-trafficomm-signature": sig})
    assert r.status_code == 200, r.text
    with Session() as s:
        iss = s.get(SupportIssue, i["id"])
        assert iss.status == "awaiting_employee" and iss.meta["open_questions"] == ["Is the verified report in Dubai time or UTC?"]
    bad = json.dumps({"issue": i["ref"], "status": "fixed"}).encode()
    sig2 = "sha256=" + hmac.new(b"cb-secret", ts.encode() + b"." + bad, hashlib.sha256).hexdigest()
    assert c.post("/api/engineering/callback", content=bad, headers={"x-trafficomm-timestamp": ts, "x-trafficomm-signature": sig2}).status_code == 400


def test_engineering_off_without_backend(app, mp):
    assert ENG.backend() == "off"
    assert ENG.verify_signature(b"{}", str(int(time.time())), "sha256=x") is False


# ---------------------------------------------------------------------------
# FAB pilot, attachments, validation hook, pages
# ---------------------------------------------------------------------------
def test_fab_pilot_collects_files_and_answers(app, mp, people):
    jan = people["janani"]
    assert jan.post("/api/support/issues", headers=H, json={"pilot": "fab_ios_all_events", "client_id": cid("Around The Clock Media")}).status_code == 400
    i = new_issue(jan, pilot="fab_ios_all_events", client_id=cid(FAB), text="Portal higher by 11 on 25 Nov and by 12 on 26 Nov; verified 26 Nov total 4191.")
    r = jan.post(f"/api/support/issues/{i['id']}/attachments", headers=H, data={"slot": "ios_in_app"},
                 files={"file": ("ios_in_app.csv", b"Event Time,Event Name\n2026-11-26 10:00:00,af_purchase\n", "text/csv")})
    assert r.status_code == 200 and r.json()["slot"] == "ios_in_app"
    assert jan.post(f"/api/support/issues/{i['id']}/attachments", headers=H, data={"slot": "x"},
                    files={"file": ("run.exe", b"MZ", "application/octet-stream")}).status_code == 400
    r = jan.post(f"/api/support/issues/{i['id']}/pilot-answers", headers=H, json={"answers": {"sheet_check": "B", "timezone": "Asia/Dubai"}})
    missing = r.json()["missing"]
    assert any("non-organic" in m.lower() for m in missing) and any("verified" in m.lower() for m in missing)
    page = jan.get(f"/support/{i['id']}").text
    assert "ios_in_app.csv" in page and "Asia/Dubai" in page
    with Session() as s:
        from app.support.models import SupportAttachment
        aid = s.scalar(select(SupportAttachment.id).where(SupportAttachment.issue_id == i["id"]))
    assert jan.get(f"/support/attachments/{aid}/download").status_code == 200
    assert people["ravi"].get(f"/support/attachments/{aid}/download").status_code == 404


def test_validation_failure_opens_an_issue_for_the_task_owner(app, mp, people):
    from app.support.service import from_validation
    with Session() as s:
        t = Task(client_id=cid(FAB), owner_id=uid("omar@trafficomm.com"), title="FAB daily Nov", status="validation_blocked")
        s.add(t)
        s.flush()
        v = TaskVersion(task_id=t.id, number=1, kind="initial", status="validation_blocked", created_by=t.owner_id,
                        result={"validation": {"checks": [{"status": "fail", "title": "Total matches source", "detail": "4203 vs 4191"}]}})
        s.add(v)
        s.flush()
        t.current_version_id = v.id
        a = from_validation(s, t, v)
        assert a is not None and a.assignee_id == uid("omar@trafficomm.com") and "4203 vs 4191" in a.summary
        assert from_validation(s, t, v) is None  # once per version
        s.commit()
        assert s.scalar(select(Notification.id).where(Notification.user_id == uid("omar@trafficomm.com"), Notification.kind == "validation_failed"))


def test_pages_render(app, mp, boss, people):
    for path in ("/support", "/support?f=waiting", "/support/new", "/notifications", "/intelligence", f"/intelligence/{cid(FAB)}"):
        r = people["janani"].get(path)
        assert r.status_code == 200, (path, r.text[:300])
    assert people["janani"].get("/admin/support").status_code in (303, 401, 403)
    r = boss.get("/admin/support")
    assert r.status_code == 200 and "Emergency" in r.text
    assert "AI Support" in boss.get("/").text
    assert people["janani"].post("/api/notifications/read", headers=H, json={}).json()["ok"]
