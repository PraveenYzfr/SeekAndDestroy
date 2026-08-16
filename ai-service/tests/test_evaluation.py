"""Grading the model against the platform's own answer key.

This platform can do what most cannot: check an answer against a known correct
one. Placement, scoring and forecasting are computed in Python, so the evidence
handed to a model is the answer key for the prose it writes back - and the
whole evaluation runs off sad.AgentAuditLog, so it costs a table scan rather
than a provider bill.

The graders are deliberately not models. An LLM-as-judge would introduce the
exact failure being measured: a grader that hallucinates cannot detect
hallucination.
"""

from __future__ import annotations

import json

from app.agents.structured import AUDIT_LIMIT, _audit_payload
from app.evaluation.graders import (
    completeness,
    entity_fidelity,
    grade_call,
    number_fidelity,
    was_truncated,
)


# =============================================================================
# Numbers
# =============================================================================


EVIDENCE = json.dumps({
    "cluster_code": "nyc-p006",
    "overall_score": 91.8,
    "projected": {"projected_headroom_percent": 27.28, "projected_cpu_utilization_percent": 72.72},
})


def test_a_number_the_model_was_never_given_is_flagged():
    """The failure this exists to catch: prose that quotes a figure nobody
    computed. It reads exactly like a real one.
    """
    result = number_fidelity("The cluster scored 45.3 overall.", EVIDENCE)
    assert result.ungrounded == ["45.3"]
    assert result.rate == 0.0


def test_a_figure_copied_correctly_is_grounded():
    result = number_fidelity("It scored 91.8 with 27.28% headroom.", EVIDENCE)
    assert result.ungrounded == []
    assert result.rate == 1.0


def test_rounding_is_reporting_not_drift():
    """"about 27.3% headroom" is honest prose about 27.28. Flagging it would
    bury the real failures in noise nobody reads.
    """
    assert number_fidelity("roughly 27.3% headroom remains", EVIDENCE).ungrounded == []
    assert number_fidelity("a score of about 92", EVIDENCE).ungrounded == []


def test_a_count_the_model_can_see_for_itself_is_not_an_invention():
    """"three candidates" and "the second one" are the model counting a list
    it was shown, not quoting a metric.
    """
    assert number_fidelity("3 candidates were considered; the 2nd is cheapest.", EVIDENCE).ungrounded == []


def test_a_wrong_figure_close_to_a_real_one_is_still_caught():
    """89.61 misread as 91.8 is the realistic failure - two real scores from
    the same shortlist swapped. It must not pass as a rounding.
    """
    assert number_fidelity("It scored 89.61 overall.", EVIDENCE).ungrounded == ["89.61"]


# =============================================================================
# Entities
# =============================================================================


def test_an_invented_cluster_code_is_flagged():
    """The most damaging output this platform can produce: prose that reads
    like a recommendation and names infrastructure that was never a candidate.
    """
    result = entity_fidelity("Place it on dal-p099.", EVIDENCE)
    assert result.ungrounded == ["dal-p099"]


def test_a_cluster_from_the_evidence_passes():
    assert entity_fidelity("nyc-p006 is the best fit.", EVIDENCE).ungrounded == []


def test_prose_with_no_entity_reference_is_not_scored():
    """Zero of zero is not a perfect score - reporting it as one would inflate
    every average it lands in.
    """
    assert entity_fidelity("Headroom is comfortable.", EVIDENCE).applies is False


# =============================================================================
# Completeness
# =============================================================================


def test_an_empty_required_field_is_a_quality_failure_the_schema_cannot_see():
    """A report that parses cleanly with an empty executive summary is valid
    and useless. Two real TradeOffSummary calls did exactly this.
    """
    assert completeness({"summary": "   "}, ("summary",)).ungrounded == ["summary"]
    assert completeness({"summary": "Two clusters fit."}, ("summary",)).rate == 1.0


# =============================================================================
# The evidence has to be complete to judge against
# =============================================================================


def test_a_truncated_prompt_is_never_graded_for_fidelity():
    """The regression this caught in its first run: capping the recorded
    prompt made every figure past the cut look invented, and the harness
    reported a well-behaved provider at 62% entity fidelity.
    """
    truncated = json.dumps({"model": "m", "truncated": True, "human": "partial evi"})
    output = json.dumps({"summary": "nyc-p006 scored 91.8"})

    names = {g.name for g in grade_call(truncated, output, "CandidateExplanation")}
    assert "number_fidelity" not in names
    assert "entity_fidelity" not in names
    assert "completeness" in names, "completeness needs only the output, so it still applies"


def test_an_unparseable_row_counts_as_truncated_rather_than_perfect():
    assert was_truncated('{"model": "m", "human": "cut off mid-tok') is True
    assert was_truncated(json.dumps({"model": "m", "truncated": False})) is False


def test_the_audit_payload_stays_valid_json_when_it_has_to_be_cut():
    """The actual bug: slicing the serialised string cut mid-token, so the row
    no longer parsed, the model attribution was lost, and the grader silently
    judged against half the evidence.
    """
    payload = _audit_payload("deepseek:v4", "sys " * 40_000, "human " * 40_000, cache_hit=False)

    parsed = json.loads(payload)  # must not raise
    assert parsed["model"] == "deepseek:v4"
    assert parsed["truncated"] is True
    assert len(payload) <= AUDIT_LIMIT


def test_an_uncut_payload_is_not_marked_truncated():
    payload = _audit_payload("deepseek:v4", "system", "human", cache_hit=True)
    parsed = json.loads(payload)
    assert parsed["truncated"] is False
    assert parsed["cache_hit"] is True
    assert parsed["human"] == "human"


# =============================================================================
# End to end, over what the platform has actually recorded
# =============================================================================


def test_the_scorecard_reads_the_audit_log():
    from app.evaluation.harness import evaluate

    result = evaluate(limit=200)
    assert result["calls_graded"] >= 0
    for card in result["models"]:
        assert card["calls"] > 0
        assert card["ungradeable"] <= card["calls"]
        for rate in (card["number_fidelity"], card["entity_fidelity"], card["completeness"]):
            assert rate is None or 0.0 <= rate <= 1.0
