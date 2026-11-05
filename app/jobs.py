"""Durable Postgres-backed job queue + worker loop.

* jobs live in the `jobs` table (survive restarts); claiming uses SELECT ... FOR UPDATE SKIP LOCKED;
* each job runs in a separate subprocess with memory/CPU limits and a wall-clock timeout;
* heartbeats let a restarted worker recover jobs orphaned by a crash;
* transient failures are retried with exponential backoff; permanent ones fail with a clear message;
* cancellation is cooperative (checked between stages) and enforced by killing the subprocess.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import resource
import signal
import socket
import subprocess
import sys
import time

from sqlalchemy import select

from .config import BASE_DIR, settings
from .db import Job, Message, Session, StageEvent, Task, TaskVersion, now

log = logging.getLogger("trafficomm.worker")
STALE_SECONDS = 90
HEARTBEAT = 5
EXIT_OK, EXIT_FAIL, EXIT_PERMANENT, EXIT_CANCELLED = 0, 1, 2, 3


def claim(worker_id: str) -> Job | None:
    with Session() as s:
        q = select(Job).where(Job.status == "queued", Job.run_after <= now()).order_by(Job.id).limit(1)
        if not settings.database_url.startswith("sqlite"):
            q = q.with_for_update(skip_locked=True)
        job = s.scalar(q)
        if not job:
            return None
        job.status, job.locked_by, job.locked_at, job.heartbeat_at = "running", worker_id, now(), now()
        job.attempts += 1
        s.commit()
        s.refresh(job)
        s.expunge(job)
        return job


def recover_stale() -> int:
    """Jobs whose worker died (no heartbeat) go back to the queue (or fail if out of attempts)."""
    cutoff = now() - dt.timedelta(seconds=STALE_SECONDS)
    n = 0
    with Session() as s:
        for j in s.scalars(select(Job).where(Job.status == "running", Job.heartbeat_at < cutoff)).all():
            n += 1
            if j.attempts < j.max_attempts:
                j.status, j.run_after, j.error = "queued", now(), "Recovered after worker interruption"
                s.add(StageEvent(version_id=j.version_id, stage="uploaded", detail="Worker interrupted - job re-queued automatically"))
            else:
                _fail(s, j, "Processing was interrupted repeatedly and has been stopped. Please resubmit or contact an admin.")
        s.commit()
    return n


def _fail(s, j: Job, msg: str):
    j.status, j.finished_at, j.error = "failed", now(), msg
    v = s.get(TaskVersion, j.version_id)
    t = s.get(Task, j.task_id)
    if v:
        v.status, v.error = "failed", msg
        s.add(StageEvent(version_id=v.id, stage="failed", detail=msg[:300]))
    if t:
        t.status = "failed"
    s.add(Message(task_id=j.task_id, version_id=j.version_id, author_id=None, kind="assistant", text=f"Processing failed: {msg}"))


def _limits():
    mem = settings.job_memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except (ValueError, OSError):
        pass
    cpu = settings.job_timeout + 60
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    os.setsid()


def run_one(job: Job, worker_id: str):
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(BASE_DIR)
    proc = subprocess.Popen([sys.executable, "-m", "app.run_job", str(job.id)], cwd=str(BASE_DIR), env=env,
                            preexec_fn=_limits, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = time.time()
    last_hb = 0.0
    outcome = None
    while True:
        rc = proc.poll()
        if rc is not None:
            outcome = rc
            break
        if time.time() - last_hb > HEARTBEAT:
            last_hb = time.time()
            with Session() as s:
                j = s.get(Job, job.id)
                j.heartbeat_at = now()
                cancel = j.cancel_requested
                s.commit()
            if cancel:
                _kill(proc)
                outcome = EXIT_CANCELLED
                break
        if time.time() - started > settings.job_timeout:
            _kill(proc)
            outcome = "timeout"
            break
        time.sleep(0.5)
    out = proc.stdout.read().decode(errors="ignore")[-4000:] if proc.stdout else ""
    with Session() as s:
        j = s.get(Job, job.id)
        if outcome == EXIT_OK:
            j.status, j.finished_at, j.error = "succeeded", now(), None
        elif outcome == EXIT_CANCELLED:
            j.status, j.finished_at = "cancelled", now()
            v = s.get(TaskVersion, j.version_id)
            v.status = "cancelled"
            s.get(Task, j.task_id).status = "cancelled"
            s.add(StageEvent(version_id=v.id, stage="cancelled", detail="Cancelled by employee"))
            s.add(Message(task_id=j.task_id, version_id=v.id, author_id=None, kind="assistant", text=f"v{v.number} processing was cancelled. Earlier versions are unaffected."))
        elif outcome == EXIT_PERMANENT:
            _fail(s, j, _last_line(out) or "The files could not be processed.")
        else:
            reason = "timed out" if outcome == "timeout" else "hit an unexpected error"
            if j.attempts < j.max_attempts:
                delay = 10 * (2 ** (j.attempts - 1))
                j.status, j.run_after, j.error = "queued", now() + dt.timedelta(seconds=delay), f"Attempt {j.attempts} {reason}"
                s.add(StageEvent(version_id=j.version_id, stage="uploaded", detail=f"Attempt {j.attempts} {reason}; retrying in {delay}s"))
            else:
                _fail(s, j, f"Processing {reason} after {j.attempts} attempts. " + (_last_line(out) or ""))
        s.commit()
    log.info("job %s finished outcome=%s", job.id, outcome)


def _last_line(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if line.startswith("USER_ERROR:"):
            return line[len("USER_ERROR:"):].strip()
    return ""


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait(timeout=10)


def worker_loop(once: bool = False, idle_exit: float | None = None):
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker %s started", worker_id)
    last_recover = 0.0
    idle_since = time.time()
    last_retention = 0.0
    last_usage_sync = time.time() - 50  # first provider sync shortly after start
    while True:
        if time.time() - last_recover > 15:
            recover_stale()
            last_recover = time.time()
        if time.time() - last_retention > 86400:
            from .service import run_retention
            with Session() as s:
                run_retention(s)
                s.commit()
            last_retention = time.time()
        if time.time() - last_usage_sync > 60:
            try:
                from .usage import maybe_scheduled_sync
                maybe_scheduled_sync()
            except Exception:  # noqa: BLE001 - cost reporting must never stop report processing
                log.exception("usage sync failed")
            last_usage_sync = time.time()
        job = claim(worker_id)
        if job:
            run_one(job, worker_id)
            idle_since = time.time()
            if once:
                return
            continue
        if once or (idle_exit and time.time() - idle_since > idle_exit):
            return
        time.sleep(1.0)
