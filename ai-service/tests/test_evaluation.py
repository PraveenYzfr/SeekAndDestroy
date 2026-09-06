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

import pytest

from app.agents.structured import AUDIT_LIMIT, _audit_payload
from app.evaluation.graders import (
    completeness,
    entity_fidelity,
    grade_call,
    number_fidelity,
    required_fields_for,
    was_truncated,
)
from app.prompts.templates import with_evidence

# =============================================================================
# Numbers
# =============================================================================


#: The evidence OBJECT, not a JSON string of it.
#:
#: This was json.dumps(...), and a string sent number_fidelity down a branch that
#: scraped every figure out of the text - so these tests were checking that a
#: number in the prose matched a number in a string, which is not what the grader
#: is for. Production passes the object; so do the tests now.
EVIDENCE = {
    "cluster_code": "nyc-p006",
    "overall_score": 91.8,
    "projected": {"projected_headroom_percent": 27.28, "projected_cpu_utilization_percent": 72.72},
}


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
    and useless - the type system cannot see the difference.

    (The two TradeOffSummary calls this originally cited were a false positive:
    the grader demanded a field that contract does not have. See
    test_required_fields_come_from_the_contract_itself.)
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
    assert result["calls_seen"] >= 0
    for card in result["models"]:
        assert card["calls"] > 0
        assert card["calls"] == card["generated"] + card["cached"]
        assert card["ungradeable"] <= card["generated"]
        for measured in card["properties"].values():
            assert 0.0 <= measured["rate"] <= 1.0
            assert measured["observations"] > 0


# =============================================================================
# The required fields have to be the contract's, not a guess at them
# =============================================================================


def test_required_fields_come_from_the_contract_itself():
    """The hand-written list was wrong on its first outing: it demanded a
    ``summary`` from TradeOffSummary, which has no such field. Two perfectly
    good calls were reported as empty, and the same wrong field names had been
    copied into the UI, which rendered an empty panel.

    Deriving them from the Pydantic model means a schema change cannot leave a
    stale list behind.
    """
    from app.models.agent_contracts import TradeOffSummary

    assert required_fields_for("TradeOffSummary") == ("title", "comparison_points", "recommendation")
    assert set(required_fields_for("TradeOffSummary")) <= set(TradeOffSummary.model_fields)


@pytest.mark.parametrize(
    "schema",
    ["CandidateExplanation", "FinalRecommendationReport", "GroundedAnswer",
     "ForecastExplanation", "RightSizingExplanation", "TradeOffSummary", "InvestigationPlan"],
)
def test_every_graded_schema_names_only_fields_it_actually_has(schema):
    from app.models import agent_contracts

    model = getattr(agent_contracts, schema)
    assert set(required_fields_for(schema)) <= set(model.model_fields)
    assert required_fields_for(schema), f"{schema} has no required narrative field to check"


def test_an_optional_field_is_not_required():
    """``top_recommendation`` is Optional on the report contract - a run with
    no eligible candidate legitimately has none, and demanding one would flag
    correct behaviour."""
    assert "top_recommendation" not in required_fields_for("FinalRecommendationReport")


def test_an_unknown_schema_is_not_graded_for_completeness():
    assert required_fields_for("SomethingElse") == ()


# =============================================================================
# Scorecard method
# =============================================================================


def _row(audit_id, *, model="m", cached=False, output=None, success=True, duration=10, truncated=False):
    return {
        "AuditId": audit_id,
        "ToolName": "llm:CandidateExplanation",
        # A REAL prompt, built the way production builds one.
        #
        # This was `"human": "cluster nyc-p006 scored 91.8"` - prose, with no
        # evidence object in it. That fixture modelled an audit row the system
        # never produces, and the tests passed BECAUSE of a defect: grade_call
        # graded against the prompt string, so "91.8" in the answer grounded
        # against "91.8" in the prompt text. Both halves were the model's own
        # output and nothing was being checked against the engine at all.
        #
        # with_evidence writes the marker and the JSON that grade_call now
        # recovers, so these tests exercise the path production uses.
        "InputJson": json.dumps({
            "model": model, "cache_hit": cached, "truncated": truncated,
            "human": with_evidence(
                "Explain this candidate",
                {"cluster_code": "nyc-p006", "overall_score": 91.8},
            ),
        }),
        "OutputJson": output if output is not None else json.dumps(
            {"cluster_code": "nyc-p006", "eligibility_status": "Eligible", "summary": "nyc-p006 scored 91.8"}
        ),
        "Success": success,
        "DurationMs": duration,
    }


def test_a_cached_answer_is_counted_but_not_graded_again():
    """The same text served twenty times is one success, not twenty. Grading
    each hit weights the score towards whatever happens to be popular and
    inflates every denominator with it.
    """
    from app.evaluation.harness import ModelScorecard

    card = ModelScorecard(model="m")
    card.record(_row(1), cached=False, schema="CandidateExplanation")
    for i in range(20):
        card.record(_row(2 + i, cached=True), cached=True, schema="CandidateExplanation")

    assert card.calls == 21
    assert card.generated == 1
    assert card.cached == 20
    # One generated call's worth of observations, not twenty-one.
    assert card.totals["number_fidelity"].total == 1


def test_the_denominator_is_reported_with_the_rate():
    from app.evaluation.harness import ModelScorecard

    card = ModelScorecard(model="m")
    card.record(_row(1), cached=False, schema="CandidateExplanation")
    numbers = card.as_dict()["properties"]["number_fidelity"]
    assert numbers["rate"] == 1.0
    assert numbers["observations"] == 1


def test_latency_percentiles_come_from_the_audit_row():
    from app.evaluation.harness import ModelScorecard

    card = ModelScorecard(model="m")
    for i, ms in enumerate([100, 200, 300, 400, 5000]):
        card.record(_row(i + 1, duration=ms), cached=False, schema="CandidateExplanation")
    result = card.as_dict()
    assert result["latency_p50_ms"] == 300
    assert result["latency_p95_ms"] == 5000


def test_a_truncated_row_is_ungradeable_not_a_failure():
    from app.evaluation.harness import ModelScorecard

    card = ModelScorecard(model="m")
    card.record(_row(1, truncated=True), cached=False, schema="CandidateExplanation")
    result = card.as_dict()
    assert result["ungradeable"] == 1
    assert result["properties"] == {}, "nothing measurable must not read as a perfect score"


# =============================================================================
# The gate
# =============================================================================


def _result(rate: float, observations: int) -> dict:
    return {"models": [{
        "model": "m",
        "properties": {"entity_fidelity": {"rate": rate, "observations": observations}},
    }]}


def test_a_model_below_the_bar_fails_the_run():
    from app.evaluation.harness import check_thresholds

    problems = check_thresholds(_result(0.94, 500), {"entity_fidelity": 1.0})
    assert any(p.startswith("FAILED") for p in problems)


def test_a_model_at_the_bar_passes():
    from app.evaluation.harness import check_thresholds

    assert check_thresholds(_result(1.0, 500), {"entity_fidelity": 1.0}) == []


def test_a_thin_sample_is_skipped_rather_than_failed():
    """Three observations at 66% is not evidence of a regression. A gate that
    cries wolf gets switched off - but it says "skipped" out loud, because
    silence would read as a clean result.
    """
    from app.evaluation.harness import check_thresholds

    problems = check_thresholds(_result(0.66, 3), {"entity_fidelity": 1.0}, min_observations=20)
    assert len(problems) == 1
    assert problems[0].startswith("SKIPPED")
