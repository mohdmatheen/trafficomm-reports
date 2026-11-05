"""REAL-CLIENT validation (Around The Clock Media samples shared by Trafficomm). Skipped when the
sample files are not present (they are client data and are not committed). Kept separate from the
synthetic acceptance tests."""
import os

import pytest

from app.profiles import ATC_PROFILE
from tests.helpers import REAL, check, run

pytestmark = pytest.mark.real_client
FILES = ["Brighton-College-Dubai.xlsx", "Brighton-College-Dubai_1.xlsx", "Brighton-College-Al-Ain.xlsx", "Brighton-College-abu_Dubai.xlsx", "Bloom-World-Academy.xlsx"]
DECK = "BWA_Report_With_Comments-22.09.2026.pptx"

if not all(os.path.exists(os.path.join(REAL, f)) for f in FILES + [DECK]):
    pytest.skip("real ATC sample files not available", allow_module_level=True)


@pytest.fixture(scope="module")
def atc(tmp_path_factory):
    files = [(os.path.join(REAL, f), "raw") for f in FILES] + [(os.path.join(REAL, DECK), "client_deck")]
    return run(files, deliverables=("excel", "deck_update"), profile=ATC_PROFILE, answers={"conversion_metric": "leads_meta_form"},
               workdir=str(tmp_path_factory.mktemp("atc")))


def test_atc_totals(atc):
    assert atc["status"] == "ready_for_review"
    t = atc["summary"]["totals"]
    assert atc["summary"]["included_rows"] == 22
    assert t["spend"] == pytest.approx(12603.21, abs=0.005)
    assert t["impressions"] == 1692696
    assert t["link_clicks"] == 19189
    assert t["video_plays_3s"] == 95749
    assert t["leads_meta_form"] == 81
    assert any(o["type"] == "identical_file" for o in atc["overlaps"])  # Brighton-College-Dubai_1.xlsx
    assert check(atc, "reconciliation")["status"] == "pass"


def test_atc_deck_matches_employee_made_deck(atc):
    """The supplied deck was built by an employee from these same exports, so a correct update
    changes no figures. The only expected difference: slide 28 shows Frequency '0.00' where reach is 0;
    we write '-' because frequency is undefined (zero denominator)."""
    sm = [m for m in atc["deck"]["slide_map"] if m.get("classification") == "metric_slide"]
    assert len(sm) == 22 and all(m["status"] == "updated" or m["status"] == "unchanged" for m in sm)
    fig = [c for c in atc["deck"]["changes"] if c["metric"] not in ("range", "period_label", "screenshot border")]
    assert [(c["slide"], c["col"]) for c in fig] == [(28, "Frequency")], fig
    assert atc["deck"]["unchanged_cells"] >= 22 * 7


def test_atc_all_clicks_revision_consistent(tmp_path):
    """Employee asks for all clicks, but ATC's exports only contain 'Link clicks'. Found by running
    the real samples: the request must not silently blank every click figure - link clicks are kept,
    the employee is told why, and Excel and the deck still agree."""
    files = [(os.path.join(REAL, f), "raw") for f in FILES] + [(os.path.join(REAL, DECK), "client_deck")]
    r = run(files, deliverables=("excel", "deck_update"), profile=ATC_PROFILE, answers={"conversion_metric": "leads_meta_form"},
            plan_overrides={"click_metric": "clicks_all"}, workdir=str(tmp_path))
    assert r["status"] == "ready_for_review"
    assert check(r, "crossfile")["status"] == "pass"
    assert r["summary"]["totals"]["clicks"] == 19189
    assert any("always report link clicks" in w for w in r["warnings"])  # agency rule
    assert not [c for c in r["deck"]["changes"] if c.get("col") == "Clicks"]
