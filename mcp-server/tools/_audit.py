"""Shared audit + JSON helpers used by every tool module.

Every tool call is logged to sad.AgentAuditLog (start + completion, success or
failure) per the security requirement in the specification ("audit every
invocation"). Tools call :func:`audited` explicitly inside their body (rather
than via a decorator) so their type-annotated signature stays exactly as
written for MCP's schema introspection.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from app.repositories import audit_repository
from app.utils.json_utils import to_jsonable
from pydantic import BaseModel


def _default(obj: Any):
    # Numbers/dates route through to_jsonable so a Decimal never becomes a
    # string here either (audit-log text should read the same as the tool's
    # actual return value) - see app/utils/json_utils.py for why this can't
    # just be obj.model_dump(mode="json").
    if isinstance(obj, BaseModel):
        return to_jsonable(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def to_json(obj: Any, limit: int = 8000) -> str:
    text = json.dumps(obj, default=_default)
    return text[:limit]


def audited(tool_name: str, params: dict, fn: Callable[[], Any]) -> Any:
    investigation_id = params.get("investigation_id")
    audit_id = audit_repository.log_start(
        tool_name=tool_name, investigation_id=investigation_id, graph_node=None,
        input_json=to_json({k: v for k, v in params.items() if k != "self"}),
    )
    try:
        result = fn()
    except Exception as exc:
        audit_repository.log_complete(audit_id, output_json=None, success=False, error_message=str(exc))
        raise
    audit_repository.log_complete(audit_id, output_json=to_json(result), success=True)
    return result


def model_list(items) -> list[dict]:
    return [to_jsonable(i) for i in items]


def model_dict(item) -> dict | None:
    return to_jsonable(item) if item is not None else None
