"""Business logic shared by the web app and the worker."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import shutil

from sqlalchemy import select

from .config import settings
from .db import (AppSetting, Artifact, AuditLog, Client, Job, Message, Profile, Question, Session, StageEvent, Task, TaskFile,
                 TaskVersion, User, now)
from .engine import ai as AI
from .engine.inspect import inspect_file
from .pipeline import Cancelled, VersionInput, run_version
from .storage import safe_name, sha256, storage

log = logging.getLogger("trafficomm.service")

ALLOWED_EXT = {".csv", ".xlsx", ".pptx", ".zip"}
ROLE_EXT = {"raw": {".csv", ".xlsx"}, "template": {".xlsx"}, "client_deck": {".pptx"}, "creatives": {".zip"}}
STAGE_TO_STATUS = {"inspecting": "inspecting", "mapping": "mapping", "awaiting_clarification": "awaiting_clarification",
                   "calculating": "calculating", "generating": "generating", "validating": "validating",
                   "ready_for_review": "ready_for_review", "validation_blocked": "validation_blocked", "blocked": "validation_blocked"}
STATUS_LABEL = {"draft": "Draft", "queued": "Uploaded - queued", "inspecting": "Inspecting files", "mapping": "Mapping data",
                "awaiting_clarification": "Awaiting clarification", "calculating": "Calculating", "generating": "Generating files",
                "validating": "Validating", "ready_for_review": "Ready for review", "validation_blocked": "Validation blocked",
                "approved": "Approved by employee", "failed": "Failed", "cancelled": "Cancelled"}
PROCESSING = {"queued", "inspecting", "mapping", "calculating", "generating", "validating"}


class UserError(Exception):
    pass


def audit(s, user_id, action, target):
    s.add(AuditLog(user_id=user_id, action=action, target=str(target)[:200]))


def profile_settings(task: Task) -> dict:
    return dict(task.profile.settings) if task.profile else {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def add_file(s, task: Task, user: User, tmp_path: str, original_name: str, role: str | None, version_no: int) -> TaskFile:
    ext = os.path.splitext(original_name)[1].lower()
    size = os.path.getsize(tmp_path)
    if size > settings.max_upload_bytes:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' is {size / 1e6:.1f} MB; the limit is {settings.max_upload_bytes / 1e6:.0f} MB.")
    if size == 0:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' is empty.")
    if ext not in ALLOWED_EXT:
        os.remove(tmp_path)
        raise UserError(f"'{original_name}': only CSV, XLSX, PPTX and ZIP (creative images) files are accepted. Macro-enabled files (.xlsm/.pptm) are rejected.")
    insp = inspect_file(tmp_path, original_name, profile_settings(task))
    if not insp.get("ok", True):
        os.remove(tmp_path)
        raise UserError(f"'{original_name}' was rejected: {insp.get('error')}")
    role = role or insp.get("role_suggested") or "raw"
    if ext not in ROLE_EXT.get(role, set()):
        role = insp.get("role_suggested") or "raw"
    f = TaskFile(task_id=task.id, role=role, original_name=safe_name(original_name), stored_path="", size=size,
                 sha256=insp["sha256"], uploaded_by=user.id, added_in_version=version_no, inspection=_json_safe(insp))
    s.add(f)
    s.flush()
    f.stored_path = storage.save_upload(task.id, f.id, original_name, tmp_path)
    audit(s, user.id, "upload", f"task {task.id} file {f.id}")
    return f


def file_summary(f: TaskFile) -> dict:
    insp = f.inspection or {}
    sheets = []
    for sh in insp.get("sheets", []):
        sheets.append({"name": sh["name"], "rows": sh.get("row_count"), "platform": sh.get("platform_label") or sh.get("platform"),
                       "header_row": sh.get("header_row"), "data_sheet": sh.get("is_data_sheet"),
                       "currency": (sh.get("currency") or {}).get("value"), "dates": sh.get("dates", {}),
                       "summary_blocks": len(sh.get("summary_blocks", [])), "total_rows": len(sh.get("total_rows", [])),
                       "unresolved": sh.get("unresolved", []), "formula_cells": sh.get("formula_cells", 0)})
    deck = insp.get("deck")
    return {"id": f.id, "name": f.original_name, "size": f.size, "role": f.role, "sheets": sheets, "warnings": insp.get("warnings", []),
            "deck": deck and {"slides": deck["slide_count"], "summary": deck["summary"], "aspect": deck["aspect"], "unsupported": deck["unsupported_total"]},
            "images": len(insp.get("images", [])) if insp.get("images") else None, "added_in_version": f.added_in_version, "excluded": f.excluded}


def _json_safe(o):
    return json.loads(json.dumps(o, default=str))


# ---------------------------------------------------------------------------
# Versions / jobs
# ---------------------------------------------------------------------------
def enqueue(s, version: TaskVersion, reason: str) -> Job:
    key = f"v{version.id}:" + hashlib.sha256(json.dumps([version.answers, version.plan_overrides, version.file_ids, reason], sort_keys=True, default=str).encode()).hexdigest()[:24]
    existing = s.scalar(select(Job).where(Job.idempotency_key == key))
    if existing and existing.status in ("queued", "running", "succeeded"):
        return existing  # idempotent: same inputs never create a duplicate job/outputs
    if existing:
        existing.status, existing.attempts, existing.error, existing.run_after, existing.cancel_requested = "queued", 0, None, now(), False
        return existing
    j = Job(task_id=version.task_id, version_id=version.id, idempotency_key=key, max_attempts=settings.job_max_attempts)
    s.add(j)
    version.status = "queued"
    version.stage = "uploaded"
    s.add(StageEvent(version_id=version.id, stage="uploaded", detail=f"Queued ({reason})"))
    return j


def submit_task(s, task: Task, user: User) -> TaskVersion:
    if task.status not in ("draft",):
        raise UserError("Task was already submitted")
    if not task.title.strip():
        raise UserError("Task title is required")
    if not task.deliverables:
        raise UserError("Choose at least one deliverable")
    files = s.scalars(select(TaskFile).where(TaskFile.task_id == task.id, TaskFile.removed.is_(False))).all()
    if not any(f.role == "raw" for f in files):
        raise UserError("Upload at least one raw report (CSV/XLSX)")
    v = TaskVersion(task_id=task.id, number=1, kind="initial", file_ids=[f.id for f in files], created_by=user.id, answers={}, plan_overrides={})
    s.add(v)
    s.flush()
    task.current_version_id = v.id
    task.status = "queued"
    if task.instructions.strip():
        s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="instructions", text=task.instructions))
    enqueue(s, v, "submitted")
    audit(s, user.id, "submit", f"task {task.id}")
    return v


def answer_questions(s, task: Task, user: User, answers: dict, save_profile: dict) -> TaskVersion:
    v = s.get(TaskVersion, task.current_version_id)
    qs = {q.id: q for q in s.scalars(select(Question).where(Question.version_id == v.id, Question.status == "open")).all()}
    new_answers = dict(v.answers or {})
    texts = []
    for qid, val in answers.items():
        q = qs.get(int(qid))
        if not q or val in (None, ""):
            continue
        val = str(val).strip()[:300]
        if q.allow_free_text:
            try:
                if float(val) <= 0:
                    raise ValueError
            except ValueError:
                raise UserError(f"'{val}' is not a valid positive number for: {q.text[:80]}")
        elif q.options and val not in [o["value"] for o in q.options]:
            raise UserError("Invalid option selected")
        q.status, q.answer, q.answered_by, q.answered_at = "answered", val, user.id, now()
        new_answers[q.key] = val
        label = next((o["label"] for o in q.options if o["value"] == val), val)
        texts.append(f"Q: {q.text}\nA: {label}")
        if save_profile.get(str(qid)) and task.profile:
            _save_answer_to_profile(s, task, q, val)
            q.saved_to_profile = True
    if not texts:
        raise UserError("No answers given")
    v.answers = new_answers
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="answer", text="\n\n".join(texts)))
    if not [q for q in qs.values() if q.status == "open"] or True:
        enqueue(s, v, "answers")
        task.status = "queued"
    return v


def _save_answer_to_profile(s, task, q: Question, val: str):
    p = s.get(Profile, task.profile_id)
    st = dict(p.settings or {})
    if q.key == "conversion_metric":
        st["conversion_metric"] = None if val == "none" else val
    elif q.key.startswith("equiv:") and val == "yes":
        st["approved_equivalences"] = st.get("approved_equivalences", []) + [["snapchat", "swipe_ups", "link_clicks"]]
    elif q.key.startswith("clicks_def:"):
        st.setdefault("clicks_definitions", {})[q.key.split(":", 1)[1]] = val
    elif q.key.startswith("deckslide:"):
        st.setdefault("deck_slide_answers", {})[q.text.split("'")[1] + "|" + q.key.split(":")[1]] = val
    p.settings = st


def create_revision(s, task: Task, user: User, feedback: str, base_version_id: int | None, new_file_ids: list[int]) -> tuple[TaskVersion | None, str]:
    versions = s.scalars(select(TaskVersion).where(TaskVersion.task_id == task.id).order_by(TaskVersion.number)).all()
    if not versions:
        raise UserError("Task has no version yet")
    base = next((v for v in versions if v.id == base_version_id), None) if base_version_id else versions[-1]
    if not base:
        raise UserError("Unknown base version")
    if versions[-1].status in PROCESSING:
        raise UserError("A version is still processing - wait for it to finish or cancel it")
    provider = AI.get_provider()
    rp = provider.interpret_feedback(feedback, bool(new_file_ids))
    s.add(Message(task_id=task.id, version_id=None, author_id=user.id, kind="feedback", text=feedback + (f"\n[{len(new_file_ids)} new file(s) attached]" if new_file_ids else "")))
    supported = [o for o in rp.ops if o.op != "unsupported"]
    if not supported:
        msg = ("I couldn't map that to a change I can make safely. Supported changes include: switching clicks definition (link vs all clicks), "
               "choosing the CPA conversion, splitting tabs by country/language/platform, adding a breakdown, removing a slide or section, "
               "a shorter summary, removing commentary, or regenerating with newly uploaded files. Please rephrase or add detail.")
        s.add(Message(task_id=task.id, author_id=None, kind="assistant", text=msg))
        return None, msg
    overrides = json.loads(json.dumps(base.plan_overrides or {}))
    answers = dict(base.answers or {})
    for o in supported:
        if o.op == "set_click_metric":
            overrides["click_metric"] = o.value
        elif o.op == "set_conversion_metric":
            answers["conversion_metric"] = o.value
            overrides["conversion_metric"] = o.value
        elif o.op == "split_tabs_by":
            overrides["split_tabs_by"] = o.value
        elif o.op == "add_breakdown":
            overrides.setdefault("breakdowns", []).append([o.value])
        elif o.op == "remove_section":
            overrides.setdefault("remove_sections", []).append(o.value)
            if o.value == "recommendations":
                overrides["include_recommendations"] = False
        elif o.op == "remove_slide":
            overrides.setdefault("remove_slides", []).append(int(o.value))
        elif o.op == "short_summary":
            overrides["short_summary"] = True
        elif o.op == "include_commentary":
            overrides["include_commentary"] = o.value not in ("0", "false")
    base_files = [fid for fid in (base.file_ids or [])]
    file_ids = base_files + [fid for fid in new_file_ids if fid not in base_files]
    v = TaskVersion(task_id=task.id, number=versions[-1].number + 1, base_version_id=base.id, kind="revision", feedback=feedback,
                    revision_ops=[o.model_dump() for o in rp.ops], plan_overrides=overrides, answers=answers, file_ids=file_ids, created_by=user.id)
    s.add(v)
    s.flush()
    task.current_version_id = v.id
    task.status = "queued"
    task.approved_version_id = None if task.approved_version_id == base.id and False else task.approved_version_id
    s.add(Message(task_id=task.id, version_id=v.id, author_id=None, kind="assistant",
                  text=f"Creating v{v.number} from v{base.number}: {rp.summary}." + (" (" + provider.name + ")" if provider.mocked else "")))
    enqueue(s, v, "revision")
    audit(s, user.id, "revision", f"task {task.id} v{v.number}")
    return v, rp.summary


def approve(s, task: Task, user: User, version_id: int):
    v = s.get(TaskVersion, version_id)
    if not v or v.task_id != task.id:
        raise UserError("Unknown version")
    if v.status != "ready_for_review":
        raise UserError("Only a version that is ready for review can be approved")
    v.approved_at, v.approved_by = now(), user.id
    task.approved_version_id = v.id
    task.status = "approved"
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="system", text=f"v{v.number} approved by {user.name}. Files are not sent to the client automatically."))
    audit(s, user.id, "approve", f"task {task.id} v{v.number}")


def cancel(s, task: Task, user: User):
    v = s.get(TaskVersion, task.current_version_id)
    jobs = s.scalars(select(Job).where(Job.version_id == v.id, Job.status.in_(["queued", "running"]))).all()
    if not jobs:
        raise UserError("Nothing is processing")
    for j in jobs:
        if j.status == "queued":
            j.status, j.finished_at = "cancelled", now()
            v.status = "cancelled"
            task.status = "cancelled"
            s.add(StageEvent(version_id=v.id, stage="cancelled", detail="Cancelled before start"))
        else:
            j.cancel_requested = True
    s.add(Message(task_id=task.id, version_id=v.id, author_id=user.id, kind="system", text="Cancellation requested."))


def delete_task(s, task: Task, user: User):
    task.deleted_at = now()
    storage.delete_task(task.id)
    audit(s, user.id, "delete", f"task {task.id}")


def retention_days(s) -> int:
    r = s.get(AppSetting, "retention_days")
    return int(r.value) if r else settings.retention_days_default


def run_retention(s) -> int:
    cutoff = now() - dt.timedelta(days=retention_days(s))
    old = s.scalars(select(Task).where(Task.updated_at < cutoff, Task.deleted_at.is_(None))).all()
    for t in old:
        t.deleted_at = now()
        storage.delete_task(t.id)
        audit(s, None, "retention_delete", f"task {t.id}")
    return len(old)


# ---------------------------------------------------------------------------
# Worker-side processing
# ---------------------------------------------------------------------------
def process_job(job_id: int, worker_id: str = "worker"):
    """Runs inside an isolated subprocess with resource limits (see jobs.py)."""
    with Session() as s:
        job = s.get(Job, job_id)
        v = s.get(TaskVersion, job.version_id)
        task = s.get(Task, v.task_id)
        if task.deleted_at:
            return {"status": "cancelled"}
        prof = profile_settings(task)
        files = s.scalars(select(TaskFile).where(TaskFile.id.in_(v.file_ids or []), TaskFile.removed.is_(False))).all()
        base_artifacts = {}
        if v.base_version_id:
            for a in s.scalars(select(Artifact).where(Artifact.version_id == v.base_version_id)).all():
                base_artifacts[a.kind] = {"path": a.stored_path, "fingerprint": a.fingerprint, "id": a.id}
        answers = dict(v.answers or {})
        for k, val in (prof.get("deck_slide_answers") or {}).items():
            title, idx = k.rsplit("|", 1)
            answers.setdefault(f"deckslide:{idx}", val)
        tdict = {"title": task.title, "client": task.client.name, "brand": task.brand, "period_start": task.period_start, "period_end": task.period_end,
                 "comparison_start": task.comparison_start, "comparison_end": task.comparison_end, "timezone": task.timezone,
                 "currency": task.currency, "deliverables": task.deliverables, "instructions": task.instructions}
        finputs = [{"id": f.id, "name": f.original_name, "path": f.stored_path, "ext": os.path.splitext(f.original_name)[1].lower(),
                    "role": f.role, "inspection": f.inspection, "uploaded_order": f.id, "excluded": f.excluded} for f in files]
        workdir = os.path.join(storage.version_dir(task.id, v.number), "work")
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)  # retry of the same version replaces partial outputs (no duplicates)
        inp = VersionInput(task=tdict, files=finputs, profile=prof, answers=answers, plan_overrides=v.plan_overrides or {},
                           workdir=workdir, version_number=v.number, base_artifacts=base_artifacts, feedback=v.feedback)
        vid, tid = v.id, task.id

    def stage(name, detail):
        with Session() as s2:
            vv = s2.get(TaskVersion, vid)
            tt = s2.get(Task, tid)
            vv.stage = name
            vv.status = STAGE_TO_STATUS.get(name, name)
            tt.status = vv.status
            s2.add(StageEvent(version_id=vid, stage=name, detail=str(detail)[:300]))
            jj = s2.get(Job, job_id)
            jj.heartbeat_at = now()
            s2.commit()

    def check_cancel():
        with Session() as s2:
            if s2.get(Job, job_id).cancel_requested:
                raise Cancelled()

    result = run_version(inp, stage, check_cancel)
    persist_result(job_id, vid, tid, result)
    return {"status": result["status"]}


def persist_result(job_id: int, vid: int, tid: int, result: dict):
    with Session() as s:
        v = s.get(TaskVersion, vid)
        task = s.get(Task, tid)
        # replace artifacts of this version (idempotent on retry)
        for a in s.scalars(select(Artifact).where(Artifact.version_id == vid)).all():
            s.delete(a)
        for a in result.get("artifacts", []):
            s.add(Artifact(version_id=vid, task_id=tid, kind=a["kind"], label=a["label"], filename=os.path.basename(a["path"]),
                           stored_path=a["path"], size=os.path.getsize(a["path"]), sha256=sha256(a["path"]),
                           fingerprint=a.get("fingerprint") or "", reused_from_id=a.get("reused_from")))
        for p in result.get("previews", []):
            s.add(Artifact(version_id=vid, task_id=tid, kind="preview", label=f"{p['artifact_kind']} page {p['page']}", filename=os.path.basename(p["path"]),
                           stored_path=p["path"], size=os.path.getsize(p["path"]), meta={"artifact_kind": p["artifact_kind"], "page": p["page"]}))
        # questions
        existing = {q.key: q for q in s.scalars(select(Question).where(Question.version_id == vid)).all()}
        for q in result.get("questions", []):
            if q["key"] in (v.answers or {}):
                continue
            if q["key"] in existing and existing[q["key"]].status == "open":
                continue
            s.add(Question(task_id=tid, version_id=vid, key=q["key"], text=q["text"], options=q.get("options") or [], default=q.get("default"),
                           allow_free_text=bool(q.get("allow_free_text")), context=q.get("context", "")))
        slim = {k: val for k, val in result.items() if k not in ("artifacts", "previews")}
        v.result = _json_safe(slim)
        v.status = result["status"]
        task.status = result["status"]
        v.error = result.get("error")
        if result["status"] in ("ready_for_review", "validation_blocked"):
            v.completed_at = now()
            if v.base_version_id:
                base = s.get(TaskVersion, v.base_version_id)
                v.change_summary = _json_safe(change_summary(base, v, s))
            n_ok = result["validation"]["counts"]
            msg = (f"v{v.number} is {'ready for review' if result['status'] == 'ready_for_review' else 'BLOCKED by validation'}: "
                   f"{n_ok['pass']} checks passed, {n_ok['warn']} warnings, {n_ok['fail']} failed.")
            fb = result.get("definition_fallback")
            if fb:
                lbl = {"link_clicks": "link clicks", "clicks_all": "all clicks"}
                msg += (f" Note: you asked for {lbl.get(fb['requested'], fb['requested'])}, but none of the uploaded exports contain that column, "
                        f"so {lbl.get(fb['used'], fb['used'])} were kept. Upload an export with that column to switch.")
            s.add(Message(task_id=tid, version_id=vid, author_id=None, kind="assistant", text=msg))
        elif result["status"] == "awaiting_clarification":
            s.add(Message(task_id=tid, version_id=vid, author_id=None, kind="assistant",
                          text=f"I need {len(result['questions'])} answer(s) before generating files. Inspection and mapping are saved."))
        job = s.get(Job, job_id)
        job.heartbeat_at = now()
        s.commit()


def change_summary(base: TaskVersion, v: TaskVersion, s) -> dict:
    out = {"ops": v.revision_ops or [], "figures": [], "artifacts": [], "sheets": {}}
    b = (base.result or {}).get("key_figures", {}).get("total", {})
    n = (v.result or {}).get("key_figures", {}).get("total", {})
    from .engine.calc import METRIC_LABELS
    from .engine import fmt as FM
    cur = (v.result or {}).get("summary", {}).get("currency") or ""
    for m in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa"):
        if b.get(m) != n.get(m):
            out["figures"].append(f"{METRIC_LABELS.get(m, m)}: {FM.fmt_metric(m, b.get(m), cur)} → {FM.fmt_metric(m, n.get(m), cur)}")
    arts = s.scalars(select(Artifact).where(Artifact.version_id == v.id, Artifact.kind.notin_(["preview", "record"]))).all()
    for a in arts:
        out["artifacts"].append(f"{a.label}: " + ("unchanged - same inputs as the previous version" if a.reused_from_id else "regenerated with changes"))
    bfiles = set(base.file_ids or [])
    newf = [fid for fid in (v.file_ids or []) if fid not in bfiles]
    if newf:
        names = [f.original_name for f in s.scalars(select(TaskFile).where(TaskFile.id.in_(newf))).all()]
        out["new_files"] = names
    return out
