"""AI Engineering Agent integration: durable dispatch, status synchronisation, approval policy, rollback.

Backends (server-side only; tokens never reach the browser):
  * routine         Claude Code cloud scheduled task "Trafficomm Reporting Engineering Agent" with an API
                    trigger: POST {CLAUDE_ROUTINE_FIRE_URL} (Bearer CLAUDE_ROUTINE_TOKEN).
  * github_actions  repository_dispatch (event 'engineering-agent') to .github/workflows/engineering-agent.yml,
                    which runs anthropics/claude-code-action (needs ANTHROPIC_API_KEY as a GitHub secret).

The trigger only starts an investigation. Progress is read back from GitHub (pull request with the
issue marker, its checks, merge) and from the deployed commit; an optional HMAC-signed callback lets the
agent report findings or ask the employee questions. A run is 'deployed' only when the running
service's commit contains the merge commit; the issue is resolved only after the employee accepts
the regenerated report.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import time

import httpx
from sqlalchemy import func, select

from ..db import AuditLog, Client, Session, Task, TaskVersion, User, now
from . import controls, knowledge, notify
from .models import EngineeringRun, SupportIssue, SupportMessage
from .sanitize import clean, scrub

log = logging.getLogger("trafficomm.support.engineering")

ACTIVE = ("queued", "dispatched", "pr_open", "checks_failed", "checks_passed", "awaiting_approval", "approved", "merged")
RUNNING = ("dispatched", "pr_open", "checks_failed", "checks_passed")
MAX_DISPATCH_ATTEMPTS = 5
NO_PR_TIMEOUT = dt.timedelta(hours=4)
ROUTINE_NAME = "Trafficomm Reporting Engineering Agent"

# Files whose change needs an ADMINISTRATOR's approval (security, auth, DB migrations, permissions,
# infrastructure, secrets). Calculation logic needs a reporting lead for the client or an admin.
ADMIN_PATHS = [r"^app/auth\.py$", r"^app/zoho\.py$", r"^app/config\.py$", r"^app/db\.py$", r"^app/support/(models|controls|engineering|sanitize)\.py$",
               r"^app/web\.py$", r"^app/support/web\.py$", r"^deploy/", r"^render.*\.yaml$", r"^\.github/", r"^requirements\.txt$",
               r"^scripts/", r"^app/usage\.py$", r"^app/storage\.py$", r"(^|/)\.env", r"secret", r"migrat"]
CALC_PATHS = [r"^app/engine/", r"^app/pipeline\.py$", r"^app/event_pipeline\.py$", r"^app/profiles\.py$", r"^app/service\.py$", r"^app/jobs\.py$"]


class EngineeringError(ValueError):
    pass


def cfg() -> dict:
    e = os.environ.get
    return {"backend": (e("ENGINEERING_BACKEND") or "").strip().lower(), "routine_url": e("CLAUDE_ROUTINE_FIRE_URL"),
            "routine_token": e("CLAUDE_ROUTINE_TOKEN"), "gh_token": e("GITHUB_TOKEN"), "gh_dispatch_token": e("GITHUB_DISPATCH_TOKEN") or e("GITHUB_TOKEN"),
            "repo": e("GITHUB_REPOSITORY", "mohdmatheen/trafficomm-reports"), "callback_secret": e("ENGINEERING_CALLBACK_SECRET"),
            "base_url": (e("PUBLIC_BASE_URL") or "").rstrip("/"), "merge_on_approval": e("ENGINEERING_MERGE_ON_APPROVAL", "0") == "1",
            "deployed_commit": e("RENDER_GIT_COMMIT") or e("DEPLOYED_COMMIT")}


def backend() -> str:
    c = cfg()
    if c["backend"] in ("routine", "github_actions", "off"):
        b = c["backend"]
    elif c["routine_url"] and c["routine_token"]:
        b = "routine"
    elif c["gh_dispatch_token"]:
        b = "github_actions"
    else:
        b = "off"
    if b == "routine" and not (c["routine_url"] and c["routine_token"]):
        return "off"
    if b == "github_actions" and not c["gh_dispatch_token"]:
        return "off"
    return b


def status_text() -> dict:
    c, b = cfg(), backend()
    return {"backend": b, "label": {"routine": f"Claude Code scheduled task ({ROUTINE_NAME})", "github_actions": "GitHub Actions (claude-code-action)",
                                    "off": "Not configured"}[b],
            "status_sync": bool(c["gh_token"]), "callback": bool(c["callback_secret"]), "deployed_commit": (c["deployed_commit"] or "")[:12]}


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def approval_level(paths: list[str]) -> str:
    if any(re.search(p, f) for f in paths for p in ADMIN_PATHS):
        return "admin"
    return "lead"


def touches_calculation(paths: list[str]) -> bool:
    return any(re.search(p, f) for f in paths for p in CALC_PATHS)


def can_approve_run(s, user: User, run: EngineeringRun, issue: SupportIssue) -> tuple[bool, str]:
    if run.status not in ("checks_passed", "awaiting_approval"):
        return False, "The pull request needs passing checks before it can be approved."
    if run.approval_level == "admin" and user.role != "admin":
        return False, "This change touches security, permissions, database, infrastructure or secrets: an administrator must approve it."
    if user.role == "admin":
        return True, ""
    if issue.client_id and knowledge.can_approve(s, user, issue.client_id):
        return True, ""
    return False, "Only a reporting lead for this client or an administrator can approve this fix."


# ---------------------------------------------------------------------------
# request + payload
# ---------------------------------------------------------------------------
def branch_for(issue: SupportIssue) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue.title.lower()).strip("-")[:40]
    return f"claude/{issue.ref.lower()}-{slug}".rstrip("-")


def active_run(s, issue_id: int) -> EngineeringRun | None:
    return s.scalar(select(EngineeringRun).where(EngineeringRun.issue_id == issue_id, EngineeringRun.status.in_(ACTIVE))
                    .order_by(EngineeringRun.id.desc()).limit(1))


def request(s, issue: SupportIssue, user: User, force: bool = False) -> EngineeringRun:
    from .agent import readiness
    from .service import event, set_status
    existing = active_run(s, issue.id)
    if existing:
        return existing  # duplicate submissions attach to the running investigation
    ready, missing = readiness(s, issue)
    if not ready and not (force and user.role == "admin"):
        raise EngineeringError("Before engineering can investigate, add: " + "; ".join(missing) + ".")
    c = s.get(Client, issue.client_id) if issue.client_id else None
    if c is not None and not c.ai_processing_allowed:
        raise EngineeringError(f"{c.name} hasn't been authorised for external AI processing. An administrator must enable it first.")
    g = controls.engineering_gate(s)
    if not g.ok:
        raise EngineeringError("Engineering investigations are paused: " + g.reason)
    n = (s.scalar(select(func.count(EngineeringRun.id)).where(EngineeringRun.issue_id == issue.id)) or 0) + 1
    run = EngineeringRun(issue_id=issue.id, status="queued", idempotency_key=f"{issue.id}:{n}", branch=branch_for(issue), requested_by=user.id)
    s.add(run)
    s.flush()
    set_status(s, issue, "engineering_queued", user.id)
    event(s, issue.id, user.id, "engineering_requested", f"run {run.id}")
    s.add(SupportMessage(issue_id=issue.id, kind="system", text=(
        "Sent for engineering investigation. The engineering agent will look for the root cause on an isolated branch, add a regression "
        "test and open a pull request. Nothing reaches production without approval; you'll be notified at each step.")))
    return run


def build_payload(s, issue: SupportIssue, run: EngineeringRun) -> str:
    from .agent import _history, diagnostics
    c = s.get(Client, issue.client_id) if issue.client_id else None
    rules = knowledge.approved(s, issue.client_id) if issue.client_id else []
    meta = issue.meta or {}
    parts = [
        f"# Trafficomm issue {issue.ref}: {clean(issue.title, 200)}",
        f"Category: {issue.category}. Risk: {issue.risk}. Client: {c.name if c else 'none'}. Task: {issue.task_id or 'none'}"
        f" (version {issue.version_number or '-'}). Engineering run: {run.id}.",
        "## Diagnostic summary", clean(issue.summary, 4000) or "(none)",
        "## Conversation (identifiers removed)", _history(s, issue, limit=30),
    ]
    if meta.get("pilot_answers"):
        parts += ["## Pilot answers from the assigned employee", json.dumps(scrub(meta["pilot_answers"], 1500), indent=1)]
    if meta.get("reconciliation"):
        parts += ["## Local reconciliation (computed on the portal server; aggregates only)", json.dumps(scrub(meta["reconciliation"], 800), indent=1)[:20000]]
    parts += ["## Local diagnostics", json.dumps(diagnostics(s, issue), default=str, indent=1)[:15000]]
    if rules:
        parts += ["## Approved client rules (Client Intelligence)"] + [f"- [{r.key} v{r.version}] {r.title}: {clean(r.body, 600)}" for r in rules[:25]]
    parts += ["## Required output",
              f"- Work on branch `{run.branch}` (create it from main). Open a DRAFT pull request titled `[{issue.ref}] <short description>`.",
              f"- The PR body must contain the line `Trafficomm-Issue: {issue.ref}` and a fenced block starting with ```agent-report containing JSON: "
              '{"root_cause": str, "summary": str, "fix": str, "tests": str, "risk": "low|medium|high", "validated_dates": [str], '
              '"questions_for_employee": [str], "needs_info": bool}.',
              "- If the evidence is insufficient, do not guess: open no code PR; instead report needs_info with precise questions (callback if available).",
              "- Smallest appropriate fix plus a regression test with privacy-safe synthetic fixtures. Never commit client exports or identifiers.",
              "- Update docs/known-issues.md and docs/fixes/ with the root cause. Never merge, deploy, or change secrets, auth or permissions unless the issue is about them."]
    text = "\n\n".join(parts)
    return text[:60000]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _gh(method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
    c = cfg()
    tok = token or c["gh_token"]
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trafficomm-reports"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    return httpx.request(method, url, headers=h, timeout=20, **kw)


class Transient(Exception):
    def __init__(self, msg, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Unknown(Exception):
    """The request may or may not have started a run (timeout after sending). Never retried blindly."""


def fire(issue: SupportIssue, run: EngineeringRun, text: str) -> dict:
    c, b = cfg(), backend()
    try:
        if b == "routine":
            r = httpx.post(c["routine_url"], timeout=30, json={"text": text}, headers={
                "Authorization": f"Bearer {c['routine_token']}", "anthropic-version": "2023-06-01",
                "anthropic-beta": "experimental-cc-routine-2026-04-01", "Content-Type": "application/json"})
        elif b == "github_actions":
            r = _gh("POST", f"/repos/{c['repo']}/dispatches", token=c["gh_dispatch_token"], json={
                "event_type": "engineering-agent", "client_payload": {"issue": issue.ref, "run": run.id, "branch": run.branch, "text": text[:60000]}})
        else:
            raise EngineeringError("No engineering backend is configured (see docs/ai-support.md).")
    except httpx.ConnectError as e:  # nothing was sent: safe to retry
        raise Transient(f"connection failed: {type(e).__name__}") from e
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
        raise Unknown(f"{type(e).__name__} after sending") from e
    if r.status_code == 429 or r.status_code >= 500:
        ra = r.headers.get("retry-after")
        raise Transient(f"HTTP {r.status_code}", float(ra) if ra and ra.replace(".", "").isdigit() else None)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.json().get("message") or r.text[:200]
        except Exception:  # noqa: BLE001
            msg = r.text[:200]
        raise EngineeringError(f"{b} trigger refused (HTTP {r.status_code}): {msg}")
    out = {}
    try:
        out = r.json() if r.content else {}
    except ValueError:
        pass
    return {"session_url": out.get("claude_code_session_url"), "session_id": out.get("claude_code_session_id")}


# ---------------------------------------------------------------------------
# worker tick
# ---------------------------------------------------------------------------
_last_tick = 0.0


def maybe_tick(interval: float = 30.0):
    global _last_tick
    if time.time() - _last_tick < interval:
        return
    _last_tick = time.time()
    try:
        tick()
    except Exception:  # noqa: BLE001 - never stop report processing
        log.exception("engineering tick failed")


def tick() -> dict:
    out = {"dispatched": 0, "synced": 0, "emails": 0}
    with Session() as s:
        out["dispatched"] = dispatch_due(s)
        s.commit()
    with Session() as s:
        out["synced"] = sync_all(s)
        s.commit()
    with Session() as s:
        out["emails"] = notify.send_pending_emails(s)
        s.commit()
    return out


def dispatch_due(s) -> int:
    from .service import event, set_status
    q = select(EngineeringRun).where(EngineeringRun.status == "queued", EngineeringRun.next_attempt_at <= now()).order_by(EngineeringRun.id).limit(5)
    if s.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    runs = s.scalars(q).all()
    n = 0
    for run in runs:
        issue = s.get(SupportIssue, run.issue_id)
        g = controls.engineering_gate(s)
        if not g.ok:
            run.last_error = "Paused: " + g.reason
            run.next_attempt_at = now() + dt.timedelta(minutes=10)
            continue
        running = s.scalar(select(func.count(EngineeringRun.id)).where(EngineeringRun.status.in_(RUNNING))) or 0
        if running >= int(controls.get(s, "engineering_max_concurrent")):
            run.last_error = "Waiting: the maximum number of investigations is already running."
            run.next_attempt_at = now() + dt.timedelta(minutes=5)
            continue
        today = now().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = s.scalar(select(func.count(EngineeringRun.id)).where(EngineeringRun.dispatched_at >= today)) or 0
        if sent_today >= int(controls.get(s, "engineering_daily_cap")):
            run.last_error = "Waiting: today's investigation cap has been reached."
            run.next_attempt_at = today + dt.timedelta(days=1, minutes=5)
            if not (run.agent_report or {}).get("cap_notified"):
                notify.push(s, [a.id for a in notify.admins(s)], "usage_limit", "The daily engineering investigation cap was reached; new runs wait until tomorrow.")
                run.agent_report = {**(run.agent_report or {}), "cap_notified": True}
            continue
        text = build_payload(s, issue, run)
        run.payload_chars, run.attempts, run.backend = len(text), run.attempts + 1, backend()
        try:
            res = fire(issue, run, text)
        except Transient as e:
            run.last_error = f"Attempt {run.attempts}: {e}"
            if run.attempts >= MAX_DISPATCH_ATTEMPTS:
                _fail(s, run, issue, f"Could not start the investigation after {run.attempts} attempts ({e}).")
            else:
                wait = e.retry_after or min(3600, 60 * 2 ** run.attempts)
                run.next_attempt_at = now() + dt.timedelta(seconds=wait)
            continue
        except Unknown as e:
            # the run may have started: don't fire again; the GitHub sync will find its pull request (or time out)
            run.status, run.dispatched_at, run.last_error = "dispatched", now(), f"Trigger outcome unknown ({e}); watching GitHub instead of retrying."
            event(s, issue.id, None, "engineering_dispatch_unknown", str(e))
            continue
        except EngineeringError as e:
            _fail(s, run, issue, str(e))
            continue
        run.status, run.dispatched_at, run.session_url, run.last_error = "dispatched", now(), res.get("session_url"), None
        set_status(s, issue, "engineering_running")
        event(s, issue.id, None, "engineering_dispatched", f"run {run.id} via {run.backend}" + (f" ({res['session_url']})" if res.get("session_url") else ""))
        s.add(SupportMessage(issue_id=issue.id, kind="engineering", text="The engineering agent has started investigating."))
        n += 1
    return n


def _fail(s, run: EngineeringRun, issue: SupportIssue, msg: str):
    from .service import event, set_status
    run.status, run.finished_at, run.last_error = "failed", now(), msg[:2000]
    set_status(s, issue, "unresolved", None, msg[:300])
    event(s, issue.id, None, "engineering_failed", msg)
    s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=f"The automated investigation could not continue: {msg} An administrator has been notified."))
    notify.push(s, [a.id for a in notify.admins(s)], "approval_required", f"{issue.ref}: engineering run failed: {msg[:200]}", issue)
    notify.for_issue(s, issue, "needs_input", f"{issue.ref}: the automated investigation stopped; a person will follow up")


REPORT_RE = re.compile(r"```agent-report\s*(\{.*?\})\s*```", re.S)


def parse_report(body: str | None) -> dict:
    m = REPORT_RE.search(body or "")
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return scrub({k: d.get(k) for k in ("root_cause", "summary", "fix", "tests", "risk", "validated_dates", "questions_for_employee", "needs_info")}, 3000)
    except ValueError:
        return {}


def find_pr(issue: SupportIssue, run: EngineeringRun) -> dict | None:
    c = cfg()
    r = _gh("GET", f"/repos/{c['repo']}/pulls", params={"state": "all", "per_page": 50, "sort": "created", "direction": "desc"})
    if r.status_code != 200:
        raise Transient(f"GitHub pulls HTTP {r.status_code}")
    marker = f"Trafficomm-Issue: {issue.ref}"
    since = (run.dispatched_at or run.created_at)
    for pr in r.json():
        created = dt.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
        if created < since - dt.timedelta(minutes=5):
            continue
        if marker in (pr.get("body") or "") or f"[{issue.ref}]" in (pr.get("title") or "") or pr["head"]["ref"].startswith(f"claude/{issue.ref.lower()}"):
            return pr
    return None


def _summarise(states: list[str]) -> str:
    """Combine per-check states (pending | success | failure) into one; nothing reported yet counts as pending."""
    if not states or "pending" in states:
        return "pending"
    return "failure" if "failure" in states else "success"


def _conclusion(status: str | None, conclusion: str | None) -> str:
    if status != "completed":
        return "pending"
    return "success" if conclusion in ("success", "neutral", "skipped") else "failure"


def checks_state(sha: str) -> str:
    """Test result for a commit: check runs, else workflow runs, else commit statuses.

    Fine-grained GitHub tokens have no "Checks" permission, so check-runs can return 403; the Actions runs
    API (Actions: read) and the combined commit status (Commit statuses: read) give the same answer.
    """
    repo = cfg()["repo"]
    r = _gh("GET", f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100})
    if r.status_code == 200:
        runs = r.json().get("check_runs", [])
        if runs:
            return _summarise([_conclusion(x.get("status"), x.get("conclusion")) for x in runs])
    r = _gh("GET", f"/repos/{repo}/actions/runs", params={"head_sha": sha, "per_page": 100})
    if r.status_code == 200:
        runs = r.json().get("workflow_runs", [])
        if runs:
            return _summarise([_conclusion(x.get("status"), x.get("conclusion")) for x in runs])
    r = _gh("GET", f"/repos/{repo}/commits/{sha}/status")
    if r.status_code == 200 and r.json().get("statuses"):
        st = r.json().get("state")
        return {"success": "success", "failure": "failure", "error": "failure"}.get(st, "pending")
    return "pending"


def pr_files(number: int) -> list[str]:
    c = cfg()
    r = _gh("GET", f"/repos/{c['repo']}/pulls/{number}/files", params={"per_page": 100})
    return [f["filename"] for f in r.json()] if r.status_code == 200 else []


def is_deployed(merge_sha: str) -> bool:
    """True when the commit this service runs contains the merge commit (Render sets RENDER_GIT_COMMIT)."""
    c = cfg()
    cur = c["deployed_commit"]
    if not cur or not merge_sha:
        return False
    if cur.startswith(merge_sha) or merge_sha.startswith(cur):
        return True
    r = _gh("GET", f"/repos/{c['repo']}/compare/{merge_sha}...{cur}")
    return r.status_code == 200 and r.json().get("status") in ("ahead", "identical")


def sync_all(s) -> int:
    if not cfg()["gh_token"]:
        return 0
    n = 0
    for run in s.scalars(select(EngineeringRun).where(EngineeringRun.status.in_(ACTIVE[1:]))).all():
        try:
            sync_run(s, run)
            n += 1
        except Transient as e:
            run.last_error = f"Status sync: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("sync failed for run %s", run.id)
            run.last_error = f"Status sync error: {type(e).__name__}"
    for run in s.scalars(select(EngineeringRun).where(EngineeringRun.status == "deployed", EngineeringRun.finished_at.is_(None))).all():
        _check_regeneration(s, run)
    return n


def sync_run(s, run: EngineeringRun):
    from .service import event, set_status
    issue = s.get(SupportIssue, run.issue_id)
    if run.status == "merged":
        if is_deployed(run.merge_sha):
            run.status = "deployed"
            event(s, issue.id, None, "deployed", f"commit {run.merge_sha[:12]} is live")
            s.add(SupportMessage(issue_id=issue.id, kind="engineering", text="The fix is live in production."))
            _regenerate(s, run, issue)
        return
    pr = find_pr(issue, run) if not run.pr_number else _gh("GET", f"/repos/{cfg()['repo']}/pulls/{run.pr_number}").json()
    if not pr or "number" not in pr:
        if run.status == "dispatched" and now() - (run.dispatched_at or run.created_at) > NO_PR_TIMEOUT:
            _fail(s, run, issue, "No pull request appeared within 4 hours of starting the investigation.")
        return
    if not run.pr_number:
        run.pr_number, run.pr_url, run.branch = pr["number"], pr["html_url"], pr["head"]["ref"]
        event(s, issue.id, None, "pr_opened", pr["html_url"])
    rep = parse_report(pr.get("body"))
    if rep and rep != {k: v for k, v in (run.agent_report or {}).items() if k in rep}:
        run.agent_report = {**(run.agent_report or {}), **rep}
        if rep.get("root_cause"):
            s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=f"Root cause found: {rep['root_cause']}"
                                 + (f"\nProposed fix: {rep.get('fix')}" if rep.get("fix") else "")))
        if rep.get("needs_info") and rep.get("questions_for_employee"):
            _ask_employee(s, issue, rep["questions_for_employee"])
    run.pr_state = "merged" if pr.get("merged_at") else pr["state"]
    if pr.get("merged_at"):
        run.status, run.merge_sha = "merged", pr.get("merge_commit_sha")
        set_status(s, issue, "awaiting_deploy")
        event(s, issue.id, None, "merged", f"PR #{run.pr_number} merged ({(run.merge_sha or '')[:12]})")
        return
    if pr["state"] == "closed":
        run.status, run.finished_at = "closed", now()
        set_status(s, issue, "unresolved", None, f"PR #{run.pr_number} was closed without merging")
        notify.for_issue(s, issue, "needs_input", f"{issue.ref}: the proposed fix was closed without merging", include_leads=True)
        return
    files = pr_files(run.pr_number)
    run.approval_level = approval_level(files) if files else (run.approval_level or "admin")
    st = checks_state(pr["head"]["sha"])
    prev = run.status
    run.checks_state = st
    if st == "failure":
        run.status = "checks_failed"
        set_status(s, issue, "fix_proposed")
    elif st == "success":
        if run.status not in ("awaiting_approval", "approved"):
            run.status = "awaiting_approval"
            set_status(s, issue, "awaiting_approval")
            who = "an administrator" if run.approval_level == "admin" else "a reporting lead or administrator"
            calc = " It changes production calculation logic." if touches_calculation(files) else ""
            s.add(SupportMessage(issue_id=issue.id, kind="engineering",
                                 text=f"Pull request #{run.pr_number} passed automated tests and needs approval from {who}.{calc}"))
            notify.for_issue(s, issue, "approval_required", f"{issue.ref}: fix #{run.pr_number} needs approval",
                             include_leads=run.approval_level != "admin", include_admins=run.approval_level == "admin")
    else:
        run.status = "pr_open"
        set_status(s, issue, "fix_proposed")
    if prev != run.status:
        event(s, issue.id, None, "run_status", f"{prev} → {run.status}")


def _ask_employee(s, issue: SupportIssue, questions: list):
    from .service import set_status
    qs = [clean(str(q), 300) for q in questions[:5] if str(q).strip()]
    if not qs:
        return
    meta = dict(issue.meta or {})
    meta["open_questions"] = qs
    issue.meta = meta
    set_status(s, issue, "awaiting_employee")
    s.add(SupportMessage(issue_id=issue.id, kind="engineering", meta={"questions": qs},
                         text="The engineering agent needs a few answers:\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(qs))))
    notify.for_issue(s, issue, "needs_input", f"{issue.ref}: the engineering agent has questions for you")


def _regenerate(s, run: EngineeringRun, issue: SupportIssue):
    """After a verified deploy, re-run the affected report so the employee can review the corrected output."""
    from .service import set_status
    t = s.get(Task, issue.task_id) if issue.task_id else None
    if not t or t.deleted_at is not None or not t.current_version_id:
        set_status(s, issue, "awaiting_validation")
        notify.for_issue(s, issue, "corrected_report", f"{issue.ref}: the fix is live; please re-run your report and confirm")
        run.finished_at = now()
        return
    try:
        v = regenerate_version(s, t, f"Regenerated after fix {issue.ref}")
    except Exception as e:  # noqa: BLE001
        log.warning("regeneration failed for %s: %s", issue.ref, e)
        v = None
    if v is None:
        set_status(s, issue, "awaiting_validation")
        notify.for_issue(s, issue, "corrected_report", f"{issue.ref}: the fix is live; please re-run the report and confirm")
        run.finished_at = now()
        return
    run.agent_report = {**(run.agent_report or {}), "regen_version_id": v.id}
    s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=f"Regenerating the report as v{v.number} with the fix."))


def regenerate_version(s, t: Task, note: str) -> TaskVersion | None:
    """A fresh run of the task's latest inputs (same files, answers and overrides) on the deployed code.
    No artifacts are reused, so every figure is recomputed by the fixed engine."""
    from .. import service as S
    from ..db import TaskFile
    cur = s.get(TaskVersion, t.current_version_id)
    if cur is None or cur.status in S.PROCESSING:
        return None
    live = set(s.scalars(select(TaskFile.id).where(TaskFile.task_id == t.id, TaskFile.removed.is_(False), TaskFile.role != "sheet_export")).all())
    num = (s.scalar(select(func.max(TaskVersion.number)).where(TaskVersion.task_id == t.id)) or 0) + 1
    v = TaskVersion(task_id=t.id, number=num, base_version_id=None, kind="initial", feedback=note, plan_overrides=dict(cur.plan_overrides or {}),
                    answers=dict(cur.answers or {}), file_ids=[f for f in (cur.file_ids or []) if f in live], created_by=t.owner_id)
    s.add(v)
    s.flush()
    t.current_version_id, t.status, t.approved_version_id = v.id, "queued", None
    from ..db import Message
    s.add(Message(task_id=t.id, version_id=v.id, author_id=None, kind="system", text=f"v{v.number}: {note}."))
    S.enqueue(s, v, "regenerate")
    return v


def _check_regeneration(s, run: EngineeringRun):
    from .service import set_status
    vid = (run.agent_report or {}).get("regen_version_id")
    issue = s.get(SupportIssue, run.issue_id)
    if not vid:
        return
    v = s.get(TaskVersion, vid)
    if not v or v.status in ("queued", "inspecting", "mapping", "calculating", "generating", "validating"):
        return
    run.finished_at = now()
    if v.status in ("ready_for_review", "awaiting_sheet", "completed"):
        set_status(s, issue, "awaiting_validation")
        s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=f"The corrected report (v{v.number}) is ready. Please review it and confirm here."))
        notify.for_issue(s, issue, "corrected_report", f"{issue.ref}: corrected report v{v.number} is ready to review")
    else:
        set_status(s, issue, "reopened", None, f"regenerated v{v.number} ended {v.status}")
        s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=f"The regenerated report v{v.number} ended with status {v.status}; the issue is reopened."))
        notify.for_issue(s, issue, "validation_failed", f"{issue.ref}: regenerated report v{v.number} failed validation", include_leads=True)


# ---------------------------------------------------------------------------
# approval, rollback, callback
# ---------------------------------------------------------------------------
def approve(s, run: EngineeringRun, user: User, note: str = "") -> str:
    from .service import event, set_status
    issue = s.get(SupportIssue, run.issue_id)
    ok, why = can_approve_run(s, user, run, issue)
    if not ok:
        raise EngineeringError(why)
    run.status, run.approved_by, run.approved_at = "approved", user.id, now()
    event(s, issue.id, user.id, "fix_approved", f"PR #{run.pr_number} ({run.approval_level} level) {note}")
    s.add(AuditLog(user_id=user.id, action="engineering_approve", target=f"{issue.ref} PR #{run.pr_number}"))
    msg = f"Approved by {user.name}."
    if cfg()["merge_on_approval"] and cfg()["gh_token"]:
        r = _gh("PUT", f"/repos/{cfg()['repo']}/pulls/{run.pr_number}/merge", json={"merge_method": "squash"})
        if r.status_code == 200:
            msg += " Merged; deployment will be confirmed when the new commit is live."
        else:
            msg += f" Automatic merge failed (HTTP {r.status_code}); merge it on GitHub."
    else:
        msg += " The pull request can now be merged on GitHub; deployment is confirmed automatically afterwards."
    set_status(s, issue, "awaiting_deploy", user.id)
    s.add(SupportMessage(issue_id=issue.id, author_id=user.id, kind="system", text=msg))
    return msg


def rollback(s, run: EngineeringRun, user: User) -> str:
    """Admin only: open a revert pull request for a merged fix (GitHub GraphQL revertPullRequest)."""
    from .service import event
    if user.role != "admin":
        raise EngineeringError("Only an administrator can roll back a fix.")
    if run.status not in ("merged", "deployed") or not run.pr_number:
        raise EngineeringError("Only a merged fix can be rolled back.")
    c = cfg()
    owner, name = c["repo"].split("/", 1)
    q = _gh("POST", "/graphql", json={"query": "query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){id}}}",
                                      "variables": {"o": owner, "n": name, "p": run.pr_number}})
    try:
        pr_id = q.json()["data"]["repository"]["pullRequest"]["id"]
        m = _gh("POST", "/graphql", json={"query": "mutation($id:ID!){revertPullRequest(input:{pullRequestId:$id,draft:true}){revertPullRequest{url number}}}",
                                          "variables": {"id": pr_id}})
        rev = m.json()["data"]["revertPullRequest"]["revertPullRequest"]
    except Exception as e:  # noqa: BLE001
        raise EngineeringError(f"GitHub didn't create the revert pull request ({type(e).__name__}). Use Render's rollback or revert on GitHub.") from e
    event(s, run.issue_id, user.id, "rollback_opened", rev["url"])
    s.add(AuditLog(user_id=user.id, action="engineering_rollback", target=f"run {run.id} -> {rev['url']}"))
    s.add(SupportMessage(issue_id=run.issue_id, author_id=user.id, kind="system", text=f"Rollback opened as a draft pull request: {rev['url']} (needs merge)."))
    return rev["url"]


def verify_signature(body: bytes, ts: str, sig: str) -> bool:
    secret = cfg()["callback_secret"]
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 600:
            return False
    except ValueError:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.removeprefix("sha256="))


CALLBACK_STATUSES = {"investigating", "needs_info", "pr_opened", "no_code_change", "failed"}


def callback(s, data: dict) -> dict:
    """Signed status report from the engineering agent. It can add findings and questions, never mark anything fixed."""
    from .service import event, set_status
    ref = str(data.get("issue") or "")
    m = re.fullmatch(r"AIS-(\d{1,8})", ref)
    if not m:
        raise EngineeringError("Unknown issue reference")
    issue = s.get(SupportIssue, int(m.group(1)))
    if not issue:
        raise EngineeringError("Unknown issue reference")
    run = active_run(s, issue.id)
    st = str(data.get("status") or "")
    if st not in CALLBACK_STATUSES:
        raise EngineeringError("Unsupported status")
    msg = clean(str(data.get("message") or ""), 3000)
    event(s, issue.id, None, "callback", f"{st}: {msg[:300]}")
    if run:
        run.agent_report = {**(run.agent_report or {}), "last_callback": st,
                            **({"root_cause": clean(str(data["root_cause"]), 2000)} if data.get("root_cause") else {})}
        if data.get("pr_url") and not run.pr_url and str(data["pr_url"]).startswith("https://github.com/"):
            run.pr_url = str(data["pr_url"])[:300]
    if msg:
        s.add(SupportMessage(issue_id=issue.id, kind="engineering", text=msg))
    if st == "needs_info":
        _ask_employee(s, issue, data.get("questions") or [])
    elif st == "no_code_change" and run:
        run.status, run.finished_at = "closed", now()
        set_status(s, issue, "investigating", None, "engineering found no code change is needed")
        notify.for_issue(s, issue, "needs_input", f"{issue.ref}: engineering found no code change is needed; see the explanation")
    elif st == "failed" and run:
        _fail(s, run, issue, msg or "The engineering agent reported a failure.")
    return {"ok": True}


def record_solution(s, issue: SupportIssue, user: User):
    """After the employee accepts the corrected report, keep the approved fix as Client Intelligence."""
    run = s.scalar(select(EngineeringRun).where(EngineeringRun.issue_id == issue.id, EngineeringRun.status == "deployed")
                   .order_by(EngineeringRun.id.desc()).limit(1))
    if not run or not issue.client_id:
        return None
    rep = run.agent_report or {}
    body = (f"Root cause: {rep.get('root_cause') or 'see pull request'}\nFix: {rep.get('fix') or rep.get('summary') or ''}\n"
            f"Validated dates: {', '.join(rep.get('validated_dates') or []) or 'see pull request'}\nPull request: {run.pr_url}")
    r = knowledge.propose(s, user, issue.client_id, f"{issue.ref}: {issue.title}"[:300], body, "correction", key=f"fix-{issue.ref.lower()}",
                          source="engineering", issue_id=issue.id, pr_url=run.pr_url)
    # the code change was approved through the pull request and validated by the employee on the regenerated report
    r.status, r.decided_by, r.decided_at, r.validated = "approved", run.approved_by, now(), True
    r.decision_note = "Approved via pull request review and accepted by the employee on the corrected report."
    return r
