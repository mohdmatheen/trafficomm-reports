"""Sign in with Zoho (SYNTHETIC: Zoho's token endpoint is replaced by a fake; no network)."""
import base64
import json
import time
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import zoho as Z
from app.config import settings
from app.db import AuditLog, Base, Session, User, engine, reset_engine

pytestmark = pytest.mark.web
CID = "1000.TESTCLIENT"


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".sig"


@pytest.fixture(scope="module")
def app(monkeypatch_module):
    reset_engine()
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    from scripts.seed import main as seed
    seed()
    from app.web import app as a
    return a


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "zoho_client_id", CID)
    mp.setattr(settings, "zoho_client_secret", "test-secret")
    mp.setattr(settings, "zoho_allowed_domains", ["trafficomm.com"])
    mp.setattr(settings, "zoho_admin_emails", ["matheen.m@trafficomm.com"])
    mp.setattr(settings, "zoho_auto_provision", False)
    mp.setattr(settings, "public_base_url", "https://reports.example.test")
    yield mp
    mp.undo()


def zoho_login(app, monkeypatch, email, *, aud=CID, verified=True, nonce_ok=True, server_param=None):
    c = TestClient(app)
    r = c.get("/auth/zoho?next=/tasks", follow_redirects=False)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "accounts.zoho.in" and loc.path == "/oauth/v2/auth"
    assert q["client_id"] == [CID] and q["redirect_uri"] == ["https://reports.example.test/auth/zoho/callback"]
    assert "openid" in q["scope"][0] and "email" in q["scope"][0]
    nonce = q["nonce"][0] if nonce_ok else "other"
    claims = {"iss": "https://accounts.zoho.in", "aud": aud, "nonce": nonce, "exp": time.time() + 600,
              "email": email, "email_verified": verified, "name": "Test Person"}
    seen = {}

    def fake_exchange(code, server, base, client=None):
        seen["server"] = server
        return {"access_token": "x", "id_token": _jwt(claims)}

    monkeypatch.setattr(Z, "exchange_code", fake_exchange)
    url = f"/auth/zoho/callback?code=abc&state={q['state'][0]}"
    if server_param:
        url += "&accounts-server=" + server_param
    r = c.get(url, follow_redirects=False)
    return c, r, seen


def _err(r):
    return parse_qs(urlparse(r.headers["location"]).query).get("error", [""])[0]


def test_login_page_shows_zoho(app):
    r = TestClient(app).get("/login")
    assert "Sign in with Zoho" in r.text and "/auth/zoho" in r.text


def test_boot_admin_gets_in_as_admin(app, monkeypatch):
    c, r, _ = zoho_login(app, monkeypatch, "Matheen.M@trafficomm.com")
    assert r.status_code == 303 and r.headers["location"] == "/tasks"
    with Session() as s:
        u = s.scalar(select(User).where(User.email == "matheen.m@trafficomm.com"))
        assert u.role == "admin" and u.password_hash == "!"
    assert c.get("/admin").status_code == 200


def test_unlisted_employee_denied_until_admin_grants(app, monkeypatch):
    _, r, _ = zoho_login(app, monkeypatch, "sara@trafficomm.com")
    assert r.headers["location"].startswith("/login?error=") and "access yet" in _err(r)
    with Session() as s:
        assert s.scalar(select(User).where(User.email == "sara@trafficomm.com")) is None

    admin, _, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com")
    r = admin.post("/admin/users", data={"email": "Sara@trafficomm.com", "role": "employee"}, follow_redirects=False)
    assert "Access granted" in unquote(r.headers["location"])
    # non-Trafficomm emails can't be granted Zoho access
    r = admin.post("/admin/users", data={"email": "x@gmail.com", "role": "employee"}, follow_redirects=False)
    assert "can be granted" in unquote(r.headers["location"])

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
    assert "revoked" in _err(r)
    with Session() as s:
        acts = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.target == "sara@trafficomm.com"))]
    assert {"login_denied", "grant_access", "login_zoho", "set_role:admin", "revoke_access"} <= set(acts)


def test_other_domains_and_bad_tokens_rejected(app, monkeypatch):
    _, r, _ = zoho_login(app, monkeypatch, "someone@gmail.com")
    assert "Only Trafficomm" in _err(r)
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", aud="1000.OTHERAPP")
    assert "wasn't issued for this app" in _err(r)
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", nonce_ok=False)
    assert "expired" in _err(r)
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", verified=False)
    assert "verified" in _err(r)
    _, r, _ = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", server_param="https://evil.example.com")
    assert "unexpected" in _err(r)


def test_other_zoho_data_centre_and_bad_state(app, monkeypatch):
    _, r, seen = zoho_login(app, monkeypatch, "matheen.m@trafficomm.com", server_param="https://accounts.zoho.eu")
    assert r.headers["location"] == "/tasks" and seen["server"] == "https://accounts.zoho.eu"
    r = TestClient(app).get("/auth/zoho/callback?code=abc&state=forged", follow_redirects=False)
    assert "expired" in _err(r)


def test_password_login_can_be_turned_off(app, monkeypatch):
    monkeypatch.setattr(settings, "password_login", False)
    r = TestClient(app).post("/login", data={"email": "admin@trafficomm.local", "password": "admin-dev-password"}, follow_redirects=False)
    assert r.status_code == 403
    assert "Sign in with a password" not in TestClient(app).get("/login").text
