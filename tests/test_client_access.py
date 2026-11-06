"""Clients and per-client access: admins manage clients and assign employees to them; employees only
see and work on the clients they are assigned to. Also covers deleting users and the one-time data
migration (legacy @trafficomm.local accounts removed, existing employees keep their clients)."""
import pytest
from sqlalchemy import select

from app.db import AppSetting, AuditLog, Base, Client, Profile, Session, Task, User, UserClient, engine, migrate_access, reset_engine
from tests import zoho_fake

pytestmark = pytest.mark.web
H = {"X-Requested-With": "fetch"}
BOSS = "matheen.m@trafficomm.com"


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
    assert r.status_code == 303 and not r.headers["location"].startswith("/login"), r.headers["location"]
    return c


def uid(email):
    with Session() as s:
        return s.scalar(select(User.id).where(User.email == email))


def cid(name):
    with Session() as s:
        return s.scalar(select(Client.id).where(Client.name == name))


ATC = "Around The Clock Media"


@pytest.fixture(scope="module")
def boss(app, mp):
    return login(app, mp, BOSS)


def test_admin_adds_client_with_rules_and_employees(app, mp, boss):
    assert boss.post("/api/admin/users", headers=H, json={"email": "janani@trafficomm.com", "name": "Janani", "client_ids": []}).status_code == 200
    assert boss.post("/api/admin/users", headers=H, json={"email": "ravi@trafficomm.com", "name": "Ravi", "client_ids": [cid(ATC)]}).status_code == 200
    r = boss.post("/api/admin/clients", headers=H, json={"name": "  Bloom   Retail ", "short": "BLM", "user_ids": [uid("janani@trafficomm.com")]})
    assert r.status_code == 200, r.text
    assert r.json()["client"]["name"] == "Bloom Retail"
    with Session() as s:
        c = s.scalar(select(Client).where(Client.name == "Bloom Retail"))
        assert s.scalar(select(Profile).where(Profile.client_id == c.id)).name == "Bloom Retail rules"
        assert s.scalars(select(UserClient.user_id).where(UserClient.client_id == c.id)).all() == [uid("janani@trafficomm.com")]
    # duplicate names (any case) and blank names are refused
    assert boss.post("/api/admin/clients", headers=H, json={"name": "bloom retail"}).status_code == 400
    assert boss.post("/api/admin/clients", headers=H, json={"name": "   "}).status_code == 400
    page = boss.get("/admin").text
    assert "Add client" in page and "Bloom Retail" in page and "Employees with access" in page
    users = boss.get("/admin?partial=users").text
    row = users.split('data-email="janani@trafficomm.com"')[1].split("</tr>")[0]
    assert "Bloom Retail" in row and ATC not in row


def test_employee_only_sees_assigned_clients(app, mp, boss):
    janani, ravi = login(app, mp, "janani@trafficomm.com"), login(app, mp, "ravi@trafficomm.com")
    bloom, atc = cid("Bloom Retail"), cid(ATC)
    # the new-report form only offers Janani her one client
    form = janani.get("/tasks/new").text
    assert "Bloom Retail" in form and ATC not in form
    # she can't create or move a task into a client she isn't assigned to
    assert janani.post("/api/tasks", headers=H, json={"client_id": atc}).status_code == 400
    tid = janani.post("/api/tasks", headers=H, json={}).json()["id"]
    with Session() as s:
        assert s.get(Task, tid).client_id == bloom  # defaults to her only client
    assert janani.patch(f"/api/tasks/{tid}", headers=H, json={"client_id": atc}).status_code == 400
    assert janani.patch(f"/api/tasks/{tid}", headers=H, json={"title": "Bloom October"}).status_code == 200
    # Ravi works on ATC: he can't see Janani's Bloom task, and she can't see his ATC task
    rtid = ravi.post("/api/tasks", headers=H, json={"client_id": atc}).json()["id"]
    ravi.patch(f"/api/tasks/{rtid}", headers=H, json={"title": "ATC October"})
    assert ravi.get(f"/tasks/{tid}/edit").status_code == 404
    assert ravi.patch(f"/api/tasks/{tid}", headers=H, json={"title": "x"}).status_code == 404
    assert janani.get(f"/tasks/{rtid}/edit").status_code == 404
    jd, rd = janani.get("/tasks").text, ravi.get("/tasks").text
    assert "Bloom October" in jd and "ATC October" not in jd
    assert "ATC October" in rd and "Bloom October" not in rd
    # the dashboard's client filter lists only her clients
    assert f'<option value="{atc}"' not in jd and f'<option value="{bloom}"' in jd
    # admins see everything
    bd = boss.get("/tasks").text
    assert "Bloom October" in bd and "ATC October" in bd


def test_access_is_shared_within_a_client_and_removable(app, mp, boss):
    janani, ravi = login(app, mp, "janani@trafficomm.com"), login(app, mp, "ravi@trafficomm.com")
    bloom, atc = cid("Bloom Retail"), cid(ATC)
    with Session() as s:
        tid = s.scalar(select(Task.id).where(Task.title == "Bloom October"))
    # give Ravi Bloom too: he now sees the Bloom task created by Janani, with her as the owner
    r = boss.patch(f"/api/admin/users/{uid('ravi@trafficomm.com')}", headers=H, json={"client_ids": [atc, bloom]})
    assert r.status_code == 200 and "added Bloom Retail" in r.json()["message"]
    assert ravi.get(f"/tasks/{tid}/edit").status_code == 200
    assert "Bloom October" in ravi.get("/tasks").text
    assert "Bloom October" not in ravi.get("/tasks?mine=1").text  # "My tasks" is still only his own
    # removing the client takes the access away straight away
    r = boss.patch(f"/api/admin/users/{uid('ravi@trafficomm.com')}", headers=H, json={"client_ids": [atc]})
    assert "removed Bloom Retail" in r.json()["message"]
    assert ravi.get(f"/tasks/{tid}/edit").status_code == 404
    # editing a client's employee list works the same way, from the client side
    r = boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"user_ids": [uid("janani@trafficomm.com"), uid("ravi@trafficomm.com")]})
    assert r.status_code == 200 and "added Ravi" in r.json()["message"]
    assert ravi.get(f"/tasks/{tid}/edit").status_code == 200
    boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"user_ids": [uid("janani@trafficomm.com")]})
    assert ravi.get(f"/tasks/{tid}/edit").status_code == 404
    with Session() as s:
        actions = s.scalars(select(AuditLog.action).where(AuditLog.action.in_(["grant_client", "revoke_client"]))).all()
    assert "grant_client" in actions and "revoke_client" in actions


def test_employee_without_clients(app, mp, boss):
    boss.post("/api/admin/users", headers=H, json={"email": "new.joiner@trafficomm.com", "name": "New Joiner"})
    c = login(app, mp, "new.joiner@trafficomm.com")
    page = c.get("/").text
    assert "No clients assigned yet" in page and "New reporting task" not in page
    assert c.get("/tasks/new").status_code == 403
    assert c.post("/api/tasks", headers=H, json={}).status_code == 400
    users = boss.get("/admin?partial=users").text
    assert "No clients" in users.split('data-email="new.joiner@trafficomm.com"')[1].split("</tr>")[0]


def test_rename_and_archive_client(app, mp, boss):
    janani = login(app, mp, "janani@trafficomm.com")
    bloom = cid("Bloom Retail")
    r = boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"name": "Bloom Retail Group"})
    assert r.status_code == 200 and "renamed" in r.json()["message"]
    assert boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"name": ATC}).status_code == 400  # name taken
    assert boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"archived": True}).json()["client"]["archived"] is True
    # archived: no new reports for it, but existing tasks stay visible
    assert janani.get("/tasks/new").status_code == 403  # it was her only client
    assert "Bloom October" in janani.get("/tasks").text
    boss.patch(f"/api/admin/clients/{bloom}", headers=H, json={"archived": False})
    assert "Bloom Retail Group" in janani.get("/tasks/new").text
    # the seed doesn't bring back renamed clients
    from scripts.seed import main as seed
    seed()
    with Session() as s:
        assert s.scalar(select(Client.id).where(Client.name == "Bloom Retail")) is None


def test_client_admin_is_admin_only(app, mp):
    janani = login(app, mp, "janani@trafficomm.com")
    assert janani.post("/api/admin/clients", headers=H, json={"name": "Sneaky"}).status_code == 403
    assert janani.patch(f"/api/admin/clients/{cid(ATC)}", headers=H, json={"user_ids": [uid("janani@trafficomm.com")]}).status_code == 403
    assert janani.patch(f"/api/admin/users/{uid('janani@trafficomm.com')}", headers=H, json={"client_ids": [cid(ATC)]}).status_code == 403
    assert janani.delete(f"/api/admin/users/{uid('ravi@trafficomm.com')}", headers=H).status_code == 403
    assert janani.get("/admin?partial=clients").status_code == 403


def test_delete_user_moves_tasks_and_protects_admins(app, mp, boss):
    ravi_id = uid("ravi@trafficomm.com")
    with Session() as s:
        rtid = s.scalar(select(Task.id).where(Task.title == "ATC October"))
    assert boss.delete(f"/api/admin/users/{uid(BOSS)}", headers=H).status_code == 400  # not yourself
    r = boss.delete(f"/api/admin/users/{ravi_id}", headers=H)
    assert r.status_code == 200 and "now belongs to you" in r.json()["message"]
    with Session() as s:
        assert s.get(User, ravi_id) is None
        assert s.get(Task, rtid).owner_id == uid(BOSS)
        assert not s.scalars(select(UserClient).where(UserClient.user_id == ravi_id)).all()
        assert s.scalar(select(AuditLog.target).where(AuditLog.action == "delete_user")).startswith("ravi@trafficomm.com")
    # another admin can be deleted (the deleting admin always remains as an active admin)
    boss.post("/api/admin/users", headers=H, json={"email": "second.admin@trafficomm.com", "role": "admin"})
    assert boss.delete(f"/api/admin/users/{uid('second.admin@trafficomm.com')}", headers=H).status_code == 200
    assert boss.delete("/api/admin/users/999999", headers=H).status_code == 400


def test_migration_removes_legacy_accounts_and_keeps_existing_access(app, mp):
    with Session() as s:
        boss = s.scalar(select(User).where(User.email == BOSS))
        legacy_admin = User(email="admin@trafficomm.local", name="Workspace Admin", role="admin", password_hash="$2b$x")
        sara = User(email="employee@trafficomm.local", name="Sara (Employee)", role="employee", password_hash="$2b$x", active=False)
        s.add_all([legacy_admin, sara])
        s.flush()
        t = Task(client_id=s.scalar(select(Client.id).where(Client.name == ATC)), owner_id=sara.id, title="Old legacy task", status="draft")
        s.add(t)
        s.add(UserClient(user_id=sara.id, client_id=t.client_id))
        s.commit()
        tid, boss_id = t.id, boss.id
    migrate_access()
    migrate_access()  # idempotent
    with Session() as s:
        assert not s.scalars(select(User).where(User.email.like("%@trafficomm.local"))).all()
        assert s.get(Task, tid).owner_id == boss_id  # the legacy user's task is kept, now owned by the Zoho admin
        assert s.scalar(select(User).where(User.email == BOSS)).role == "admin"
        assert len(s.scalars(select(AuditLog).where(AuditLog.action == "delete_legacy_user")).all()) == 2


def test_first_migration_grants_existing_employees_every_client(app, mp):
    with Session() as s:
        s.delete(s.get(AppSetting, "client_access_v1"))
        s.query(UserClient).delete()
        s.commit()
        emp = s.scalar(select(User).where(User.email == "janani@trafficomm.com"))
        n_clients = len(s.scalars(select(Client.id)).all())
    migrate_access()
    with Session() as s:
        assert len(s.scalars(select(UserClient).where(UserClient.user_id == emp.id)).all()) == n_clients
        assert not s.scalars(select(UserClient).join(User, User.id == UserClient.user_id).where(User.role == "admin")).all()
    # after the first run, new employees start with no clients (the flag is set)
    with Session() as s:
        s.query(UserClient).filter(UserClient.user_id == emp.id).delete()
        s.commit()
    migrate_access()
    with Session() as s:
        assert not s.scalars(select(UserClient).where(UserClient.user_id == emp.id)).all()
