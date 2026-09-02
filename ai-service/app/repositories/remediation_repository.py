"""The failures the graph used to drop.

Seven except branches in app/graph/nodes.py log a warning and continue. What
they DO is right - a narration failure must not fail an investigation whose
numbers are already computed - but nothing counted them, nothing stored them,
and "how often does narration fail" was answerable only by grepping container
logs on a production box.

"Best effort" describes what the code should do about a failure. It does not
decide whether anyone should be told.

WRITES ARE BEST-EFFORT AND COUNTED. Enqueuing a failure must not turn a
degraded answer into a broken one, so record() never raises. But every loss
increments a counter, because a queue that silently fails to record failures is
the same bug one level up - and that is not hypothetical: sad.AnswerEvaluation
sat empty for hours behind a missing INSERT grant while every write "succeeded".
"""

from __future__ import annotations

import json

import structlog

from app.repositories.base import T, execute, fetch_all

logger = structlog.get_logger(__name__)

#: Long text is truncated rather than rejected. A row that records the failure
#: imperfectly is worth more than no row, and the alternative - a write that
#: fails on an oversized narration - loses exactly the pathological cases most
#: worth looking at.
_TEXT_LIMIT = 60_000
_DETAIL_LIMIT = 1900

_COLUMNS = (
    "InvestigationId", "ConversationId", "Site", "Source", "Detail",
    "AnswerText", "EvidenceJson",
    "JudgeRelevance", "JudgeGroundedness", "JudgeActionability", "JudgeJustifications",
    "Status",
)


def record(
    *,
    site: str,
    source: str = "python",
    investigation_id: int | None = None,
    conversation_id: str | None = None,
    detail: str | None = None,
    answer_text: str | None = None,
    evidence: object | None = None,
    judge: dict | None = None,
) -> bool:
    """Enqueue one failure. Never raises. Returns whether it was stored.

    ``site`` is the logger event name from the drop site, unchanged, so a row
    traces back to the exact except branch that produced it without a
    translation table that would drift from the code.
    """
    from app.observability.metrics import remediation_enqueued_total

    judge = judge or {}
    params = {
        "InvestigationId": investigation_id,
        "ConversationId": conversation_id,
        "Site": site[:80],
        "Source": source,
        "Detail": (detail or "")[:_DETAIL_LIMIT] or None,
        "AnswerText": (answer_text or "")[:_TEXT_LIMIT] or None,
        "EvidenceJson": _as_json(evidence),
        "JudgeRelevance": judge.get("relevance"),
        "JudgeGroundedness": judge.get("groundedness"),
        "JudgeActionability": judge.get("actionability"),
        # Stored beside the numbers because a bare 2/5 says something is wrong
        # and nothing about what.
        "JudgeJustifications": _as_json(judge.get("justifications")),
        "Status": "Queued",
    }
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    try:
        execute(f"INSERT INTO {T('RemediationTask')} ({columns}) VALUES ({placeholders})", params)
    except Exception as exc:  # noqa: BLE001
        # Counted, not merely logged. A queue that quietly fails to record
        # failures reproduces the bug it exists to fix.
        remediation_enqueued_total.labels(site=site[:80], outcome="lost").inc()
        logger.warning("remediation.enqueue_failed", site=site, error=str(exc)[:300])
        return False
    remediation_enqueued_total.labels(site=site[:80], outcome="stored").inc()
    return True


def _as_json(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)[:_TEXT_LIMIT]
    except Exception:  # noqa: BLE001
        # Unserialisable evidence is still worth its shape. repr beats dropping
        # the column and losing the only record of what the model was given.
        return repr(value)[:_TEXT_LIMIT]


def queue(status: str | None = "Queued", limit: int = 100) -> list[dict]:
    """The open queue, newest first. Read-only: nothing acts on these yet.

    The triage taxonomy is a guess until there are real failures to check it
    against, and an agent built on a guessed taxonomy would confidently mis-route
    fifty cases before anybody noticed.
    """
    where = "WHERE Status = :status" if status else ""
    return fetch_all(
        f"""
        SELECT TOP (:limit)
               RemediationTaskId, InvestigationId, ConversationId, Site, Source,
               TriageClass, Detail, JudgeRelevance, JudgeGroundedness,
               JudgeActionability, Attempt, Status, CreatedAt
        FROM   {T('RemediationTask')}
        {where}
        ORDER BY RemediationTaskId DESC
        """,
        {"limit": limit, **({"status": status} if status else {})},
        max_rows=max(limit, 1),
    )


def counts_by_site(days: int = 7) -> list[dict]:
    """How often each drop site fires. The question that needed a log grep."""
    return fetch_all(
        f"""
        SELECT   Site, Source, COUNT(*) AS Failures,
                 SUM(CASE WHEN Status = 'Queued' THEN 1 ELSE 0 END) AS StillQueued,
                 MAX(CreatedAt) AS LastSeen
        FROM     {T('RemediationTask')}
        WHERE    CreatedAt >= DATEADD(day, -:days, SYSUTCDATETIME())
        GROUP BY Site, Source
        ORDER BY Failures DESC
        """,
        {"days": days},
        max_rows=200,
    )
