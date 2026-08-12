"""State for the InfrastructureRecommendationGraph.

The 17 fields named in the specification are present verbatim, plus a small
number of control-flow fields (investigation_type, created_by, reviewer_*)
that the graph needs to route and to record human review - these are
plumbing, not additional business concepts.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict


class InfrastructureRecommendationState(TypedDict, total=False):
    # --- specified state fields -------------------------------------------
    investigation_id: Optional[int]
    user_query: str
    parsed_intent: Optional[dict]
    application_requirements: Optional[dict]
    capacity_requirements: Optional[dict]
    investigation_plan: Optional[dict]
    candidate_clusters: list[dict]
    candidate_nodes: list[dict]
    eligible_candidates: list[dict]
    rejected_candidates: list[dict]
    capacity_calculations: dict
    forecast_results: dict
    candidate_scores: list[dict]
    retrieved_context: list[dict]
    recommendation_explanations: list[dict]
    human_review_required: bool
    final_report: Optional[dict]
    errors: Annotated[list[str], operator.add]

    # --- control-flow plumbing ----------------------------------------------
    investigation_type: str
    created_by: int
    requirement: Optional[dict]  # HostingRequirement, when applicable
    confidence: str  # High | Medium | Low
    decision: Optional[str]
    reviewer_employee_id: Optional[int]
    review_comments: Optional[str]
    # Which option the reviewer picked. Must be declared here or LangGraph
    # drops them: a TypedDict state silently discards keys a node returns that
    # the schema does not name, so the selection would reach the graph and
    # vanish before persist_recommendations could act on it.
    selected_cluster_code: Optional[str]
    selected_host_name: Optional[str]


def new_state(user_query: str, created_by: int) -> InfrastructureRecommendationState:
    return InfrastructureRecommendationState(
        user_query=user_query, created_by=created_by, errors=[], candidate_clusters=[], candidate_nodes=[],
        eligible_candidates=[], rejected_candidates=[], capacity_calculations={}, forecast_results={},
        candidate_scores=[], retrieved_context=[], recommendation_explanations=[], human_review_required=True,
        final_report=None, investigation_id=None, parsed_intent=None, application_requirements=None,
        capacity_requirements=None, investigation_plan=None, investigation_type="Question", requirement=None,
        confidence="Medium", decision=None, reviewer_employee_id=None, review_comments=None,
        selected_cluster_code=None, selected_host_name=None,
    )
