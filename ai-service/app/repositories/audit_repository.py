"""Audit logging for every MCP tool invocation and LangGraph node execution.

Security requirement (spec §20/§14): every MCP tool call is audited. Callers
use :func:`log_start` / :func:`log_complete` as a pair around the call.
"""

from __future__ import annotations

import structlog

from app.repositories.base import T, execute, execute_insert

logger = structlog.get_logger(__name__)


def log_start(
    *, tool_name: str, investigation_id: int | None, graph_node: str | None, input_json: str | None
) -> int:
    return execute_insert(
        T("AgentAuditLog"),
        "AuditId",
        {
            "InvestigationId": investigation_id,
            "GraphNode": graph_node,
            "ToolName": tool_name,
            "InputJson": input_json,
            "OutputJson": None,
            "StartedAt": _now(),
            "CompletedAt": None,
            "Success": None,
            "ErrorMessage": None,
        },
    )


def log_complete(
    audit_id: int, *, output_json: str | None, success: bool, error_message: str | None = None,
    usage: dict | None = None,
) -> None:
    """Close an audit row.

    ``usage`` carries the provider's token counts, normalised by the chat
    models to one vocabulary regardless of whether the provider called them
    ``prompt_tokens`` or ``promptTokenCount``. It is None for the mock model,
    for a cache hit, and for any provider that omits the block - and the
    columns stay NULL in that case rather than 0, because "no tokens recorded"
    and "zero tokens billed" are different facts and averaging them together
    would understate real cost.
    """
    # CompletedAt is computed here in Python (the same clock source as
    # StartedAt in log_start), not via SQL Server's SYSUTCDATETIME(). Mixing
    # two independent clocks for a single "start <= end" invariant risks
    # tripping CK_AgentAuditLog_CompletedAfterStarted under any skew between
    # the app host's and the DB server's clocks, however small - a single
    # clock source makes CompletedAt >= StartedAt true by construction.
    usage = usage or {}
    provider = usage.get("provider")
    model = usage.get("model")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    # Priced on the MODEL alone, not on the "provider:model" identity stored in
    # ModelIdentity. sad.ModelPrice keys on the model name, so looking it up by
    # the combined string would miss every row and quietly record every call as
    # unpriced - a spend report reading zero, with nothing to indicate it was
    # wrong rather than free.
    cost = _price(model, prompt_tokens, completion_tokens)

    execute(
        f"UPDATE {T('AgentAuditLog')} SET OutputJson = :output_json, CompletedAt = :completed_at, "
        f"Success = :success, ErrorMessage = :error_message, PromptTokens = :prompt_tokens, "
        f"CompletionTokens = :completion_tokens, ModelIdentity = :model_identity, "
        f"Provider = :provider, CostUsd = :cost, UnitPriceInput = :unit_in, "
        f"UnitPriceOutput = :unit_out, "
        # Latency from the row's own StartedAt rather than a value passed in: the
        # caller does not always know when the row was opened, and the two
        # timestamps are already guaranteed ordered by CK_AgentAuditLog_CompletedAfterStarted.
        f"LatencyMs = DATEDIFF(millisecond, StartedAt, :completed_at) "
        f"WHERE AuditId = :id",
        {
            "output_json": output_json,
            "completed_at": _now(),
            "success": success,
            "error_message": error_message,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model_identity": f"{provider}:{model}" if provider else None,
            "provider": provider,
            "cost": cost.cost,
            "unit_in": cost.input_per_million,
            "unit_out": cost.output_per_million,
            "id": audit_id,
        },
    )


def _price(model, prompt_tokens, completion_tokens):
    """Cost of this call, or UNPRICED.

    Wrapped so that pricing can never break audit logging. The audit row is the
    record that a model was called at all; a missing price table, an unseeded
    database or a lookup error must degrade to "cost unknown" rather than losing
    the row entirely. Unknown is already a first-class value here - the daily
    spend view counts unpriced calls separately rather than treating them as
    zero - so falling back to it costs accuracy and not integrity.
    """
    from app.services.model_pricing import UNPRICED, cost_of

    if not model:
        return UNPRICED
    try:
        return cost_of(model, prompt_tokens, completion_tokens)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("audit.pricing_failed", model=model)
        return UNPRICED


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)
