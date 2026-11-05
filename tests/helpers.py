"""Run the processing pipeline directly on files (no web/DB) - used by engine-level acceptance tests."""
from __future__ import annotations

import datetime as dt
import os
import tempfile

from app.engine.inspect import inspect_file
from app.pipeline import VersionInput, run_version
from app.profiles import ATC_PROFILE, GENERIC_PROFILE

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic")
REAL = os.environ.get("TC_REAL_SAMPLES", "/home/claude/samples_real")


def fx(name):
    return os.path.join(FIX, name)


def run(files, deliverables=("excel",), answers=None, plan_overrides=None, profile=None, workdir=None, instructions="",
        period=("2026-09-01", "2026-09-30"), comparison=None, currency="AED", base_artifacts=None, version=1, stages=None):
    profile = GENERIC_PROFILE if profile is None else profile
    fin = []
    for i, (path, role) in enumerate(files):
        name = os.path.basename(path)
        insp = inspect_file(path, name, profile)
        fin.append({"id": i + 1, "name": name, "path": path, "ext": os.path.splitext(name)[1].lower(), "role": role,
                    "inspection": insp, "uploaded_order": i + 1, "excluded": False})
    d = lambda s: dt.date.fromisoformat(s) if s else None
    task = {"title": "Synthetic test", "client": "Test client", "brand": "Acme Dates", "period_start": d(period[0]), "period_end": d(period[1]),
            "comparison_start": d(comparison[0]) if comparison else None, "comparison_end": d(comparison[1]) if comparison else None,
            "timezone": "Asia/Dubai", "currency": currency, "deliverables": list(deliverables), "instructions": instructions}
    wd = workdir or tempfile.mkdtemp(prefix="tc_test_")
    inp = VersionInput(task=task, files=fin, profile=profile, answers=answers or {}, plan_overrides=plan_overrides or {},
                       workdir=wd, version_number=version, base_artifacts=base_artifacts or {})
    log = stages if stages is not None else []
    return run_version(inp, lambda n, dtl: log.append((n, dtl)))


def qkeys(res):
    return [q["key"] for q in res.get("questions", [])]


def check(res, cid):
    return next((c for c in res["validation"]["checks"] if c["id"] == cid), None)
