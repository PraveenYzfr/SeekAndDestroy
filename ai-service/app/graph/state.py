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
    #: Set instead of final_report when the reviewer rejects. Carries the
    #: question to put back to them and the constraints they can pick from.
    rejection_prompt: Optional[dict]
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

    # --- conversation context ------------------------------------------------
    # Same rule as above: every one of these must be declared or it is silently
    # dropped and the follow-up loses exactly the context it exists to carry.
    conversation_id: Optional[str]
    #: The query the pipeline classifies and extracts from. Equal to user_query
    #: for an ordinary request; for a follow-up it is user_query with the
    #: previous subject appended (app.graph.conversation.carry_subject), so
    #: "what about in staging?" still knows which application it is about.
    #: user_query stays the literal text the engineer typed - it is what gets
    #: shown back to them, and rewriting that would be putting words in their
    #: mouth.
    resolved_query: Optional[str]
    follow_up_kind: Optional[str]
    prior_investigation_id: Optional[int]
    #: The previous investigation's own evidence, already shaped like retrieval
    #: results, for the Question path to answer from.
    prior_context_docs: list[dict]

    #: Data centres the engineer has just declined, carried from the previous
    #: turn so "give me from a different DC" re-runs the placement somewhere
    #: else instead of returning the shortlist that was rejected.
    #:
    #: EMPTY AND ABSENT MEAN THE SAME THING HERE - no exclusion. That has to
    #: stay true all the way down to the SQL: a list that arrives empty must
    #: not become NOT IN (), which excludes everything. Same distinction the
    #: CapacityRequirement bug got wrong in the other direction.
    exclude_data_centers: list[str]


def new_state(
    user_query: str,
    created_by: int,
    *,
    conversation_id: str | None = None,
    resolved_query: str | None = None,
    follow_up_kind: str | None = None,
    prior_investigation_id: int | None = None,
    prior_context_docs: list[dict] | None = None,
    exclude_data_centers: list[str] | None = None,
) -> InfrastructureRecommendationState:
    return InfrastructureRecommendationState(
        user_query=user_query, created_by=created_by, errors=[], candidate_clusters=[], candidate_nodes=[],
        eligible_candidates=[], rejected_candidates=[], capacity_calculations={}, forecast_results={},
        candidate_scores=[], retrieved_context=[], recommendation_explanations=[], human_review_required=True,
        final_report=None, rejection_prompt=None, investigation_id=None, parsed_intent=None, application_requirements=None,
        capacity_requirements=None, investigation_plan=None, investigation_type="Question", requirement=None,
        confidence="Medium", decision=None, reviewer_employee_id=None, review_comments=None,
        selected_cluster_code=None, selected_host_name=None,
        conversation_id=conversation_id,
        # Defaults to the literal query, so every node can read resolved_query
        # unconditionally and an investigation with no conversation behaves
        # exactly as it always did.
        resolved_query=resolved_query or user_query,
        follow_up_kind=follow_up_kind,
        prior_investigation_id=prior_investigation_id,
        prior_context_docs=prior_context_docs or [],
        exclude_data_centers=exclude_data_centers or [],
    )
