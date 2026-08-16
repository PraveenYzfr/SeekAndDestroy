"""Every model call leaves a record.

The gap this closes: ``audit_repository``'s own docstring promised auditing for
"every MCP tool invocation *and LangGraph node execution*", the MCP half was
built, and the AI service wrote zero rows to sad.AgentAuditLog. Six chains ran
against a paid provider and nothing durable recorded what was asked or what
came back - so "what did investigation 74's report actually say?" had no
answer once the response had been returned to the caller.

Auditing lives in ``run_structured`` because every chain in app.agents funnels
through it. One hook, no chain signature changed.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.agents.mock_llm import MockChatModel
from app.agents.structured import run_structured
from app.models.agent_contracts import GroundedAnswer
from app.observability import audit_context
from app.repositories.base import T, fetch_one


def _unique_prompt() -> str:
    """A prompt nothing has seen before.

    run_structured caches on the exact prompt text, so a reused prompt would
    test the cache-hit path while claiming to test the generation path.
    """
    return f"Answer this question using only the retrieved context: {uuid4().hex}"


def _row_for(graph_node: str) -> dict | None:
    return fetch_one(
        f"SELECT TOP 1 * FROM {T('AgentAuditLog')} WHERE GraphNode = :node ORDER BY AuditId DESC",
        {"node": graph_node},
    )


def test_a_model_call_is_recorded_with_the_node_that_made_it():
    node = f"test_node_{uuid4().hex[:8]}"
    with audit_context.graph_node(None, node):
        run_structured(MockChatModel(), "system prompt", _unique_prompt(), GroundedAnswer)

    row = _row_for(node)
    assert row is not None, "a model call produced no audit row"
    assert row["ToolName"] == "llm:GroundedAnswer"
    assert row["Success"] is True
    assert row["CompletedAt"] is not None
    assert row["OutputJson"], "the model's answer must be recorded, not just the fact of a call"


def test_the_prompt_and_the_model_are_recorded_not_just_the_schema():
    """An audit row that says only "a GroundedAnswer happened" cannot answer
    which model produced it - which is the question that matters once more than
    one provider is in play.
    """
    node = f"test_node_{uuid4().hex[:8]}"
    prompt = _unique_prompt()
    with audit_context.graph_node(None, node):
        run_structured(MockChatModel(), "system prompt", prompt, GroundedAnswer)

    payload = json.loads(_row_for(node)["InputJson"])
    assert payload["human"].startswith("Answer this question")
    assert payload["model"], "the model identity must be on the row"
    assert payload["cache_hit"] is False


def test_a_cached_answer_is_still_recorded_and_flagged_as_cached():
    """Cache hits are audited too. Without them the log has a hole exactly
    where "what did this investigation report?" gets asked - the text was
    served to a caller, so it belongs in the record, marked as served rather
    than generated.
    """
    prompt = _unique_prompt()
    first = f"test_node_{uuid4().hex[:8]}"
    second = f"test_node_{uuid4().hex[:8]}"

    with audit_context.graph_node(None, first):
        run_structured(MockChatModel(), "system prompt", prompt, GroundedAnswer)
    with audit_context.graph_node(None, second):
        run_structured(MockChatModel(), "system prompt", prompt, GroundedAnswer)

    assert json.loads(_row_for(first)["InputJson"])["cache_hit"] is False
    assert json.loads(_row_for(second)["InputJson"])["cache_hit"] is True
    assert _row_for(second)["OutputJson"], "a served answer is still an answer"


def test_an_audit_failure_does_not_take_the_investigation_down_with_it():
    """Fail-open on purpose. This platform produces recommendations and never
    executes an infrastructure change, so losing an investigation to protect
    its log is the worse trade - but the failure is logged rather than
    swallowed silently.
    """
    from app.agents import structured

    def explode(**kwargs):
        raise RuntimeError("audit table unavailable")

    original = structured.audit_repository.log_start
    structured.audit_repository.log_start = explode
    try:
        result = run_structured(MockChatModel(), "system prompt", _unique_prompt(), GroundedAnswer)
    finally:
        structured.audit_repository.log_start = original

    assert isinstance(result, GroundedAnswer), "the narration must survive a broken audit table"


def test_a_call_outside_any_node_is_still_audited():
    """A chain invoked directly by an API endpoint has no graph node. That is
    a row with a NULL GraphNode, not a missing row.
    """
    prompt = _unique_prompt()
    run_structured(MockChatModel(), "system prompt", prompt, GroundedAnswer)

    row = fetch_one(
        f"SELECT TOP 1 * FROM {T('AgentAuditLog')} WHERE ToolName = 'llm:GroundedAnswer' "
        f"AND GraphNode IS NULL ORDER BY AuditId DESC",
        {},
    )
    assert row is not None
    assert row["Success"] is True


# =============================================================================
# The context itself
# =============================================================================


def test_the_scope_is_restored_rather_than_cleared():
    """Nested scopes must restore the outer one. Clearing instead would leave
    the rest of an enclosing node's model calls unattributed.
    """
    with audit_context.graph_node(7, "outer"):
        with audit_context.graph_node(7, "inner"):
            assert audit_context.current().graph_node == "inner"
        assert audit_context.current().graph_node == "outer"
    assert audit_context.current().graph_node is None


def test_the_scope_survives_an_exception():
    with pytest.raises(ValueError):
        with audit_context.graph_node(7, "boom"):
            raise ValueError("node failed")
    assert audit_context.current().investigation_id is None


def test_every_graph_node_carries_its_name_into_the_audit_scope():
    """The wrapper is applied centrally in app.graph.graph so a node added
    later cannot forget it. If that wiring is ever removed, audit rows quietly
    stop naming where they came from - which nobody notices until they need
    the log.
    """
    from app.graph.graph import _build_graph

    graph = _build_graph()
    assert len(graph.nodes) >= 19

    seen = []
    for name, node in graph.nodes.items():
        runnable = getattr(node, "runnable", None)
        target = getattr(runnable, "func", None) or getattr(node, "func", None)
        if target is not None and getattr(target, "__wrapped__", None) is not None:
            seen.append(name)
    assert len(seen) >= 19, f"nodes missing the audit wrapper: {set(graph.nodes) - set(seen)}"
