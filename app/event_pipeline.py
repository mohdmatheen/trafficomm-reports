"""Processing for daily app-event report tasks (deliverable "event_report").

Built-in rules only, unless the previous report has a layout no saved report type matches: then,
if the employee described the report, Claude writes a recipe once. The recipe must reproduce the
previous day's report before its output is offered, and it is saved for the client after the
employee's Google Sheet is verified, so the next run needs no Claude call.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from .engine import event_report as E


def _blocked(result: dict, err: E.ReportError | dict, checks: list, needs_instructions: bool = False) -> dict:
    e = err.as_dict() if isinstance(err, E.ReportError) else err
    checks.append({"id": "event_error", "title": e["title"], "status": "fail", "message": e["fix"], "details": e.get("detail") or []})
    result["status"] = "validation_blocked"
    result["error"] = e["title"] + ". " + e["fix"]
    result["event"]["error"] = e
    result["event"]["needs_instructions"] = needs_instructions
    result["validation"] = _validation(checks)
    return result


def _validation(checks: list) -> dict:
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "not_run")}
    overall = "blocked" if counts["fail"] else "warnings" if counts["warn"] else "passed"
    return {"overall": overall, "checks": checks, "counts": counts}


def run_event_report(inp, stage, check_cancel=lambda: None, provider=None) -> dict:
    t = inp.task
    out_dir = os.path.join(inp.workdir, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    result: dict = {"status": None, "kind": "event_report", "questions": [], "artifacts": [], "previews": [],
                    "ai_provider": getattr(provider, "name", "built-in rules"), "ai_mocked": True, "ai_steps": [], "event": {}}
    checks: list[dict] = []
    stage("inspecting", "Reading the AppsFlyer exports and the previous day's report")
    raw = [{"name": f["name"], "path": f["path"]} for f in inp.files if f["role"] == "raw" and not f.get("excluded")]
    prevs = [f for f in inp.files if f["role"] == "previous_report" and not f.get("excluded")]
    if not raw:
        return _blocked(result, E.ReportError("No raw exports were uploaded",
                                              "Add the AppsFlyer raw reports (In-app events and Non-organic in-app events, CSV, for iOS and Android)."), checks)
    if not prevs:
        return _blocked(result, E.ReportError("The previous day's report is missing",
                                              "Upload yesterday's report (e.g. 'FAB_CASA Always On Report_24_11_2026.xlsx') under 'Previous day report'. "
                                              "It sets the layout and is used to check today's figures."), checks)
    if len(prevs) > 1:
        return _blocked(result, E.ReportError("More than one previous day report was uploaded",
                                              "Keep only yesterday's report under 'Previous day report' and remove the others.",
                                              [f["name"] for f in prevs]), checks)
    prev_file = prevs[0]
    try:
        prev = E.read_previous(prev_file["path"])
    except E.ReportError as e:
        return _blocked(result, e, checks)
    prev_cols = E.previous_layout_formulas(prev_file["path"], prev)
    check_cancel()

    stage("mapping", "Matching the report type")
    recipes = E.recipes_for(t.get("client") or "", inp.profile)
    recipe = E.match_recipe(prev, recipes)
    source = None
    if recipe:
        source = "built-in" if recipe.get("builtin") else "saved"
        result["ai_steps"] = [{"step": "Report type", "engine": "rules" if recipe.get("builtin") else "saved",
                               "reason": f"Known report type '{recipe['name']}' - built-in rules, no Claude call"}]
    else:
        instructions = (t.get("instructions") or "").strip()
        unknown = E.describe_unknown(prev, recipes)
        if not instructions:
            return _blocked(result, E.ReportError(
                f"This is a new report type for {t.get('client') or 'this client'}",
                "Describe the report in the box below: which campaigns to keep, which events to count, and whether it covers iOS, Android or both. "
                "Claude reads it once, checks it against the previous day's report, and after your Google Sheet is verified it becomes a built-in rule.",
                unknown), checks, needs_instructions=True)
        if not hasattr(provider, "propose_event_recipe") or getattr(provider, "mocked", True):
            return _blocked(result, E.ReportError(
                "Claude isn't available to read this new report type",
                "Ask an admin to check the Anthropic key in Render (healthz shows ai=mocked). Known report types still run with built-in rules.",
                unknown), checks, needs_instructions=True)
        ctx = E.recipe_context(raw, prev, instructions, prev_cols)
        proposal = provider.propose_event_recipe(ctx)
        result["ai_mocked"] = False
        result["ai_steps"] = list(getattr(provider, "steps", []) or [])
        if not proposal:
            return _blocked(result, E.ReportError("Claude couldn't read this report type",
                                                  "Try again in a few minutes. If it keeps failing, add more detail to the description below.", unknown),
                            checks, needs_instructions=True)
        if not proposal.get("ok", True):
            return _blocked(result, E.ReportError("Claude needs more detail to build this report",
                                                  (proposal.get("question") or "Describe the campaigns and events to count.") +
                                                  " Add it to the description below and run again.", unknown), checks, needs_instructions=True)
        recipe = {k: v for k, v in proposal.items() if k not in ("ok", "question")}
        recipe["builtin"] = False
        recipe["sheet_title"] = recipe.get("sheet_title") or prev["sheet_title"]
        recipe.setdefault("title_cell", prev.get("title_cell"))
        recipe["file_prefix"] = re.sub(r"[_\s-]*\d{1,2}[_.-]\d{1,2}[_.-]\d{4}.*$", "", os.path.splitext(prev_file["name"])[0]) or recipe["sheet_title"]
        recipe["name"] = (recipe.get("name") or recipe["file_prefix"]).strip()[:120]
        recipe["key"] = "custom_" + E.recipe_signature(recipe)
        recipe["created"] = dt.date.today().isoformat()
        probs = E.validate_recipe(recipe)
        if not probs and [c["header"].strip().lower() for c in recipe["columns"]] != [h.strip().lower() for h in prev["headers"]]:
            probs.append("its columns don't match the previous report's columns (" + ", ".join(prev["headers"]) + ")")
        if probs:
            return _blocked(result, E.ReportError("Claude's reading of this report type isn't usable",
                                                  "Add more detail to the description below (exact campaign names and event names) and run again.", probs),
                            checks, needs_instructions=True)
        source = "claude"
    result["event"].update({"recipe_key": recipe["key"], "recipe_name": recipe["name"], "source": source,
                            "headers": [c["header"] for c in recipe["columns"]], "previous_file": prev_file["name"]})
    checks.append({"id": "event_type", "title": "Report type", "status": "pass",
                   "message": {"built-in": f"{recipe['name']} - built-in rules", "saved": f"{recipe['name']} - saved rule (no Claude call)",
                               "claude": f"New report type read by Claude: {recipe['name']}"}[source],
                   "details": [f"Campaigns: " + (", ".join(recipe["campaigns"]) if recipe.get("campaign_match") != "all" else "all") +
                               f" ({recipe.get('campaign_match')} match)",
                               "Exports: " + ", ".join(f"{E.PLATFORM_LABEL[p]} {E.SOURCE_LABEL[s]}" for p in recipe["platforms"] for s in recipe["sources"]),
                               "Events: " + ", ".join(f"{c['header']}" + (f" (= {c['event']})" if c.get("event") and c["event"] != c["header"] else "")
                                                      for c in recipe["columns"])]})
    check_cancel()

    stage("calculating", "Filtering campaigns, finding device IDs and removing duplicates")
    try:
        run = E.build_counts(recipe, raw)
    except E.ReportError as e:
        return _blocked(result, e, checks, needs_instructions=source == "claude")
    steps = run["steps"]
    checks.append({"id": "event_dedup", "title": "Duplicates removed (same device and event)", "status": "pass",
                   "message": f"{steps[-1]['after_dedup']:,} unique events from {sum(s.get('rows', 0) for s in steps if s.get('file')):,} raw rows",
                   "details": [(f"{s['step']}: {s['rows']:,} rows, {s['in_campaigns']:,} in the campaigns, {s['after_dedup']:,} after de-duplication"
                                if s.get("file") else f"{s['step']}: {s['rows']:,} merged, {s['after_dedup']:,} after de-duplication") for s in steps]})
    if run["warnings"]:
        checks.append({"id": "event_inputs", "title": "Raw export notes", "status": "warn", "message": run["warnings"][0], "details": run["warnings"][1:]})
    cmp = E.compare_previous(prev, run, recipe)
    if source == "claude":
        total_cells = (cmp.get("compared_days") or 0) * len([c for c in recipe["columns"] if c.get("event")])
        bad = len(cmp.get("issues") or [])
        if cmp["status"] == "fail" or (total_cells and bad / total_cells > 0.05) or (total_cells == 0 and not cmp.get("new_month")):
            return _blocked(result, E.ReportError(
                "Claude's reading doesn't reproduce the previous day's report",
                "The figures for days already reported should stay the same. Check the description below (campaign names, events, iOS/Android) and run again.",
                cmp.get("issues") or [cmp["text"]]), checks, needs_instructions=True)
    checks.append({"id": "event_previous", "title": "Compared with the previous day's report", "status": cmp["status"], "message": cmp["text"],
                   "details": cmp.get("issues") or []})
    if cmp["status"] == "fail":
        return _blocked(result, E.ReportError(cmp["text"], (cmp.get("issues") or [""])[0]), checks)
    if prev.get("last_day") == run["end_day"]:
        same = all((prev["blocks"].get(c["event"]) or {}).get("days", {}).get(run["end_day"], 0) == run["counts"].get(run["end_day"], {}).get(c["event"], 0)
                   for c in recipe["columns"] if c.get("event"))
        if same:
            checks.append({"id": "event_fresh", "title": "New data", "status": "warn",
                           "message": f"The exports end on {run['end_day']:%d %b}, the same day as the previous report, with the same figures. "
                                      "Check that you downloaded today's exports.", "details": []})
    check_cancel()

    stage("generating", "Writing the updated report")
    fname = E.output_name(recipe, run["end_day"], prev_file["name"])
    path = os.path.join(out_dir, fname)
    E.write_workbook(recipe, run, prev, path)
    rows = E.sheet_rows(recipe, run)
    result["artifacts"].append({"kind": "excel", "label": f"{recipe['name']} - {run['end_day']:%d %b %Y}", "path": path})

    stage("validating", "Reading the report back")
    back = E.read_report_back(path, recipe)
    diffs = []
    for d, c in run["counts"].items():
        for ev, n in c.items():
            if back.get(ev, {}).get(d.isoformat()) != n:
                diffs.append(f"{d:%d %b} {ev}: written {back.get(ev, {}).get(d.isoformat())}, calculated {n}")
    checks.append({"id": "event_readback", "title": "Report read-back", "status": "fail" if diffs else "pass",
                   "message": "Every day and event in the file equals the calculated count" if not diffs else f"{len(diffs)} figure(s) differ in the written file",
                   "details": diffs[:30]})
    checks.append({"id": "event_sheet", "title": "Google Sheet", "status": "not_run",
                   "message": "Waiting for the updated Google Sheet export. The task completes when it matches this report.", "details": []})
    result["validation"] = _validation(checks)
    totals = {c["header"]: sum(r["values"][i] for r in rows) for i, c in enumerate(recipe["columns"])}
    result["event"].update({"rows": rows, "totals": totals, "end_day": run["end_day"].isoformat(), "month": run["month_start"].isoformat(),
                            "file": fname, "unique_events": steps[-1]["after_dedup"], "steps": steps, "compare": cmp,
                            "campaign_label": recipe.get("sheet_campaign_label")})
    if source == "claude":
        result["event"]["new_recipe"] = recipe
    if diffs:
        result["status"] = "validation_blocked"
        result["error"] = "The written report doesn't match the calculated counts."
    else:
        result["status"] = "awaiting_sheet"
    return result
