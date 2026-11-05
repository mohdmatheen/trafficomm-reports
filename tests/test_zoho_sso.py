"""Sign in with Zoho - the only sign-in method (SYNTHETIC: Zoho's token endpoint is replaced by a
fake; no network, no real Zoho account)."""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import AuditLog, Base, Session, Task, User, engine, reset_engine
from tests import zoho_fake

pytestmark = pytest.mark.web


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    zoho_fake.configure(mp)
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def app(monkeypatch_module):
    reset_engine()
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    from scripts.seed import main as seed
    seed()
    from app.web import app as a
    return a


def zoho_login(app, monkeypatch, email, **kw):
    return zoho_fake.sign_in(app, monkeypatch, email, **kw)


def _err(r):
    return parse_qs(urlparse(r.headers["location"]).query).get("error", [""])[0]


def test_login_page_is_zoho_only(app):
    r = TestClient(app).get("/login")
    assert "Sign in with Zoho" in r.text and "/auth/zoho" in r.text
    for gone in ('type="password"', 'type="email"', "password instead", "Other sign-in options", "DESIGN CONCEPT"):
        assert gone not in r.text
    for copy in ("Welcome back.", "Your work account", "Authorized Trafficomm employees only.",
                 "Need access? Contact your workspace administrator.", "From raw reports.", "Built for the Trafficomm team."):
        assert copy in r.text


def test_boot_admin_gets_in_as_admin(app, monkeypatch):
    c, r, _ = zoho_login(app, monkeypatch, "Matheen.M@trafficomm.com")
    assert r.status_code == 303 and r.headers["location"] == "/tasks"
    with Session() as s:
        u = s.scalar(select(User).where(User.email == "matheen.m@trafficomm.com"))
        assert u.role == "admin" and u.password_hash == "!"
    assert c.get("/admin").status_code == 200


def test_existing_password_account_links_and_keeps_history(app, monkeypatch):
    """An account created under password sign-in links to Zoho by email: same row, role and tasks."""
    with Session() as s:
        from app.db import Client
        u = User(email="omar@trafficomm.com", name="Omar", role="admin", password_hash="$2b$12$legacyhashlegacyhashlegacyhashlegacyhashlegacyhashlega")
        s.add(u)
        s.flush()
        cl = s.scalar(select(Client))
        s.add(Task(client_id=cl.id, owner_id=u.id, title="Legacy task", status="draft"))
        s.commit()
        uid = u.id
    c, r, _ = zoho_login(app, monkeypatch, "OMAR@trafficomm.com")
    assert r.headers["location"] == "/tasks"
    with Session() as s:
        assert s.scalars(select(User).where(User.email.ilike("omar@trafficomm.com"))).all()[0].id == uid
        assert s.scalar(select(User.password_hash).where(User.id == uid)).startswith("$2b$")  # column left untouched
    assert c.get("/admin").status_code == 200  # role kept
    assert "Legacy task" in c.get("/tasks").text


def test_unlisted_employee_denied_until_admin_grants(app, monkeypatch):
    c0, r, _ = zoho_login(app, monkeypatch, "sara@trafficomm.com")
    assert r.headers["location"] == "/login?error=no_access"
    page = c0.get("/login?error=no_access").text
    assert "have access yet" in page and "sara@trafficomm.com" in page
    with Session() as s:
        assert s.scalar(select(User).where(User.email == "sara@trafficomm.com")) is None

    admin, _, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com")
    r = admin.post("/admin/users", data={"email": "Sara@trafficomm.com", "role": "employee"}, follow_redirects=False)
    assert "Access granted" in r.headers["location"].replace("%20", " ")
    r = admin.post("/admin/users", data={"email": "x@gmail.com", "role": "employee"}, follow_redirects=False)
    assert "can%20be%20granted" in r.headers["location"] or "can be granted" in r.headers["location"]

    c, r, _ = zoho_login(app, monkeypatch, "sara@trafficomm.com")
    assert r.headers["location"] == "/tasks"
    assert c.get("/admin").status_code == 403  # employee

    with Session() as s:
        sid = s.scalar(select(User.id).where(User.email == "sara@trafficomm.com"))
    admin.post(f"/admin/users/{sid}/role", data={"role": "admin"})
    assert c.get("/admin").status_code == 200  # role is read from the DB on each request

    admin.post(f"/admin/users/{sid}/toggle")  # revoke
    assert c.get("/", follow_redirects=False).status_code == 303  # existing session no longer valid
    _, r, _ = zoho_login(app, monkeypatch, "sara@trafficomm.com")
    assert _err(r) == "revoked"
    with Session() as s:
        acts = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.target == "sara@trafficomm.com"))]
    assert {"login_denied", "grant_access", "login_zoho", "set_role:admin", "revoke_access"} <= set(acts)


def test_domain_alone_never_grants_access(app, monkeypatch):
    for email in ("new.hire@trafficomm.com", "another@trafficomm.com"):
        _, r, _ = zoho_login(app, monkeypatch, email)
        assert _err(r) == "no_access"
    with Session() as s:
        assert s.scalar(select(User).where(User.email == "new.hire@trafficomm.com")) is None


def test_other_domains_and_bad_tokens_rejected(app, monkeypatch):
    _, r, _ = zoho_login(app, monkeypatch, "someone@gmail.com")
    assert _err(r) == "unauthorized"
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", aud="1000.OTHERAPP")
    assert _err(r) == "failed"
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", nonce_ok=False)
    assert _err(r) == "expired"
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", verified=False)
    assert _err(r) == "unauthorized"
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", server_param="https://evil.example.com")
    assert _err(r) == "failed"


def test_cancel_failure_and_bad_state_messages(app):
    c = TestClient(app)
    c.get("/auth/zoho", follow_redirects=False)
    r = c.get("/auth/zoho/callback?error=access_denied&state=x", follow_redirects=False)
    assert _err(r) == "cancelled"
    assert "Sign-in cancelled" in c.get(r.headers["location"]).text
    c.get("/auth/zoho", follow_redirects=False)
    r = c.get("/auth/zoho/callback?error=server_error", follow_redirects=False)
    assert _err(r) == "failed"
    assert "didn’t complete" in c.get(r.headers["location"]).text
    r = TestClient(app).get("/auth/zoho/callback?code=abc&state=forged", follow_redirects=False)
    assert _err(r) == "expired"
    # free text in the URL is never echoed: unknown codes show the generic failure
    t = TestClient(app).get("/login?error=Call+0800+to+verify+your+password").text
    assert "0800" not in t and "didn’t complete" in t


def test_other_zoho_data_centre(app, monkeypatch):
    _, r, seen = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", server_param="https://accounts.zoho.eu")
    assert r.headers["location"] == "/tasks" and seen["server"] == "https://accounts.zoho.eu"


def test_open_redirect_blocked(app, monkeypatch):
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", next="//evil.example.com")
    assert r.headers["location"] == "/"


def test_not_configured_shows_admin_message_and_no_fallback(app, monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_secret", None)
    c = TestClient(app)
    assert c.get("/auth/zoho", follow_redirects=False).headers["location"] == "/login?error=not_configured"
    page = c.get("/login").text
    assert "isn’t available" in page and 'type="password"' not in page
    assert c.post("/login", data={"email": "matheen.m@trafficomm.com", "password": "anything"}).status_code == 405
