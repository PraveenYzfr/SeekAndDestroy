"""LangChain chains. Every function here is a thin, testable wrapper:
build evidence -> call run_structured -> (if evidence carries numbers)
assert_no_number_drift -> return the parsed Pydantic object.

None of these functions touch the database or the deterministic engines
directly - they take already-computed results as arguments. That separation
is what makes app.agents safe to unit test with MockChatModel alone.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from app.agents.guards import assert_no_number_drift
from app.agents.structured import run_structured
from app.models.agent_contracts import (
    ApplicationHostingRequirement,
    CandidateExplanation,
    CapacityRequirement,
    ForecastExplanation,
    FinalRecommendationReport,
    GroundedAnswer,
    InvestigationPlan,
    RightSizingExplanation,
    TradeOffSummary,
)
from app.models.forecast import ResourceForecast
from app.models.rightsizing import ApplicationRightSizingResult, ClusterRightSizingResult
from app.models.scoring import CandidateScore
from app.prompts.templates import (
    CANDIDATE_EXPLANATION_SYSTEM,
    FINAL_REPORT_SYSTEM,
    FORECAST_EXPLANATION_SYSTEM,
    GROUNDED_QA_SYSTEM,
    REJECTION_ANSWER_SYSTEM,
    INTENT_PARSER_SYSTEM,
    REQUIREMENT_EXTRACTION_SYSTEM,
    RIGHTSIZING_EXPLANATION_SYSTEM,
    TRADEOFF_SYSTEM,
    with_evidence,
)


def parse_investigation_plan(llm: BaseChatModel, user_query: str) -> InvestigationPlan:
    human = f"User request:\n{user_query}\n\nClassify it and produce a short investigation plan."
    return run_structured(llm, INTENT_PARSER_SYSTEM, human, InvestigationPlan)


def extract_hosting_requirement(llm: BaseChatModel, user_query: str) -> ApplicationHostingRequirement:
    human = f"User request:\n{user_query}\n\nExtract the hosting requirement."
    return run_structured(llm, REQUIREMENT_EXTRACTION_SYSTEM, human, ApplicationHostingRequirement)


def extract_capacity_requirement(llm: BaseChatModel, user_query: str) -> CapacityRequirement:
    human = f"User request:\n{user_query}\n\nExtract the raw capacity requirement."
    return run_structured(llm, REQUIREMENT_EXTRACTION_SYSTEM, human, CapacityRequirement)


def explain_candidate(
    llm: BaseChatModel, candidate: CandidateScore, application_code: str | None
) -> CandidateExplanation:
    evidence = {
        "cluster_code": candidate.cluster_code,
        "eligibility_status": candidate.eligibility_status,
        "overall_score": float(candidate.overall_score) if candidate.overall_score is not None else None,
        # Cost is deliberately withheld from the narration evidence. It is an
        # internal chargeback rate, not spend, and quoting it ("an estimated
        # monthly cost of 7000.0") reads as a real figure driving the
        # recommendation when it is neither. A model can only mention a number
        # it was given, so removing it here removes it from the prose.
        "subscores": candidate.subscores.model_dump() if candidate.subscores else None,
        "rule_results": candidate.rule_results,
        "projected_utilization": candidate.projected.model_dump() if candidate.projected else None,
    }
    human = with_evidence(
        f"Explain candidate {candidate.cluster_code} for hosting application {application_code or '(new capacity request)'}.",
        evidence,
    )
    result = run_structured(llm, CANDIDATE_EXPLANATION_SYSTEM, human, CandidateExplanation)
    assert_no_number_drift(result, evidence)
    return result


def _num(value) -> float | None:
    """Decimal -> float, keeping None as None.

    None means "not measured" and must stay distinguishable from 0.0, which means
    "measured, and idle". Collapsing them would let a cluster with no telemetry
    read as a cluster with nothing running on it.
    """
    return None if value is None else float(value)


def _pct(part, whole) -> float | None:
    """Allocation as a percentage, or None when there is nothing to divide by."""
    if part is None or not whole:
        return None
    return round(float(part) / float(whole) * 100.0, 2)


def explain_cluster_right_sizing(llm: BaseChatModel, result: ClusterRightSizingResult) -> RightSizingExplanation:
    # The figures the model is being asked to narrate, as VALUES.
    #
    # They were reachable only through `rationale`, which is prose the engine
    # writes. So a model quoting "CPU utilization at 93.98%" was quoting a number
    # the engine had computed - correctly - out of a sentence, and the grader had
    # no value to check it against and reported an invented figure.
    #
    # That is the harsh half of a fidelity number that could not be defended in
    # either direction: too generous while any digit in the prompt grounded, too
    # harsh once only values did. Neither was a model behaving badly; both were
    # the engine handing over its arithmetic as narrative.
    #
    # Passing the snapshot fixes it at the source rather than teaching the grader
    # to read prose - which is the thing the grader exists NOT to do.
    snapshot = result.snapshot
    evidence = {
        "cluster_or_application_code": result.cluster_code,
        "classification": result.classification,
        "estimated_monthly_savings": float(result.estimated_monthly_savings),
        "current_node_count": result.current_node_count,
        "recommended_node_count": result.recommended_node_count,
        "measured_cpu_percent": _num(snapshot.measured_cpu_percent),
        "measured_memory_percent": _num(snapshot.measured_memory_percent),
        "measured_storage_percent": _num(snapshot.measured_storage_percent),
        "allocated_cpu_percent": _pct(snapshot.allocated_cpu_cores, snapshot.effective_cpu_cores),
        "allocated_memory_percent": _pct(snapshot.allocated_memory_gb, snapshot.effective_memory_gb),
        "available_cpu_cores": _num(snapshot.available_cpu_cores),
        "available_memory_gb": _num(snapshot.available_memory_gb),
        "rationale": result.rationale,
        "risks": result.risks,
    }
    human = with_evidence(f"Explain the right-sizing recommendation for cluster {result.cluster_code}.", evidence)
    parsed = run_structured(llm, RIGHTSIZING_EXPLANATION_SYSTEM, human, RightSizingExplanation)
    assert_no_number_drift(parsed, evidence)
    return parsed


def explain_application_right_sizing(
    llm: BaseChatModel, result: ApplicationRightSizingResult
) -> RightSizingExplanation:
    evidence = {
        "cluster_or_application_code": result.application_code,
        "classification": result.classification,
        "estimated_monthly_savings": float(result.estimated_monthly_savings),
        "allocated_cpu_cores": float(result.allocated_cpu_cores),
        "recommended_cpu_cores": float(result.recommended_cpu_cores),
        "rationale": result.rationale,
    }
    human = with_evidence(f"Explain the right-sizing recommendation for application {result.application_code}.", evidence)
    parsed = run_structured(llm, RIGHTSIZING_EXPLANATION_SYSTEM, human, RightSizingExplanation)
    assert_no_number_drift(parsed, evidence)
    return parsed


def explain_forecast(llm: BaseChatModel, entity_code: str, forecast: ResourceForecast) -> ForecastExplanation:
    evidence = {
        "entity_code": entity_code,
        "resource": forecast.resource,
        "exhaustion_date": forecast.exhaustion_date.isoformat() if forecast.exhaustion_date else None,
        "current_percent": float(forecast.current_percent),
        "predicted_percent": float(forecast.predicted_percent),
        "breaches_threshold_within_horizon": forecast.breaches_threshold_within_horizon,
        "recommended_action": forecast.recommended_action,
    }
    human = with_evidence(f"Explain the {forecast.resource} forecast for {entity_code}.", evidence)
    parsed = run_structured(llm, FORECAST_EXPLANATION_SYSTEM, human, ForecastExplanation)
    assert_no_number_drift(parsed, evidence)
    return parsed


def summarize_tradeoffs(llm: BaseChatModel, title: str, candidates: list[CandidateScore]) -> TradeOffSummary:
    evidence = {
        "title": title,
        "candidates": [
            {
                "cluster_code": c.cluster_code,
                "eligibility_status": c.eligibility_status,
                "overall_score": float(c.overall_score) if c.overall_score is not None else None,
            }
            for c in candidates
        ],
    }
    human = with_evidence(f"Summarize the trade-offs for: {title}.", evidence)
    return run_structured(llm, TRADEOFF_SYSTEM, human, TradeOffSummary)


def answer_grounded_question(llm: BaseChatModel, question: str, retrieved_context: list[dict]) -> GroundedAnswer:
    # The evidence keys are part of the prompt the model reads, so they are
    # named for the domain rather than for the retrieval machinery. Calling
    # this "retrieved_context" is how answers ended up saying "the retrieved
    # context contains no Java-specific information" - the model was echoing
    # our vocabulary back at an engineer who does not care how we found it.
    evidence = {"question": question, "records_from_the_estate": retrieved_context}
    human = with_evidence(f"Answer this question using only the evidence: {question}", evidence)
    return run_structured(llm, GROUNDED_QA_SYSTEM, human, GroundedAnswer)


def answer_rejection_question(
    llm: BaseChatModel, question: str, rule_evidence: list[dict], follow_up_options: list[str]
) -> GroundedAnswer:
    """Two sentences and a question, not a summary.

    Separate from answer_grounded_question because the constraint is the point.
    Sharing a prompt and hoping the model infers brevity from short evidence is
    what was tried first: the evidence went from eleven documents to two and the
    answer stayed long, because nothing had ever told it to be brief.

    ``follow_up_options`` are computed by services.refinement from the rules that
    actually failed. The model is told to use only these, so it cannot offer a
    move that does not follow from the verdict.
    """
    evidence = {
        "question": question,
        "rules_that_failed": rule_evidence,
        "follow_up_options": follow_up_options,
    }
    human = with_evidence(
        f"Answer in at most two sentences, then ask one follow-up question: {question}", evidence
    )
    return run_structured(llm, REJECTION_ANSWER_SYSTEM, human, GroundedAnswer)


def generate_final_report(
    llm: BaseChatModel, investigation_id: int, title: str, evidence: dict
) -> FinalRecommendationReport:
    full_evidence = {"investigation_id": investigation_id, **evidence}
    human = with_evidence(f"Generate the final investigation report: {title}.", full_evidence)
    parsed = run_structured(llm, FINAL_REPORT_SYSTEM, human, FinalRecommendationReport)
    assert_no_number_drift(parsed, full_evidence)
    return parsed
