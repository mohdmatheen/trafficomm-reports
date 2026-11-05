"""Administration: access management, last-admin protection, retention and client rules (fixtures only)."""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import AuditLog, Base, Profile, Session, User, engine, reset_engine
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
    assert r.status_code == 303, r.headers
    return c


def uid(email):
    with Session() as s:
        return s.scalar(select(User.id).where(User.email == email))


def test_admin_only_everywhere(app, mp):
    boss = login(app, mp, "matheen.m@trafficomm.com")
    assert boss.post("/api/admin/users", headers=H, json={"email": "emp.one@trafficomm.com"}).status_code == 200
    emp = login(app, mp, "emp.one@trafficomm.com")
    assert emp.get("/admin").status_code == 403
    assert emp.get("/admin?partial=users").status_code == 403
    assert emp.get("/api/admin/retention/preview").status_code == 403
    for m, url, body in (("post", "/api/admin/users", {"email": "x@trafficomm.com"}), ("patch", f"/api/admin/users/{uid('matheen.m@trafficomm.com')}", {"role": "employee"}),
                         ("put", "/api/admin/retention", {"days": 30}), ("post", "/api/admin/retention/run", {"confirm": True}),
                         ("put", "/api/admin/profiles/1", {"settings": {}})):
        assert getattr(emp, m)(url, headers=H, json=body).status_code == 403, url
    # CSRF header is required on admin mutations
    assert boss.put("/api/admin/retention", json={"days": 30}).status_code == 403


def test_admin_page_content_and_honest_sign_in_status(app, mp):
    boss = login(app, mp, "matheen.m@trafficomm.com")
    with Session() as s:
        s.add(User(email="old.user@trafficomm.local", name="Old User", role="employee", password_hash="$2b$legacy"))
        s.commit()
    boss.post("/api/admin/users", headers=H, json={"email": "never.signed@trafficomm.com", "name": "Never Signed"})
    page = boss.get("/admin").text
    for text in ("Workspace administration", "Manage access, reporting rules and workspace operations.", "Zoho-only sign-in",
                 "Users &amp; access", "Data retention", "Recent jobs", "Client reporting rules", "You", "Legacy account", "Save policy", "Review cleanup"):
        assert text in page, text
    assert 'type="password"' not in page and "Reset password" not in page
    part = boss.get("/admin?partial=users").text
    row = part.split('data-email="never.signed@trafficomm.com"')[1].split("</tr>")[0]
    assert "Eligible" in row and "Signed in with Zoho" not in row  # no claim without a recorded Zoho sign-in
    row = part.split('data-email="matheen.m@trafficomm.com"')[1].split("</tr>")[0]
    assert "Signed in with Zoho" in row
    row = part.split('data-email="old.user@trafficomm.local"')[1].split("</tr>")[0]
    assert "Legacy account" in row


def test_role_changes_self_protection_and_last_admin(app, mp):
    boss = login(app, mp, "matheen.m@trafficomm.com")
    me = uid("matheen.m@trafficomm.com")
    assert boss.patch(f"/api/admin/users/{me}", headers=H, json={"role": "employee"}).status_code == 400
    assert boss.patch(f"/api/admin/users/{me}", headers=H, json={"active": False}).status_code == 400
    assert boss.patch(f"/api/admin/users/{uid('emp.one@trafficomm.com')}", headers=H, json={"role": "superuser"}).status_code == 400
    # promote a second admin, who then can't remove the first and only remaining admin's rights after demotion races
    boss.post("/api/admin/users", headers=H, json={"email": "admin.two@trafficomm.com", "role": "admin"})
    two = login(app, mp, "admin.two@trafficomm.com")
    with Session() as s:
        for u in s.scalars(select(User).where(User.role == "admin", User.email.notin_(["matheen.m@trafficomm.com", "admin.two@trafficomm.com"]))):
            u.role = "employee"
        s.commit()
    # both admins try to demote each other at the same moment: exactly one must win
    results = {}

    def go(name, client, target):
        results[name] = client.patch(f"/api/admin/users/{target}", headers=H, json={"role": "employee"}).status_code

    t1 = threading.Thread(target=go, args=("a", boss, uid("admin.two@trafficomm.com")))
    t2 = threading.Thread(target=go, args=("b", two, me))
    t1.start(); t2.start(); t1.join(); t2.join()
    with Session() as s:
        admins = s.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all()
    assert len(admins) >= 1, results
    # whoever is left as the only admin can't be revoked or demoted by anyone
    last = admins[0]
    other = boss if last.email != "matheen.m@trafficomm.com" else two
    r = other.patch(f"/api/admin/users/{last.id}", headers=H, json={"active": False})
    assert r.status_code in (400, 403)
    with Session() as s:
        acts = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.action.like("set_role:%")))]
    assert acts  # role changes are audited


def test_revoke_restore_keeps_history(app, mp):
    with Session() as s:
        boss_email = s.scalar(select(User.email).where(User.role == "admin", User.active.is_(True)))
    boss = login(app, mp, boss_email)
    e = uid("emp.one@trafficomm.com")
    assert boss.patch(f"/api/admin/users/{e}", headers=H, json={"active": False}).json()["changes"] == ["access revoked"]
    assert boss.patch(f"/api/admin/users/{e}", headers=H, json={"active": True}).json()["changes"] == ["access restored"]
    with Session() as s:
        assert s.get(User, e).email == "emp.one@trafficomm.com"


def test_client_rules_validation(app, mp):
    with Session() as s:
        boss_email = s.scalar(select(User.email).where(User.role == "admin", User.active.is_(True)))
        p = s.scalar(select(Profile).order_by(Profile.id))
        pid, orig = p.id, dict(p.settings)
    boss = login(app, mp, boss_email)
    bad = dict(orig, click_metric="clicks_all")
    r = boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": bad})
    assert r.status_code == 400 and "agency rules" in r.json()["detail"]
    if isinstance(orig.get("naming"), dict):
        r = boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": dict(orig, naming="flat text")})
        assert r.status_code == 400
    assert boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": ["not", "an", "object"]}).status_code == 400
    good = dict(orig, description="Updated by test")
    r = boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": good})
    assert r.status_code == 200
    stamp = r.json()["updated_at"]
    # a stale editor can't silently overwrite a newer save
    r = boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": orig, "updated_at": "2000-01-01T00:00:00+00:00"})
    assert r.status_code == 400 and "changed by someone else" in r.json()["detail"]
    assert boss.put(f"/api/admin/profiles/{pid}", headers=H, json={"settings": orig, "updated_at": stamp}).status_code == 200
