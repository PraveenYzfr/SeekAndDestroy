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
        "estimated_monthly_cost": (
            float(candidate.estimated_monthly_cost) if candidate.estimated_monthly_cost is not None else None
        ),
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


def explain_cluster_right_sizing(llm: BaseChatModel, result: ClusterRightSizingResult) -> RightSizingExplanation:
    evidence = {
        "cluster_or_application_code": result.cluster_code,
        "classification": result.classification,
        "estimated_monthly_savings": float(result.estimated_monthly_savings),
        "current_node_count": result.current_node_count,
        "recommended_node_count": result.recommended_node_count,
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
                "estimated_monthly_cost": float(c.estimated_monthly_cost) if c.estimated_monthly_cost is not None else None,
            }
            for c in candidates
        ],
    }
    human = with_evidence(f"Summarize the trade-offs for: {title}.", evidence)
    return run_structured(llm, TRADEOFF_SYSTEM, human, TradeOffSummary)


def answer_grounded_question(llm: BaseChatModel, question: str, retrieved_context: list[dict]) -> GroundedAnswer:
    evidence = {"question": question, "retrieved_context": retrieved_context}
    human = with_evidence(f"Answer this question using only the retrieved context: {question}", evidence)
    return run_structured(llm, GROUNDED_QA_SYSTEM, human, GroundedAnswer)


def generate_final_report(
    llm: BaseChatModel, investigation_id: int, title: str, evidence: dict
) -> FinalRecommendationReport:
    full_evidence = {"investigation_id": investigation_id, **evidence}
    human = with_evidence(f"Generate the final investigation report: {title}.", full_evidence)
    parsed = run_structured(llm, FINAL_REPORT_SYSTEM, human, FinalRecommendationReport)
    assert_no_number_drift(parsed, full_evidence)
    return parsed
