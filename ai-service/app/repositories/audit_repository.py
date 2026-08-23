"""Audit logging for every MCP tool invocation and LangGraph node execution.

Security requirement (spec §20/§14): every MCP tool call is audited. Callers
use :func:`log_start` / :func:`log_complete` as a pair around the call.
"""

from __future__ import annotations

from app.repositories.base import T, execute, execute_insert


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
    execute(
        f"UPDATE {T('AgentAuditLog')} SET OutputJson = :output_json, CompletedAt = :completed_at, "
        f"Success = :success, ErrorMessage = :error_message, PromptTokens = :prompt_tokens, "
        f"CompletionTokens = :completion_tokens, ModelIdentity = :model_identity "
        f"WHERE AuditId = :id",
        {
            "output_json": output_json,
            "completed_at": _now(),
            "success": success,
            "error_message": error_message,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "model_identity": (
                f"{usage.get('provider')}:{usage.get('model')}" if usage.get("provider") else None
            ),
            "id": audit_id,
        },
    )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)
