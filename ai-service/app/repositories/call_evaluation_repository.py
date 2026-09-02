"""A stored verdict for every graded model output.

sad.AnswerEvaluation (018) records one row per delivered ANSWER. This records
one row per (model call, grader), so a bad answer can be traced to the call that
caused it: which prompt, which model, which figure.

An answer scoring 0.91 could be one call at 0.55 and four at 1.0, or five at
0.91. The aggregate cannot tell those apart, and "which call invented that
number" is the question anyone actually asks.

WRITES ARE BEST-EFFORT, like the answer-level table above it. A verdict is a
comment on work already handed over; failing to store one must never fail the
thing it was commenting on. Losses are logged at warning rather than swallowed
silently - which is how the answer-level table sat empty for hours behind a
missing INSERT grant while every write "succeeded".
"""

from __future__ import annotations

import json

import structlog

from app.repositories.base import T, execute, fetch_all

logger = structlog.get_logger(__name__)

#: Bump when a grader's RULES change - not when its code is merely refactored.
#:
#: This is part of the uniqueness key on purpose. In one night the graders
#: produced 0.9764, 0.8891 and 0.9740 from the SAME recorded calls: identifier
#: tokenisation was fixed, an injection hole was closed, a list-count rule was
#: added. Every one of those numbers was correct under the rules in force when it
#: ran, and comparing them without knowing which rules applied is meaningless.
#:
#: Re-grading under a new version ADDS a row rather than overwriting the old
#: verdict, so a rule change shows up as two scores for one call instead of one
#: score that silently moved.
GRADER_VERSION = "2026-09-02.a"

_COLUMNS = (
    "AuditId", "InvestigationId", "Grader",
    "Grounded", "Total", "Rate", "UngroundedJson", "GraderVersion",
)

#: The offending tokens are stored so "which figure was ungrounded" needs no
#: re-run. Capped because a pathological narration could otherwise write an
#: unbounded blob into a table meant for reading.
_UNGROUNDED_LIMIT = 1800


def record_many(rows: list[dict]) -> int:
    """Store verdicts for a batch of graded calls. Never raises.

    Returns how many were written, so a caller can report "graded 40, stored 40"
    rather than assuming. A silent difference between those two numbers is
    exactly the failure this module's docstring describes.
    """
    written = 0
    for row in rows:
        ungrounded = row.get("ungrounded") or []
        params = {
            "AuditId": row.get("audit_id"),
            "InvestigationId": row.get("investigation_id"),
            "Grader": row.get("grader"),
            "Grounded": int(row.get("grounded") or 0),
            "Total": int(row.get("total") or 0),
            # NULL rather than 0.0 when nothing was measurable. Zero is a score;
            # "there was nothing to score" is not, and collapsing them would put
            # a perfect-looking 0% into every average.
            "Rate": (
                round(float(row["grounded"]) / float(row["total"]), 4)
                if row.get("total") else None
            ),
            "UngroundedJson": (
                json.dumps(ungrounded)[:_UNGROUNDED_LIMIT] if ungrounded else None
            ),
            "GraderVersion": row.get("grader_version") or GRADER_VERSION,
        }
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        try:
            execute(
                f"INSERT INTO {T('CallEvaluation')} ({columns}) VALUES ({placeholders})",
                params,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            # A duplicate is not a failure: the unique key is
            # (AuditId, Grader, GraderVersion), so re-running a grading pass over
            # calls already scored under the same rules is a no-op by design.
            message = str(exc)
            if "UQ_CallEvaluation" in message or "duplicate key" in message.lower():
                continue
            logger.warning(
                "call_evaluation.persist_failed",
                audit_id=row.get("audit_id"), error=message[:300],
            )
    return written


def for_investigation(investigation_id: int) -> list[dict]:
    """Every graded call in one investigation, newest grading first.

    Joined to the audit row so the caller gets the prompt, the response and the
    model beside the score - the whole point of storing per call rather than per
    answer.
    """
    return fetch_all(
        f"""
        SELECT  e.CallEvaluationId, e.AuditId, e.Grader, e.Grounded, e.Total,
                e.Rate, e.UngroundedJson, e.GraderVersion, e.CreatedAt,
                a.GraphNode, a.ToolName, a.ModelIdentity, a.Provider,
                a.StartedAt, a.CompletedAt, a.Success,
                a.InputJson, a.OutputJson
        FROM    {T('CallEvaluation')} e
        JOIN    {T('AgentAuditLog')} a ON a.AuditId = e.AuditId
        WHERE   e.InvestigationId = :investigation_id
        ORDER BY e.AuditId, e.Grader
        """,
        {"investigation_id": investigation_id},
        max_rows=2000,
    )


def rollup_for_conversation(conversation_id: str) -> list[dict]:
    """One row per grader for a whole conversation.

    Sums the underlying counts rather than averaging the per-call rates. Those
    are different numbers: a call with two figures and a call with two hundred
    contribute equally to a mean of rates, which lets one short narration
    outweigh an entire report. Summing grounded and total gives the rate over
    figures actually written, which is the claim being made.
    """
    return fetch_all(
        f"""
        SELECT  e.Grader,
                SUM(e.Grounded)      AS Grounded,
                SUM(e.Total)         AS Total,
                COUNT(*)             AS Calls,
                MIN(e.GraderVersion) AS MinVersion,
                MAX(e.GraderVersion) AS MaxVersion
        FROM    {T('CallEvaluation')} e
        JOIN    {T('ConversationTurn')} t ON t.InvestigationId = e.InvestigationId
        WHERE   t.ConversationId = :conversation_id
        GROUP BY e.Grader
        ORDER BY e.Grader
        """,
        {"conversation_id": conversation_id},
        max_rows=100,
    )
