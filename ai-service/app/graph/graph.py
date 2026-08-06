"""Compiles and runs InfrastructureRecommendationGraph.

``run_investigation`` creates the Investigation row first (so its id can seed
a stable LangGraph ``thread_id``), then invokes the compiled graph. When a
node calls ``interrupt()`` (human_review_interrupt), the graph pauses and
this function returns a ``status: "AwaitingReview"`` payload instead of a
final report. ``resume_investigation`` reconnects to that same thread with a
``Command(resume=...)`` to continue past the interrupt.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

import structlog
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config import get_settings
from app.graph import nodes, router
from app.graph.state import InfrastructureRecommendationState, new_state
from app.repositories import investigation_repository

logger = structlog.get_logger(__name__)


def _build_graph() -> StateGraph:
    g = StateGraph(InfrastructureRecommendationState)

    g.add_node("parse_user_request", nodes.parse_user_request)
    g.add_node("load_application_requirements", nodes.load_application_requirements)
    g.add_node("create_investigation_plan", nodes.create_investigation_plan)
    g.add_node("identify_candidate_infrastructure", nodes.identify_candidate_infrastructure)
    g.add_node("apply_hard_eligibility_rules", nodes.apply_hard_eligibility_rules)
    g.add_node("calculate_current_capacity", nodes.calculate_current_capacity)
    g.add_node("calculate_projected_utilization", nodes.calculate_projected_utilization)
    g.add_node("run_capacity_forecast", nodes.run_capacity_forecast)
    g.add_node("analyze_dependencies", nodes.analyze_dependencies)
    g.add_node("calculate_candidate_scores", nodes.calculate_candidate_scores)
    g.add_node("rank_candidates", nodes.rank_candidates)
    g.add_node("retrieve_related_context", nodes.retrieve_related_context)
    g.add_node("generate_recommendation_explanations", nodes.generate_recommendation_explanations)
    g.add_node("assess_risk_and_confidence", nodes.assess_risk_and_confidence)
    g.add_node("human_review_interrupt", nodes.human_review_interrupt)
    g.add_node("generate_final_report", nodes.generate_final_report)
    g.add_node("persist_recommendations", nodes.persist_recommendations)
    g.add_node("complete_investigation", nodes.complete_investigation)

    g.add_edge(START, "parse_user_request")
    g.add_edge("parse_user_request", "load_application_requirements")
    g.add_edge("load_application_requirements", "create_investigation_plan")

    g.add_conditional_edges(
        "create_investigation_plan", router.route_after_plan,
        {
            "retrieve_related_context": "retrieve_related_context",
            "generate_final_report": "generate_final_report",
            "identify_candidate_infrastructure": "identify_candidate_infrastructure",
        },
    )

    g.add_edge("identify_candidate_infrastructure", "apply_hard_eligibility_rules")
    g.add_edge("apply_hard_eligibility_rules", "calculate_current_capacity")
    g.add_edge("calculate_current_capacity", "calculate_projected_utilization")
    g.add_edge("calculate_projected_utilization", "run_capacity_forecast")
    g.add_edge("run_capacity_forecast", "analyze_dependencies")
    g.add_edge("analyze_dependencies", "calculate_candidate_scores")
    g.add_edge("calculate_candidate_scores", "rank_candidates")
    g.add_edge("rank_candidates", "retrieve_related_context")

    g.add_edge("retrieve_related_context", "generate_recommendation_explanations")
    g.add_edge("generate_recommendation_explanations", "assess_risk_and_confidence")

    g.add_conditional_edges(
        "assess_risk_and_confidence", router.route_after_risk,
        {"generate_final_report": "generate_final_report", "human_review_interrupt": "human_review_interrupt"},
    )
    g.add_edge("human_review_interrupt", "generate_final_report")

    g.add_edge("generate_final_report", "persist_recommendations")
    g.add_edge("persist_recommendations", "complete_investigation")
    g.add_edge("complete_investigation", END)

    return g


@lru_cache(maxsize=1)
def get_checkpointer() -> SqliteSaver:
    path = str(get_settings().service.checkpoint_file)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)


@lru_cache(maxsize=1)
def get_compiled_graph():
    return _build_graph().compile(checkpointer=get_checkpointer())


def _thread_config(investigation_id: int) -> dict:
    return {"configurable": {"thread_id": f"investigation-{investigation_id}"}}


def _summarize(final_state: dict) -> dict:
    return {
        "investigation_id": final_state.get("investigation_id"),
        "investigation_type": final_state.get("investigation_type"),
        "status": "AwaitingReview" if (final_state.get("human_review_required") and not final_state.get("decision") and "__interrupt__" in final_state) else "Completed",
        "confidence": final_state.get("confidence"),
        "eligible_candidates": final_state.get("eligible_candidates"),
        "rejected_candidates": final_state.get("rejected_candidates"),
        "candidate_scores": final_state.get("candidate_scores"),
        "forecast_results": final_state.get("forecast_results"),
        "recommendation_explanations": final_state.get("recommendation_explanations"),
        "final_report": final_state.get("final_report"),
        "errors": final_state.get("errors", []),
    }


def run_investigation(*, query: str, created_by: int) -> dict:
    from app.observability.metrics import investigations_total

    investigation_id = investigation_repository.create(query, "Question", created_by)
    state = new_state(query, created_by)
    state["investigation_id"] = investigation_id

    compiled = get_compiled_graph()
    config = _thread_config(investigation_id)
    result = compiled.invoke(state, config=config)
    investigations_total.labels(investigation_type=result.get("investigation_type", "Unknown")).inc()

    if result.get("__interrupt__"):
        return {
            "investigation_id": investigation_id, "status": "AwaitingReview",
            "review_payload": result["__interrupt__"][0].value if result["__interrupt__"] else None,
        }
    return _summarize(result)


def resume_investigation(*, investigation_id: int, decision: str, reviewer_employee_id: int, comments: str | None) -> dict:
    from langgraph.types import Command

    compiled = get_compiled_graph()
    config = _thread_config(investigation_id)
    resume_payload = {"decision": decision, "reviewer_employee_id": reviewer_employee_id, "comments": comments}
    result = compiled.invoke(Command(resume=resume_payload), config=config)
    return _summarize(result)


def get_investigation_state(investigation_id: int) -> dict | None:
    compiled = get_compiled_graph()
    snapshot = compiled.get_state(_thread_config(investigation_id))
    return dict(snapshot.values) if snapshot else None
