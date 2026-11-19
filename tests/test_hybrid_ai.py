"""Hybrid AI: built-in rules by default; Claude for a client's first report or a new report
structure; repeat data updates use rules only; any Claude failure falls back to rules.
Claude is replaced by a fake client - no network."""
import types

from app.engine import ai as AI
from app.profiles import GENERIC_PROFILE
from tests.helpers import fx, run


class FakeClaude:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def parse(self, model, max_tokens, system, output_config, messages, output_format):
        self.calls.append(output_format.__name__)
        if self.fail:
            raise RuntimeError("API down")
        if output_format is AI.Commentary:
            out = AI.Commentary(statements=[AI.Statement(type="observation", text="Spend was cheap this month!", fact_ids=["total.spend"])])
        elif output_format is AI.PlanOut:
            out = AI.PlanOut(**{**AI.Plan().model_dump(), "split_tabs_by": "", "conversion_metric": "",
                                "click_metric": "unspecified", "notes": ["read by fake Claude"]})
        else:
            out = AI.RevisionPlan(ops=[AI.RevisionOp(op="add_breakdown", value="market", note="Add market breakdown")], summary="Add market breakdown")
        return types.SimpleNamespace(stop_reason="end_turn", parsed_output=out)


def hybrid(fail=False):
    p = AI.HybridProvider.__new__(AI.HybridProvider)
    p.model, p.name, p.fallback, p.steps, p.mocked, p.use_claude, p.reason = "claude-test", "Hybrid", AI.RuleProvider(), [], False, False, ""
    fake = FakeClaude(fail)
    p.client = types.SimpleNamespace(messages=types.SimpleNamespace(parse=fake.parse))
    return p, fake


META = [(fx("meta_acme_sept.csv"), "raw")]


def test_first_report_uses_claude_then_repeat_uses_rules():
    p, fake = hybrid()
    r = run(META, answers={"conversion_metric": "none"}, instructions="Break down by country.", provider=p)
    assert r["status"] == "ready_for_review"
    assert r["structure"]["known"] is False and "First report" in r["structure"]["reason"]
    assert {s["step"]: s["engine"] for s in r["ai_steps"]} == {"Instructions": "claude", "Insights": "claude"}
    assert [c["text"] for c in r["commentary"]] == ["Spend was cost-efficient this month."]  # language rule applies to Claude
    # next month: same exports layout, same request -> data update -> built-in rules only
    prof = {**GENERIC_PROFILE, "known_structures": [r["structure"]["fingerprint"]]}
    p2, fake2 = hybrid()
    r2 = run([(fx("meta_acme_aug.csv"), "raw")], answers={"conversion_metric": "none"}, instructions="Break down by country.",
             period=("2026-08-01", "2026-08-31"), profile=prof, provider=p2)
    assert r2["structure"]["known"] is True
    assert {s["engine"] for s in r2["ai_steps"]} == {"rules"} and fake2.calls == []
    # a new layout (TikTok added) for the same client -> new structure -> Claude again
    p3, fake3 = hybrid()
    r3 = run(META + [(fx("tiktok_acme_sept_usd.xlsx"), "raw")], answers={"conversion_metric": "none"}, instructions="Break down by country.",
             profile=prof, provider=p3)
    assert r3["structure"]["known"] is False and "New report structure" in r3["structure"]["reason"]
    assert "Commentary" in fake3.calls


def test_claude_failure_falls_back_to_rules():
    p, fake = hybrid(fail=True)
    r = run(META, answers={"conversion_metric": "none"}, provider=p)
    assert r["status"] == "ready_for_review"
    steps = {s["step"]: s for s in r["ai_steps"]}
    assert steps["Instructions"]["engine"] == "rules" and "unavailable" in steps["Instructions"]["reason"]
    assert steps["Insights"]["engine"] == "rules" and r["commentary"]


def test_feedback_uses_rules_unless_unmapped():
    p, fake = hybrid()
    rp = p.interpret_feedback("Split tabs by country", False)
    assert [o.op for o in rp.ops] == ["split_tabs_by"] and fake.calls == []
    p.interpret_feedback("Can you make the story more about the Abu Dhabi school's trend?", False)
    assert fake.calls == ["RevisionPlan"] and p.steps[-1]["engine"] == "claude"


def test_repeat_reuses_saved_claude_reading_without_calling_claude():
    p, fake = hybrid()
    instr = "Please prepare the monthly update. Focus the story on the Abu Dhabi audience and keep it brief for the client team."
    r = run(META, answers={"conversion_metric": "none"}, instructions=instr, provider=p)
    st = r["structure"]
    assert st["claude_plan"]["include_commentary"] is True
    prof = {**GENERIC_PROFILE, "known_structures": [st["fingerprint"]], "structure_plans": {st["fingerprint"]: {**st["claude_plan"], "short_summary": True}}}
    # next month: same layout, free-form instructions worded differently -> still known, saved reading reused
    p2, fake2 = hybrid()
    r2 = run([(fx("meta_acme_aug.csv"), "raw")], answers={"conversion_metric": "none"},
             instructions="Please prepare this month's update. Focus the story on the Abu Dhabi audience and keep it brief for the client.",
             period=("2026-08-01", "2026-08-31"), profile=prof, provider=p2)
    assert r2["structure"]["known"] is True
    assert fake2.calls == []
    steps = {s["step"]: s["engine"] for s in r2["ai_steps"]}
    assert steps == {"Instructions": "saved", "Insights": "rules"}
    assert r2["plan"]["short_summary"] is True


def test_rolling_deck_fingerprint_ignores_added_month_section():
    def deck(n_sections):
        slides = [{"classification": "cover", "tables": []}]
        for _ in range(n_sections):
            slides += [{"classification": "client_content", "tables": [{"metric_cols": {"1": "impressions", "2": "spend"}}]},
                       {"classification": "screenshots", "tables": []}]
        return {"inspection": {"deck": {"slide_count": len(slides), "slides": slides}}}
    a, _ = AI.structure_fingerprint([], deck(2), None, ["deck"], "", {})
    b, _ = AI.structure_fingerprint([], deck(3), None, ["deck"], "", {})
    assert a == b
