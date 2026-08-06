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
    audit_id: int, *, output_json: str | None, success: bool, error_message: str | None = None
) -> None:
    # CompletedAt is computed here in Python (the same clock source as
    # StartedAt in log_start), not via SQL Server's SYSUTCDATETIME(). Mixing
    # two independent clocks for a single "start <= end" invariant risks
    # tripping CK_AgentAuditLog_CompletedAfterStarted under any skew between
    # the app host's and the DB server's clocks, however small - a single
    # clock source makes CompletedAt >= StartedAt true by construction.
    execute(
        f"UPDATE {T('AgentAuditLog')} SET OutputJson = :output_json, CompletedAt = :completed_at, "
        f"Success = :success, ErrorMessage = :error_message WHERE AuditId = :id",
        {
            "output_json": output_json,
            "completed_at": _now(),
            "success": success,
            "error_message": error_message,
            "id": audit_id,
        },
    )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)
