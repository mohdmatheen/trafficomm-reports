"""Processing pipeline for one task version. Independent of the web/DB layer: it receives plain
inputs, reports stage transitions via a callback and returns a JSON-serialisable result."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import logging
import shutil
from dataclasses import dataclass, field

from .engine import ai as AI
from .engine import fmt as FM
from .engine import rules as R
from .engine.calc import calculate
from .engine.excel_out import build_excel
from .engine.inspect import inspect_file
from .engine.normalize import Question, build_dataset
from .engine.pptx_new import build_new_deck
from .engine.pptx_update import DeckUpdater
from .engine.render import RenderError, layout_checks, pdf_to_pngs, to_pdf
from .engine.template_fill import fill_template
from .engine.validate import run_validation

log = logging.getLogger("trafficomm.pipeline")


class Cancelled(Exception):
    pass


@dataclass
class VersionInput:
    task: dict                  # title, client, brand, period_start/end, comparison_*, timezone, currency, deliverables, instructions
    files: list[dict]           # {id, name, path, ext, role, inspection, uploaded_order}
    profile: dict
    answers: dict
    plan_overrides: dict        # accumulated revision ops / employee choices
    workdir: str
    version_number: int
    base_artifacts: dict = field(default_factory=dict)  # kind -> {path, fingerprint, id}
    feedback: str | None = None


def _fp(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:60]


def run_version(inp: VersionInput, stage, check_cancel=lambda: None, provider=None) -> dict:
    provider = provider or AI.get_provider()
    t = inp.task
    os.makedirs(inp.workdir, exist_ok=True)
    out_dir = os.path.join(inp.workdir, "outputs")
    prev_dir = os.path.join(inp.workdir, "previews")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(prev_dir, exist_ok=True)
    result: dict = {"status": None, "questions": [], "artifacts": [], "ai_provider": provider.name, "ai_mocked": provider.mocked,
                    "ai_steps": getattr(provider, "steps", None) if not provider.mocked else []}

    # ---------------- Inspecting files
    stage("inspecting", "Inspecting uploaded files")
    for f in inp.files:
        if not f.get("inspection"):
            f["inspection"] = inspect_file(f["path"], f["name"], inp.profile)
    raw = [f for f in inp.files if f["role"] == "raw" and not f.get("excluded")]
    deck_files = [f for f in inp.files if f["role"] == "client_deck"]
    deck_file = deck_files[0] if deck_files else None
    template_file = next((f for f in inp.files if f["role"] == "template"), None)
    shots = [f for f in inp.files if f["role"] in ("screenshots", "creatives") and not f.get("excluded") and f["inspection"].get("ok", True)]
    rejected = [f for f in inp.files if not f["inspection"].get("ok", True)]
    result["inspection"] = [{"file": f["name"], "role": f["role"], "ok": f["inspection"].get("ok", True), "error": f["inspection"].get("error"),
                             "sheets": [{"name": s["name"], "platform": s.get("platform"), "rows": s.get("row_count"), "header_row": s.get("header_row"),
                                         "currency": (s.get("currency") or {}).get("value"), "dates": s.get("dates"), "summary_blocks": s.get("summary_blocks"),
                                         "total_rows": len(s.get("total_rows", [])), "unresolved": s.get("unresolved"), "result_indicators": s.get("result_indicators"),
                                         "is_data_sheet": s.get("is_data_sheet")} for s in f["inspection"].get("sheets", [])],
                             "deck": ({k: f["inspection"]["deck"][k] for k in ("slide_count", "summary", "aspect", "fonts", "unsupported_total")} if f["inspection"].get("deck") else None)}
                            for f in inp.files]
    check_cancel()

    # ---------------- Interpret instructions (model may interpret, never calculate)
    headers = sorted({c["original"] for f in raw for s in f["inspection"].get("sheets", []) for c in s.get("columns", [])})
    # hybrid routing: repeat reports on a known structure use built-in rules; new ones get Claude
    rule_plan = AI.RuleProvider().interpret_instructions(t.get("instructions") or "", t.get("deliverables") or [], headers).model_dump()
    fp, fp_desc = AI.structure_fingerprint(raw, deck_files, template_file, t.get("deliverables"), t.get("instructions") or "", rule_plan)
    known = list(inp.profile.get("known_structures") or [])
    if fp in known:
        mode = (False, "Repeat report on a known structure (data update) - built-in rules")
    elif not known:
        mode = (True, "First report for this client - Claude reads the request and writes the insights")
    else:
        mode = (True, "New report structure for this client (" + "; ".join(fp_desc) + ") - Claude reads the request and writes the insights")
    result["structure"] = {"fingerprint": fp, "known": fp in known, "reason": mode[1]}
    if hasattr(provider, "set_mode"):
        provider.set_mode(*mode)
    plan_obj = provider.interpret_instructions(t.get("instructions") or "", t.get("deliverables") or [], headers)
    plan = plan_obj.model_dump()
    for k, v in (inp.plan_overrides or {}).items():
        if k == "breakdowns":
            plan["breakdowns"] = plan.get("breakdowns", []) + [b for b in v if b not in plan.get("breakdowns", [])]
        elif k == "remove_sections":
            plan["remove_sections"] = sorted(set(plan.get("remove_sections", []) + v))
        else:
            plan[k] = v
    if plan.get("conversion_metric") is None and inp.answers.get("conversion_metric"):
        plan["conversion_metric"] = inp.answers["conversion_metric"]
    deliverables = set(t.get("deliverables") or [])
    plan["needs_cpa"] = True
    result["plan"] = plan

    # ---------------- Mapping
    stage("mapping", "Mapping columns and checking definitions")
    questions: list[Question] = []
    if "deck_update" in deliverables and not deck_file:
        if inp.answers.get("deck_missing") == "new":
            deliverables.discard("deck_update")
            deliverables.add("deck_new")
        else:
            questions.append(Question(key="deck_missing", text="You asked for an updated client PowerPoint but no PPTX was uploaded. Upload the client deck (then answer 'I've uploaded it'), or switch to a new presentation.",
                                      options=[{"value": "uploaded", "label": "I've uploaded the client deck"}, {"value": "new", "label": "Create a new presentation instead"}]))
    if not raw:
        questions.append(Question(key="no_raw", text="No raw reporting files are attached (or all are excluded). Upload at least one CSV/XLSX export.", options=[{"value": "uploaded", "label": "I've uploaded files"}]))
    norm = None
    if raw:
        norm = build_dataset(raw, t, inp.profile, inp.answers, plan)
        questions += norm.questions
    result["mapping"] = norm.mapping_summary if norm else []
    result["exclusions"] = (norm.exclusions if norm else []) + ([{"file": f["name"], "scope": "file", "reason": f["inspection"].get("error")} for f in rejected])
    if norm is not None and len(norm.df):
        result["exclusions"] += [{"file": r["_file"], "sheet": r["_sheet"], "scope": f"row {r['_row']}", "reason": r["_excluded"]} for _, r in norm.df[norm.df["_excluded"].notna()].iterrows()]
    result["assumptions"] = norm.assumptions if norm else []
    result["warnings"] = norm.warnings if norm else []
    result["overlaps"] = norm.overlaps if norm else []
    result["derivations"] = norm.derivations if norm else {}
    result["definition_fallback"] = getattr(norm, "definition_fallback", None) if norm else None
    blocking = [q for q in questions if q.blocking]
    if blocking:
        stage("awaiting_clarification", f"{len(blocking)} question(s) need an answer")
        result["status"] = "awaiting_clarification"
        result["questions"] = [q.as_dict() for q in questions]
        return result
    check_cancel()

    # ---------------- Calculating
    stage("calculating", "Calculating figures (deterministic engine)")
    currency = (t.get("currency") or (norm.currencies[0] if norm.currencies else None))
    calc = calculate(norm.df, plan, norm.dynamic_fields, currency)
    if calc.included_rows == 0:
        stage("blocked", "No rows inside the reporting period")
        result["status"] = "blocked"
        result["error"] = "No rows fall inside the reporting period after exclusions."
        return result
    rec_dir = os.path.join(inp.workdir, "records")
    os.makedirs(rec_dir, exist_ok=True)
    ds_path = os.path.join(rec_dir, "normalised_dataset.csv")
    keep = [c for c in norm.df.columns if c not in ("_key",)]
    norm.df[keep].to_csv(ds_path, index=False)
    ds, de = norm.period.get("data_start"), norm.period.get("data_end")
    style = (inp.profile.get("deck") or {}).get("range_style", "plain")
    period_label = FM.month_label(t.get("period_start") or ds, t.get("period_end") or de, long=True)
    ctx = {"title": t.get("title") or "Performance report", "client": t.get("client") or "", "brand": t.get("brand") or None,
           "period_label": period_label, "data_window": FM.range_label(ds, de, style), "currency": currency,
           "commentary_enabled": plan.get("include_commentary", True) and not plan.get("template_only"),
           "period_start": t.get("period_start"), "period_end": t.get("period_end"), "data_start": ds, "data_end": de}
    result["summary"] = {"totals": {k: v for k, v in calc.totals.items() if not k.startswith("_") and v is not None and not isinstance(v, list)},
                         "currency": currency, "period_label": period_label, "data_window": ctx["data_window"], "included_rows": calc.included_rows,
                         "click_metric": calc.click_metric_label, "conversion": calc.conversion_label, "definitions": calc.definitions,
                         "tables": {k: {"title": tb.title, "dims": tb.dims, "rows": [{kk: vv for kk, vv in r.items() if kk in tb.dims or kk in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa", "reach", "views", "video_plays_3s", "vtr", "_rows")} for r in tb.rows[:50]]} for k, tb in calc.tables.items()},
                         "comparison": calc.comparison and {"deltas": calc.comparison["deltas"], "previous": {k: v for k, v in calc.comparison["previous"].items() if not str(k).startswith("_") and not isinstance(v, list)}}}

    # deck slide matching may raise its own clarification questions before generating
    # one updated deck per uploaded client PowerPoint; the first keeps the original kind/keys
    updaters: list[tuple[str, dict, DeckUpdater]] = []
    if "deck_update" in deliverables and deck_files:
        multi = len(deck_files) > 1
        for i, df in enumerate(deck_files):
            kind = "deck_update" if i == 0 else f"deck_update_{i + 1}"
            up = DeckUpdater(df["path"], calc, norm.period, inp.profile, inp.answers,
                             {"screenshots": [{"path": f["path"], "name": f["name"]} for f in shots],
                              "add_missing_slides": plan.get("add_missing_slides", False),
                              "question_prefix": "" if i == 0 else f"deck{i + 1}:",
                              "deck_name": df["name"] if multi else None})
            up.match_slides()
            updaters.append((kind, df, up))
        deck_qs = [q for _, _, up in updaters for q in up.questions]
        if any(q["blocking"] for q in deck_qs):
            stage("awaiting_clarification", "Deck slides need confirmation")
            result["status"] = "awaiting_clarification"
            result["questions"] = deck_qs
            return result
    check_cancel()

    # ---------------- Commentary (interpretation only; numbers checked)
    commentary, dropped = [], []
    if ctx["commentary_enabled"]:
        labels = []
        for tb in calc.tables.values():
            for r in tb.rows:
                labels += [str(r[d]) for d in tb.dims]
        raw_comm = provider.write_commentary(calc.facts, ctx, calc)
        commentary, dropped = AI.validate_commentary(raw_comm, calc.facts, labels + [ctx["data_window"], period_label])
        if not plan.get("include_recommendations", True) or "recommendations" in plan.get("remove_sections", []):
            commentary = [c for c in commentary if c["type"] != "recommendation"]
    result["commentary"] = commentary
    result["commentary_dropped"] = dropped

    # ---------------- Generating
    stage("generating", "Generating deliverables")
    naming = inp.profile.get("file_naming") or "{client}_{title}_{period}_v{version}"
    base = _safe(naming.format(client=t.get("client") or "Report", brand=t.get("brand") or t.get("client") or "Report", title=t.get("title") or "Report",
                               period=period_label.replace(" ", ""), version=inp.version_number))
    calc_fp = _fp([f["inspection"]["sha256"] for f in raw], str(t.get("period_start")), str(t.get("period_end")), str(t.get("comparison_start")), currency, R.usd_aed_rate(t), "rules-2026-11",
                  calc.settings, sorted(inp.answers.items()), inp.profile.get("column_overrides"), inp.profile.get("naming"))
    artifacts = []
    excel_info = template_info = upd_info = new_info = None

    def reuse(kind, fp, dest):
        """Outputs are deterministic: when every input that affects a deliverable is unchanged from
        the base version, the deliverable is marked as unchanged (same content) rather than new."""
        b = inp.base_artifacts.get(kind)
        if b and b.get("fingerprint") == fp:
            return b.get("id")
        return None

    if "excel" in deliverables:
        if template_file:
            path = os.path.join(out_dir, f"{base}.xlsx")
            fp = _fp(calc_fp, "template", template_file["inspection"]["sha256"])
            try:
                template_info = fill_template(template_file["path"], path, calc, ctx, os.path.join(inp.workdir, "tmp"))
            except Exception as e:  # noqa: BLE001 - an unusual template must not cost the employee the whole report
                log.exception("template fill failed")
                template_info = {"error": f"{type(e).__name__}: {e}", "written": [], "unsupported": [], "checks": [],
                                 "skipped_formula_cells": 0, "notes": [], "labels": []}
                result.setdefault("warnings", []).append(
                    f"The Excel template '{template_file['name']}' could not be filled ({type(e).__name__}: {e}). "
                    "The consolidated Excel report was still produced; please fill the template manually or send it to an admin.")
                if os.path.exists(path):
                    os.remove(path)
            else:
                artifacts.append({"kind": "excel", "path": path, "label": "Populated Excel template", "fingerprint": fp, "reused_from": None})
                result.setdefault("warnings", []).extend(template_info.get("notes", []))
        if not template_file or not plan.get("template_only"):
            path = os.path.join(out_dir, f"{base}{'_consolidated' if template_file else ''}.xlsx")
            fp = _fp(calc_fp, "excel", plan.get("split_tabs_by"), plan.get("breakdowns"), plan.get("include_commentary"), plan.get("short_summary"), commentary)
            reused = reuse("excel_consolidated", fp, path)
            written = build_excel(path, calc, norm, ctx, commentary, plan, inp.profile)
            excel_info = {"path": path, "written": written}
            artifacts.append({"kind": "excel_consolidated", "path": path, "label": "Consolidated Excel report", "fingerprint": fp, "reused_from": reused})
    check_cancel()
    extra_decks: list[dict] = []
    used_names: set[str] = set()
    for i, (kind, df, up) in enumerate(updaters):
        if len(updaters) == 1:
            path = os.path.join(out_dir, f"{base}_client_deck.pptx")
            label = "Updated client PowerPoint"
        else:
            stem = _safe(os.path.splitext(df["name"])[0]) or f"deck{i + 1}"
            if stem in used_names:
                stem = f"{stem}_{i + 1}"
            used_names.add(stem)
            path = os.path.join(out_dir, f"{base}_{stem}_updated.pptx")
            label = f"Updated client PowerPoint ({df['name']})"
        res = up.run(path)
        for sl in sorted({int(x) for x in plan.get("remove_slides", [])}, reverse=True):
            _delete_slide(path, sl, res)
        info = {**res, "path": path, "na": up.na, "kind": kind, "label": label, "name": df["name"]}
        if i == 0:
            upd_info = info
        else:
            extra_decks.append(info)
        fpd = _fp(calc_fp, "deck", plan.get("remove_slides"), df["inspection"]["sha256"], sorted(f["inspection"]["sha256"] for f in shots))
        artifacts.append({"kind": kind, "path": path, "label": label, "fingerprint": fpd, "reused_from": reuse(kind, fpd, path)})
    if "deck_new" in deliverables:
        path = os.path.join(out_dir, f"{base}_new_deck.pptx")
        new_info = build_new_deck(path, calc, ctx, commentary, plan)
        new_info["path"] = path
        fpn = _fp(calc_fp, "newdeck", plan.get("remove_sections"), plan.get("short_summary"), commentary)
        artifacts.append({"kind": "deck_new", "path": path, "label": "New PowerPoint", "fingerprint": fpn, "reused_from": reuse("deck_new", fpn, path)})
    check_cancel()

    # ---------------- Validating (render + checks)
    stage("validating", "Validating outputs and rendering previews")
    render: dict = {}
    previews: list[dict] = []
    layout: list[dict] = []
    for a in artifacts:
        try:
            pdf = to_pdf(a["path"], os.path.join(inp.workdir, "pdf"))
            pngs = pdf_to_pngs(pdf, prev_dir, prefix=a["kind"], dpi=55 if a["kind"].startswith("deck") else 45, last=40 if a["kind"].startswith("deck") else 4)
            a["pdf"] = pdf
            previews += [{"artifact_kind": a["kind"], "path": p, "page": i + 1} for i, p in enumerate(pngs)]
            render[a["label"]] = {"ok": bool(pngs), "message": f"Opened with LibreOffice and rendered {len(pngs)} page(s)", "details": []}
        except RenderError as e:
            render[a["label"]] = {"ok": False, "message": str(e)}
        if a["kind"].startswith("deck"):
            dinfo = next((d for d in [upd_info, *extra_decks] if d and d["kind"] == a["kind"]), None)
            if dinfo:
                from pptx import Presentation as _P
                hdr_idx = {}
                changed = {}
                prs_ = _P(a["path"])
                for c in dinfo["changes"]:
                    if c.get("row") is None or c.get("unchanged"):
                        continue
                    key = (c["slide"], c["shape"])
                    if key not in hdr_idx:
                        sh = next((x for x in prs_.slides[c["slide"] - 1].shapes if x.name == c["shape"] and getattr(x, "has_table", False) and x.has_table), None)
                        hdr_idx[key] = [cc.text.replace("\u200b", "").strip() for cc in sh.table.rows[0].cells] if sh else []
                    if c["col"] in hdr_idx[key]:
                        changed[(c["slide"], c["shape"], c["row"], hdr_idx[key].index(c["col"]))] = c["old"]
                layout += [dict(x, message=f"{dinfo['name']}: {x['message']}") if extra_decks else x for x in layout_checks(a["path"], None, changed)]
            else:
                layout += layout_checks(a["path"])
    if "pdf" in deliverables:
        src = next((a for a in artifacts if a["kind"] in ("deck_update", "deck_new") and a.get("pdf")), None) or next((a for a in artifacts if a.get("pdf")), None)
        if src:
            pdfp = os.path.join(out_dir, f"{base}.pdf")
            shutil.copy(src["pdf"], pdfp)
            artifacts.append({"kind": "pdf", "path": pdfp, "label": f"PDF export of {src['label']}", "fingerprint": src["fingerprint"], "reused_from": src.get("reused_from")})
    validation = run_validation(files=raw, norm=norm, calc=calc, excel=excel_info, deck_update=upd_info, extra_decks=extra_decks, deck_new=new_info, template=template_info,
                                commentary_dropped=dropped, render=render, layout_issues=layout, ctx=ctx)
    result["validation"] = validation
    result["deck"] = upd_info and {"slide_map": upd_info["slide_map"], "flags": upd_info["flags"],
                                   "changes": [c for c in upd_info["changes"] if not c.get("unchanged")][:400], "range_text": upd_info.get("range_text"),
                                   "unchanged_cells": len([c for c in upd_info["changes"] if c.get("unchanged")])}
    result["extra_decks"] = [{"kind": d["kind"], "name": d["name"], "label": d["label"], "slide_map": d["slide_map"], "flags": d["flags"],
                              "changes": [c for c in d["changes"] if not c.get("unchanged")][:400], "range_text": d.get("range_text"),
                              "unchanged_cells": len([c for c in d["changes"] if c.get("unchanged")])} for d in extra_decks]
    if extra_decks and result["deck"]:
        result["deck"]["name"] = upd_info["name"]
    result["template"] = template_info and {k: template_info.get(k) for k in ("written", "unsupported", "checks", "labels", "skipped_formula_cells", "notes", "error")}
    result["new_deck"] = new_info and {"sections": new_info["sections"], "slides": new_info["slides"]}
    rec_json = os.path.join(rec_dir, "mapping_calculation_validation.json")
    with open(rec_json, "w") as fh:
        json.dump({"plan": plan, "answers": inp.answers, "mapping": result["mapping"], "exclusions": result["exclusions"],
                   "assumptions": result["assumptions"], "summary": result["summary"], "validation": validation,
                   "commentary": commentary, "deck": result["deck"], "extra_decks": result["extra_decks"]}, fh, default=str, indent=1)
    artifacts.append({"kind": "record", "path": ds_path, "label": "Normalised dataset (CSV, with provenance and exclusions)", "fingerprint": calc_fp, "reused_from": None})
    artifacts.append({"kind": "record", "path": rec_json, "label": "Mappings, calculations and validation record (JSON)", "fingerprint": calc_fp, "reused_from": None})
    result["artifacts"] = [{k: v for k, v in a.items() if k != "pdf"} for a in artifacts]
    result["previews"] = previews
    result["key_figures"] = _key_figures(calc)
    result["status"] = "ready_for_review" if validation["overall"] != "blocked" else "validation_blocked"
    stage(result["status"], {"passed": "All checks passed", "warnings": "Ready with warnings to review", "blocked": "Validation blocked - see failed checks"}[validation["overall"]])
    return result


def _key_figures(calc) -> dict:
    out = {"total": {k: calc.totals.get(k) for k in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa", "views", "vtr")}}
    for k in ("platform", "brand", "creative", "country", "language"):
        tb = calc.tables.get(k)
        if tb:
            out[k] = {" / ".join(str(r[d]) for d in tb.dims): {m: r.get(m) for m in ("spend", "impressions", "clicks", "ctr")} for r in tb.rows}
    return out


def _delete_slide(path: str, slide_no: int, res: dict):
    from pptx import Presentation
    prs = Presentation(path)
    if slide_no < 1 or slide_no > len(prs.slides):
        res["flags"].append({"slide": slide_no, "severity": "warning", "message": f"Cannot remove slide {slide_no}: deck has {len(prs.slides)} slides"})
        return
    sldIdLst = prs.slides._sldIdLst
    sld = list(sldIdLst)[slide_no - 1]
    prs.part.drop_rel(sld.rId)
    sldIdLst.remove(sld)
    prs.save(path)
    res["flags"].append({"slide": slide_no, "severity": "info", "message": f"Slide {slide_no} removed as requested"})
    for m in res["slide_map"]:
        if m["slide"] == slide_no:
            m["status"] = "removed"
