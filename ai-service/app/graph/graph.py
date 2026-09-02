"""Compiles and runs InfrastructureRecommendationGraph.

``run_investigation`` creates the Investigation row first (so its id can seed
a stable LangGraph ``thread_id``), then invokes the compiled graph. When a
node calls ``interrupt()`` (human_review_interrupt), the graph pauses and
this function returns a ``status: "AwaitingReview"`` payload instead of a
final report. ``resume_investigation`` reconnects to that same thread with a
``Command(resume=...)`` to continue past the interrupt.

When a ``conversation_id`` is supplied, this module is also where a turn is
placed in its conversation: what the engineer said, what came back, and - for
a follow-up - which earlier investigation it refers to. Resolution happens
before the Investigation row is created, because a follow-up that only asks to
see the previous shortlist again should not manufacture a second investigation
of the same question. See app.graph.conversation.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache, wraps

import structlog
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config import get_settings
from app.graph import conversation, nodes, router
from app.graph.state import InfrastructureRecommendationState, new_state
from app.observability import audit_context
from app.repositories import conversation_repository, investigation_repository

logger = structlog.get_logger(__name__)


def _audited(node_name: str, fn):
    """Wrap a node so every model call inside it is attributable.

    Applied centrally here rather than by decorating each node, so a node added
    later cannot forget to do it - the audit rows would simply stop naming
    where they came from, which is the kind of gap nobody notices until they
    need the log.
    """

    @wraps(fn)
    def wrapper(state: InfrastructureRecommendationState) -> dict:
        with audit_context.graph_node(state.get("investigation_id"), node_name):
            return fn(state)

    return wrapper


def _build_graph() -> StateGraph:
    g = StateGraph(InfrastructureRecommendationState)

    g.add_node("parse_user_request", _audited("parse_user_request", nodes.parse_user_request))
    g.add_node("load_application_requirements", _audited("load_application_requirements", nodes.load_application_requirements))
    g.add_node("create_investigation_plan", _audited("create_investigation_plan", nodes.create_investigation_plan))
    g.add_node("identify_candidate_infrastructure", _audited("identify_candidate_infrastructure", nodes.identify_candidate_infrastructure))
    g.add_node("apply_hard_eligibility_rules", _audited("apply_hard_eligibility_rules", nodes.apply_hard_eligibility_rules))
    g.add_node("calculate_current_capacity", _audited("calculate_current_capacity", nodes.calculate_current_capacity))
    g.add_node("calculate_projected_utilization", _audited("calculate_projected_utilization", nodes.calculate_projected_utilization))
    g.add_node("run_capacity_forecast", _audited("run_capacity_forecast", nodes.run_capacity_forecast))
    g.add_node("analyze_dependencies", _audited("analyze_dependencies", nodes.analyze_dependencies))
    g.add_node("calculate_candidate_scores", _audited("calculate_candidate_scores", nodes.calculate_candidate_scores))
    g.add_node("rank_candidates", _audited("rank_candidates", nodes.rank_candidates))
    g.add_node("select_candidate_nodes", _audited("select_candidate_nodes", nodes.select_candidate_nodes))
    g.add_node("retrieve_related_context", _audited("retrieve_related_context", nodes.retrieve_related_context))
    g.add_node("generate_recommendation_explanations", _audited("generate_recommendation_explanations", nodes.generate_recommendation_explanations))
    g.add_node("assess_risk_and_confidence", _audited("assess_risk_and_confidence", nodes.assess_risk_and_confidence))
    g.add_node("human_review_interrupt", _audited("human_review_interrupt", nodes.human_review_interrupt))
    g.add_node("generate_final_report", _audited("generate_final_report", nodes.generate_final_report))
    g.add_node("persist_recommendations", _audited("persist_recommendations", nodes.persist_recommendations))
    g.add_node("ask_rejection_reason", _audited("ask_rejection_reason", nodes.ask_rejection_reason))
    g.add_node("complete_investigation", _audited("complete_investigation", nodes.complete_investigation))

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
    g.add_edge("rank_candidates", "select_candidate_nodes")
    g.add_edge("select_candidate_nodes", "retrieve_related_context")

    g.add_edge("retrieve_related_context", "generate_recommendation_explanations")
    g.add_edge("generate_recommendation_explanations", "assess_risk_and_confidence")

    g.add_conditional_edges(
        "assess_risk_and_confidence", router.route_after_risk,
        {"generate_final_report": "generate_final_report", "human_review_interrupt": "human_review_interrupt"},
    )
    # Approve writes the report. Reject asks what was wrong instead - rejecting a
    # placement used to produce an executive summary of the thing just declined.
    # Both paths still reach persist_recommendations, so the decision is recorded
    # either way; only the narration differs.
    g.add_conditional_edges(
        "human_review_interrupt",
        nodes.route_after_decision,
        {
            "generate_final_report": "generate_final_report",
            "ask_rejection_reason": "ask_rejection_reason",
        },
    )
    g.add_edge("ask_rejection_reason", "persist_recommendations")

    g.add_edge("generate_final_report", "persist_recommendations")
    g.add_edge("persist_recommendations", "complete_investigation")
    g.add_edge("complete_investigation", END)

    return g


@lru_cache(maxsize=1)
def get_checkpointer():
    """Where a paused investigation lives between ``interrupt()`` and resume.

    Redis when configured, SQLite otherwise.

    The SQLite saver was the original, and it has three problems that only a
    shared store fixes:

    * **It is a file, so it cannot be shared.** Two replicas mean two files.
      Investigation 42 pauses on replica A; the engineer's Approve lands on
      replica B, which has never heard of that thread, and the investigation
      is unresumable. No load balancer helps - the state is on another
      machine's disk. This alone pinned the service to a single replica.
    * **The file lives inside the container** at ``.state/checkpoints.db``
      with no volume behind it, so every redeploy destroyed every paused
      investigation. That was silent: a redeploy looks successful, and the
      loss only appears when somebody clicks Approve on a review that is now
      a dead thread.
    * **SQLite serialises writes.** One connection shared across threads means
      concurrent investigations contend on a single file lock.

    Redis is already on the ``hub`` network for caching, so this adds no new
    infrastructure. ``setup()`` is idempotent and creates the indices on first
    use. SQLite remains the default for a laptop with no Redis running, where
    a single process makes all three problems moot.
    """
    settings = get_settings()
    url = settings.cache.checkpoint_redis_url or (
        settings.cache.redis_url if settings.cache.backend == "redis" else ""
    )
    if url:
        from langgraph.checkpoint.redis import RedisSaver

        # from_conn_string is a context manager; entering it without exiting
        # keeps the connection open for the process lifetime, which is what a
        # module-level singleton wants.
        saver = RedisSaver.from_conn_string(url).__enter__()
        saver.setup()
        logger.info("graph.checkpointer", backend="redis")
        return saver

    path = str(settings.service.checkpoint_file)
    conn = sqlite3.connect(path, check_same_thread=False)
    logger.warning(
        "graph.checkpointer",
        backend="sqlite",
        detail="single-process only; paused investigations are lost on restart",
        path=path,
    )
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
        "candidate_nodes": final_state.get("candidate_nodes"),
        "forecast_results": final_state.get("forecast_results"),
        "recommendation_explanations": final_state.get("recommendation_explanations"),
        "final_report": final_state.get("final_report"),
        # Set instead of final_report when the reviewer rejected. Omitted here it
        # would be built correctly and then dropped on the way out, which is the
        # quietest possible way for a feature to not exist.
        "rejection_prompt": final_state.get("rejection_prompt"),
        "errors": final_state.get("errors", []),
    }


def _conversation_reply(text: str, conversation_id: str | None) -> dict:
    """A direct answer with no Investigation row behind it - a greeting, a
    request too vague to act on, or a reference with nothing to refer to."""
    return {
        "investigation_id": None,
        "conversation_id": conversation_id,
        "investigation_type": "Conversation",
        "status": "Completed",
        "final_report": {
            "investigation_id": None,
            "title": "",
            "executive_summary": text,
            "top_recommendation": None,
            "alternatives_considered": [],
            "risks": [],
            "next_steps": [],
            "human_action_required": "",
        },
        "candidate_scores": [],
        "errors": [],
    }


def _prior_investigation(conversation_id: str) -> conversation.PriorInvestigation | None:
    """The last investigation this conversation produced, read back out of its
    checkpoint.

    The checkpoint rather than the InfrastructureRecommendation rows: those
    hold only the persisted top N, while "why was that one rejected?" is
    usually about a candidate that never made the shortlist, and the reason it
    was rejected lives in the rule results on the state.
    """
    investigation_id = conversation_repository.last_investigation_id(conversation_id)
    if investigation_id is None:
        return None
    state = get_investigation_state(investigation_id)
    if not state:
        return None
    row = investigation_repository.get_by_id(investigation_id)
    return conversation.PriorInvestigation.from_state(
        investigation_id, state, status=row.Status if row else "Completed"
    )


def _recall(prior: conversation.PriorInvestigation, conversation_id: str | None) -> dict:
    """Show the previous shortlist again, without re-running anything.

    Re-running would create a second Investigation row and, because utilization
    moves between requests, could answer "again" with different numbers - which
    is not what "again" means. A shortlist still awaiting a decision comes back
    as a live review payload, so the engineer can still act on it rather than
    reading a table that no longer does anything.
    """
    from app.graph.nodes import build_review_payload

    summary = conversation.recall_summary(prior)
    if prior.awaiting_review:
        payload = build_review_payload(
            {
                "candidate_scores": prior.candidate_scores,
                "investigation_id": prior.investigation_id,
                "investigation_type": prior.investigation_type,
                "confidence": prior.confidence,
            }
        )
        payload["message"] = summary
        return {
            "investigation_id": prior.investigation_id,
            "conversation_id": conversation_id,
            "investigation_type": prior.investigation_type,
            "status": "AwaitingReview",
            "review_payload": payload,
            "recall_of_investigation_id": prior.investigation_id,
        }

    report = dict(prior.final_report or {})
    return {
        "investigation_id": prior.investigation_id,
        "conversation_id": conversation_id,
        "investigation_type": prior.investigation_type,
        "status": "Completed",
        "recall_of_investigation_id": prior.investigation_id,
        "final_report": {
            "investigation_id": prior.investigation_id,
            "title": report.get("title") or f"Options from investigation #{prior.investigation_id}",
            "executive_summary": summary,
            "top_recommendation": report.get("top_recommendation"),
            "alternatives_considered": report.get("alternatives_considered") or [],
            "risks": report.get("risks") or [],
            "next_steps": report.get("next_steps") or [],
            "human_action_required": report.get("human_action_required") or "",
        },
        "candidate_scores": prior.candidate_scores,
        "errors": [],
    }


def run_investigation(*, query: str, created_by: int, conversation_id: str | None = None) -> dict:
    from app.graph.nodes import quick_reply
    from app.observability.metrics import investigations_total

    # What this turn refers to, if anything. Resolved before the Investigation
    # row exists, because two of the three answers below do not want a row at
    # all. ``conversation_id`` is None for every caller outside the chat (the
    # structured screens, the MCP client), and those keep the exact behaviour
    # they always had: no prior, no resolution, no turns recorded.
    prior = _prior_investigation(conversation_id) if conversation_id else None
    resolution = conversation.resolve(query, prior)

    if conversation_id:
        conversation_repository.add_turn(conversation_id, "User", query)

    def answered(result: dict) -> dict:
        if conversation_id:
            conversation_repository.add_turn(
                conversation_id, "Assistant", conversation.turn_summary(result),
                investigation_id=result.get("investigation_id"),
            )
        return result

    # A reference with nothing to refer to ("give me the options again" as the
    # first message of a chat). Saying so beats running it as a fresh
    # investigation and reporting that the context was empty.
    if resolution.reply is not None:
        return answered(_conversation_reply(resolution.reply, conversation_id))

    if resolution.kind == conversation.RECALL and resolution.prior is not None:
        return answered(_recall(resolution.prior, conversation_id))

    # Answer directly, before an Investigation row exists, when the input is
    # not an investigation: a greeting, or an infrastructure ask with the
    # specifics missing. Running the full graph on "hi" produced an
    # Investigation row, a Question classification, an empty retrieval and the
    # report "I don't have enough grounded information" - a correct answer to
    # a question nobody asked, and one that buries the real investigations.
    #
    # Skipped for a follow-up: "what about another cluster?" is short and
    # infrastructure-shaped, and asking it to be more specific when the
    # specifics are sitting in the previous turn is the same unhelpfulness in
    # a different costume.
    if resolution.kind is None:
        reply = quick_reply(query)
        if reply is not None:
            return answered(_conversation_reply(reply, conversation_id))

    investigation_id = investigation_repository.create(query, "Question", created_by, conversation_id)
    state = new_state(
        query, created_by,
        conversation_id=conversation_id,
        resolved_query=resolution.resolved_query,
        follow_up_kind=resolution.kind,
        prior_investigation_id=prior.investigation_id if prior else None,
        # Only a question *about* the previous results needs them as grounding.
        # A follow-up that inherits a subject is a fresh investigation of that
        # subject, and feeding it the old run's candidates would let stale
        # numbers narrate a new answer.
        prior_context_docs=(
            conversation.grounding_documents(prior)
            if resolution.kind == conversation.ABOUT_PREVIOUS and prior is not None
            else []
        ),
        # "give me from a different DC" re-runs the placement with the data
        # centres of the rejected shortlist excluded. Empty for every other turn,
        # and empty means no exclusion - see conversation.excluded_data_centers.
        exclude_data_centers=resolution.exclude_data_centers,
    )
    state["investigation_id"] = investigation_id

    compiled = get_compiled_graph()
    config = _thread_config(investigation_id)
    result = compiled.invoke(state, config=config)
    investigations_total.labels(investigation_type=result.get("investigation_type", "Unknown")).inc()

    if result.get("__interrupt__"):
        return answered({
            "investigation_id": investigation_id, "conversation_id": conversation_id,
            "status": "AwaitingReview",
            "review_payload": result["__interrupt__"][0].value if result["__interrupt__"] else None,
        })
    return answered({**_summarize(result), "conversation_id": conversation_id})


def resume_investigation(
    *, investigation_id: int, decision: str, reviewer_employee_id: int, comments: str | None,
    selected_cluster_code: str | None = None, selected_host_name: str | None = None,
) -> dict:
    """``selected_*`` name the option the reviewer chose. Approving without
    naming one leaves every recommendation PendingReview rather than approving
    the whole shortlist - three approved placements for one workload is not a
    decision, it is the absence of one.
    """
    from langgraph.types import Command

    compiled = get_compiled_graph()
    config = _thread_config(investigation_id)
    resume_payload = {
        "decision": decision, "reviewer_employee_id": reviewer_employee_id, "comments": comments,
        "selected_cluster_code": selected_cluster_code, "selected_host_name": selected_host_name,
    }
    result = compiled.invoke(Command(resume=resume_payload), config=config)

    # The decision is part of the conversation: without this turn, "show me
    # those options again" after an approval would replay the shortlist with
    # no sign that one of them had already been chosen.
    row = investigation_repository.get_by_id(investigation_id)
    conversation_id = row.ConversationId if row else None
    if conversation_id:
        chosen = " / ".join(p for p in (selected_cluster_code, selected_host_name) if p)
        conversation_repository.add_turn(
            conversation_id, "Assistant",
            f"Decision on investigation #{investigation_id}: {decision}"
            + (f", {chosen}." if chosen else "."),
            investigation_id=investigation_id,
        )
    return {**_summarize(result), "conversation_id": conversation_id}


def get_investigation_state(investigation_id: int) -> dict | None:
    compiled = get_compiled_graph()
    snapshot = compiled.get_state(_thread_config(investigation_id))
    return dict(snapshot.values) if snapshot else None
