"""Conditional routing for InfrastructureRecommendationGraph.

Both routing decisions are deterministic string comparisons over already-set
state fields - never an LLM call. See nodes.classify_investigation_type for
where investigation_type itself is decided (also deterministic).
"""

from __future__ import annotations

from app.graph.state import InfrastructureRecommendationState
from app.models.enums import InvestigationType


def route_after_plan(state: InfrastructureRecommendationState) -> str:
    itype = state["investigation_type"]
    if itype == InvestigationType.QUESTION:
        return "retrieve_related_context"
    if itype == InvestigationType.REFUSED:
        return "generate_final_report"
    return "identify_candidate_infrastructure"


def route_after_risk(state: InfrastructureRecommendationState) -> str:
    if not state.get("human_review_required", True):
        return "generate_final_report"
    return "human_review_interrupt"
