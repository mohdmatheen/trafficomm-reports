"""End-to-end acceptance tests through the web app, Postgres job queue and worker subprocesses
(SYNTHETIC fixtures). Requires the Postgres test database (see conftest.py) and LibreOffice."""
import io
import threading
import time

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs as J
from app.db import Artifact, Base, Job, Session, Task, TaskVersion, engine, now, reset_engine
from tests.helpers import fx

pytestmark = pytest.mark.web
H = {"X-Requested-With": "fetch"}


@pytest.fixture(scope="module")
def app_client():
    reset_engine()
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    from scripts.seed import main as seed
    seed()
    from app.web import app
    return app


def login(app, email, pw):
    c = TestClient(app)
    r = c.post("/login", data={"email": email, "password": pw}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture(scope="module")
def emp(app_client):
    return login(app_client, "employee@trafficomm.local", "employee-dev-password")


@pytest.fixture(scope="module")
def emp2(app_client):
    return login(app_client, "employee2@trafficomm.local", "employee2-dev-password")


@pytest.fixture(scope="module")
def admin(app_client):
    return login(app_client, "admin@trafficomm.local", "admin-dev-password")


def drain():
    """Run queued jobs the way the worker does (subprocess per job, limits, heartbeats)."""
    n = 0
    while (job := J.claim("pytest")) is not None:
        J.run_one(job, "pytest")
        n += 1
    return n


def new_task(c, files, deliverables=("excel",), title="Acme September", instructions="Monthly report. Use link clicks."):
    tid = c.post("/api/tasks", json={}, headers=H).json()["id"]
    r = c.patch(f"/api/tasks/{tid}", headers=H, json={"title": title, "brand": "Acme Dates", "period_start": "2026-09-01", "period_end": "2026-09-30",
                                                      "currency": "AED", "deliverables": list(deliverables), "instructions": instructions})
    assert r.status_code == 200, r.text
    fids = []
    for path, role in files:
        r = c.post(f"/api/tasks/{tid}/files", headers=H, files={"file": (path.split("/")[-1], open(path, "rb"))}, data={"role": role})
        assert r.status_code == 200, r.text
        fids.append(r.json()["id"])
    return tid, fids


def state(c, tid):
    return c.get(f"/api/tasks/{tid}/state").json()


def answer_all(c, tid, choices):
    with Session() as s:
        from app.db import Question
        qs = s.scalars(select(Question).where(Question.task_id == tid, Question.status == "open")).all()
        ans = {str(q.id): choices.get(q.key, q.default) for q in qs}
    r = c.post(f"/api/tasks/{tid}/answers", headers=H, json={"answers": ans, "save": {}})
    assert r.status_code == 200, r.text


def artifacts(tid, version_no):
    with Session() as s:
        v = s.scalar(select(TaskVersion).where(TaskVersion.task_id == tid, TaskVersion.number == version_no))
        return v, s.scalars(select(Artifact).where(Artifact.version_id == v.id)).all()


# ----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def workflow(emp):
    """Full workflow: create -> upload -> submit -> clarification -> answer -> ready."""
    tid, fids = new_task(emp, [(fx("meta_acme_sept.csv"), "raw"), (fx("tiktok_acme_sept_aed.xlsx"), "raw"), (fx("acme_client_deck.pptx"), "client_deck")],
                         deliverables=("excel", "deck_update", "pdf"))
    r = emp.post(f"/api/tasks/{tid}/submit", headers=H)
    assert r.status_code == 200, r.text
    assert state(emp, tid)["processing"]
    drain()
    assert state(emp, tid)["status"] == "awaiting_clarification"
    answer_all(emp, tid, {"conversion_metric": "leads_meta_form"})
    drain()
    return tid


def test_full_workflow_ready(emp, workflow):
    tid = workflow
    assert state(emp, tid)["status"] == "ready_for_review"
    page = emp.get(f"/tasks/{tid}")
    assert page.status_code == 200 and "Validation" in page.text and "Download" in page.text
    v, arts = artifacts(tid, 1)
    kinds = {a.kind for a in arts}
    assert {"excel_consolidated", "deck_update", "pdf", "record", "preview"} <= kinds
    assert v.result["validation"]["overall"] in ("passed", "warnings")
    # download goes through a short-lived signed link bound to the user
    a = next(a for a in arts if a.kind == "excel_consolidated")
    r = emp.get(f"/artifacts/{a.id}/download", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/d/")
    f = emp.get(r.headers["location"])
    assert f.status_code == 200 and f.content[:2] == b"PK"
    pv = next(a for a in arts if a.kind == "preview")
    assert emp.get(f"/artifacts/{pv.id}/preview").headers["content-type"] == "image/png"


def test_dashboard_and_approve(emp, workflow):
    d = emp.get("/")
    assert "Acme September" in d.text
    v, _ = artifacts(workflow, 1)
    assert emp.post(f"/api/tasks/{workflow}/approve", headers=H, json={"version_id": v.id}).status_code == 200
    assert state(emp, workflow)["status"] == "approved"


def test_revision_changes_breakdown_and_keeps_versions(emp, workflow):
    tid = workflow
    v1, arts1 = artifacts(tid, 1)
    r = emp.post(f"/api/tasks/{tid}/feedback", headers=H, json={"text": "Split tabs by country and use all clicks instead of link clicks", "base_version_id": v1.id})
    assert r.status_code == 200 and r.json()["ok"], r.text
    drain()
    assert state(emp, tid)["status"] == "ready_for_review"
    v2, arts2 = artifacts(tid, 2)
    ops = {(o["op"], o["value"]) for o in v2.change_summary["ops"]}
    assert ("split_tabs_by", "country") in ops and ("set_click_metric", "clicks_all") in ops
    assert any(f.startswith("Clicks") for f in v2.change_summary["figures"])
    xl = next(a for a in arts2 if a.kind == "excel_consolidated")
    wb = openpyxl.load_workbook(xl.stored_path)
    split = [n for n in wb.sheetnames if "UAE" in n or "KSA" in n or "Saudi" in n or "Emirates" in n]
    assert len(split) >= 2, wb.sheetnames  # one tab per country
    wb1 = openpyxl.load_workbook(next(a for a in arts1 if a.kind == "excel_consolidated").stored_path)
    assert not [n for n in wb1.sheetnames if n in split]
    # earlier version retained and still downloadable
    a1 = next(a for a in arts1 if a.kind == "excel_consolidated")
    r = emp.get(f"/artifacts/{a1.id}/download")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert v1.result["summary"]["totals"]["clicks"] != v2.result["summary"]["totals"]["clicks"]


def test_revision_with_additional_upload(emp, workflow):
    tid = workflow
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": ("snapchat_acme_sept.csv", open(fx("snapchat_acme_sept.csv"), "rb"))}, data={"for_revision": "1"})
    assert r.status_code == 200
    fid = r.json()["id"]
    r = emp.post(f"/api/tasks/{tid}/feedback", headers=H, json={"text": "Include the new Snapchat file I uploaded", "file_ids": [fid]})
    assert r.json()["ok"], r.text
    drain()
    if state(emp, tid)["status"] == "awaiting_clarification":
        answer_all(emp, tid, {"equiv:snapchat:swipe_ups=link_clicks": "no"})
        drain()
    assert state(emp, tid)["status"] == "ready_for_review"
    v3, _ = artifacts(tid, 3)
    assert v3.change_summary["new_files"] == ["snapchat_acme_sept.csv"]
    plats = [r["platform"] for r in v3.result["summary"]["tables"]["platform"]["rows"]]
    assert "snapchat" in plats
    assert len(emp.get(f"/tasks/{tid}").text.split('class="ver ')) - 1 == 3  # all versions listed


def test_unsupported_feedback_gets_explanation(emp, workflow):
    r = emp.post(f"/api/tasks/{workflow}/feedback", headers=H, json={"text": "make it pop more"})
    assert r.status_code == 200 and r.json()["ok"] is False and "Supported changes" in r.json()["summary"]


def test_cross_user_access_denied(emp2, workflow):
    tid = workflow
    _, arts = artifacts(tid, 1)
    a = arts[0]
    assert emp2.get(f"/tasks/{tid}").status_code == 404
    assert emp2.get(f"/api/tasks/{tid}/state").status_code == 404
    assert emp2.get(f"/artifacts/{a.id}/download", follow_redirects=False).status_code == 404
    assert emp2.get(f"/artifacts/{a.id}/preview").status_code == 404
    assert emp2.post(f"/api/tasks/{tid}/feedback", headers=H, json={"text": "Add a breakdown by country"}).status_code == 404
    assert emp2.patch(f"/api/tasks/{tid}", headers=H, json={"title": "x"}).status_code == 404
    assert "Acme September" not in emp2.get("/").text
    assert emp2.get("/admin").status_code == 403
    # a signed link minted for another user is refused
    from app.storage import sign_download
    with Session() as s:
        owner = s.get(Task, tid).owner_id
    assert emp2.get(f"/d/{sign_download(a.id, owner)}").status_code == 403


def test_admin_sees_all(admin, workflow):
    assert admin.get(f"/tasks/{workflow}").status_code == 200
    assert "Acme September" in admin.get("/").text
    assert admin.get("/admin").status_code == 200


def test_unauthenticated_redirect_and_csrf(app_client, emp):
    anon = TestClient(app_client)
    assert anon.get("/", follow_redirects=False).status_code == 303
    assert anon.get("/api/tasks/1/state").status_code in (401, 403)
    assert emp.post("/api/tasks", json={}).status_code == 403  # missing CSRF header


def test_deck_update_requires_deck(emp):
    tid, _ = new_task(emp, [(fx("meta_acme_sept.csv"), "raw")], deliverables=("deck_update",))
    r = emp.post(f"/api/tasks/{tid}/submit", headers=H)
    assert r.status_code == 400 and "no client PPTX" in r.json()["detail"]


def test_upload_validation(emp):
    tid = emp.post("/api/tasks", json={}, headers=H).json()["id"]
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": ("macro.xlsm", io.BytesIO(b"PK\x03\x04junk"))}, data={"role": "raw"})
    assert r.status_code == 400
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": ("broken.xlsx", io.BytesIO(b"not a zip"))}, data={"role": "raw"})
    assert r.status_code == 400
    r = emp.post(f"/api/tasks/{tid}/files", headers=H, files={"file": ("fake.pptx", open(fx("meta_acme_sept.csv"), "rb"))}, data={"role": "client_deck"})
    assert r.status_code == 400


def test_refresh_during_processing_and_cancel(emp, monkeypatch):
    monkeypatch.setattr(J, "HEARTBEAT", 0.5)
    tid, _ = new_task(emp, [(fx("meta_acme_sept.csv"), "raw"), (fx("acme_client_deck.pptx"), "client_deck")],
                      deliverables=("excel", "deck_update", "deck_new", "pdf"), title="Cancel me")
    assert emp.post(f"/api/tasks/{tid}/submit", headers=H).status_code == 200
    job = J.claim("pytest")
    th = threading.Thread(target=J.run_one, args=(job, "pytest"))
    th.start()
    # refreshing the workspace while the job runs is safe and shows it as processing
    r = emp.get(f"/tasks/{tid}")
    assert r.status_code == 200 and "updates automatically" in r.text
    assert state(emp, tid)["processing"]
    assert emp.post(f"/api/tasks/{tid}/cancel", headers=H).status_code == 200  # running -> cancel_requested -> subprocess killed
    while th.is_alive():
        assert emp.get(f"/tasks/{tid}").status_code == 200
        time.sleep(0.2)
    th.join()
    assert state(emp, tid)["status"] == "cancelled"
    with Session() as s:
        assert s.get(Job, job.id).status == "cancelled"
    assert "processing was cancelled" in emp.get(f"/tasks/{tid}").text


def test_cancel_queued_job(emp):
    tid, _ = new_task(emp, [(fx("meta_acme_sept.csv"), "raw")], title="Cancel queued")
    emp.post(f"/api/tasks/{tid}/submit", headers=H)
    assert emp.post(f"/api/tasks/{tid}/cancel", headers=H).status_code == 200
    assert state(emp, tid)["status"] == "cancelled"
    assert J.claim("pytest") is None  # nothing left to run
    assert emp.post(f"/api/tasks/{tid}/retry", headers=H).status_code == 200
    drain()
    assert state(emp, tid)["status"] in ("awaiting_clarification", "ready_for_review")


def test_idempotent_enqueue_and_stale_recovery(emp):
    tid, _ = new_task(emp, [(fx("meta_acme_sept.csv"), "raw")], title="Recovery")
    emp.post(f"/api/tasks/{tid}/submit", headers=H)
    from app import service as S
    with Session() as s:
        v = s.scalar(select(TaskVersion).where(TaskVersion.task_id == tid))
        S.enqueue(s, v, "submitted")  # same inputs -> no duplicate job
        s.commit()
        assert len(s.scalars(select(Job).where(Job.version_id == v.id)).all()) == 1
    job = J.claim("dead-worker")  # simulate a worker that crashed mid-job
    import datetime as dt
    with Session() as s:
        j = s.get(Job, job.id)
        j.heartbeat_at = now() - dt.timedelta(seconds=600)
        s.commit()
    assert J.recover_stale() == 1
    with Session() as s:
        assert s.get(Job, job.id).status == "queued"
    drain()
    assert state(emp, tid)["status"] in ("awaiting_clarification", "ready_for_review")
    with Session() as s:
        assert s.get(Job, job.id).attempts == 2


def test_retention_deletes_old_tasks(admin, emp):
    tid, _ = new_task(emp, [(fx("meta_acme_sept.csv"), "raw")], title="Old task")
    import datetime as dt
    with Session() as s:
        t = s.get(Task, tid)
        t.updated_at = now() - dt.timedelta(days=400)
        s.commit()
    r = admin.post("/admin/retention", data={"days": "180", "run": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with Session() as s:
        assert s.get(Task, tid).deleted_at is not None
    assert emp.get(f"/tasks/{tid}").status_code == 404
