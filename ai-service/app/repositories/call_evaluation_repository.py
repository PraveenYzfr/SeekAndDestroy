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
    """EVERY call in one investigation, with its grade when it has one.

    Driven from the audit log, not from the grades. That direction is the whole
    fix: this used to read

        FROM CallEvaluation e JOIN AgentAuditLog a ON a.AuditId = e.AuditId

    which iterates GRADES and attaches the prompt, so a call that was never
    graded could not appear at all. Grading is per narration call, so most calls
    are not graded - and the screen built to show an engineer what the model was
    sent showed nothing for exactly the conversations worth reading.

    Measured before the change: investigation 131 had two audit rows and
    get_transcript returned zero calls. The conversation that drifted onto
    another cluster's incidents was the one it could not display.

    A LEFT JOIN, so an ungraded call arrives with Grader NULL rather than being
    dropped. The caller must treat that as "not graded", which is not the same
    as a score of zero - the distinction this codebase keeps everywhere else.
    """
    return fetch_all(
        f"""
        SELECT  e.CallEvaluationId, e.Grader, e.Grounded, e.Total,
                e.Rate, e.UngroundedJson, e.GraderVersion, e.CreatedAt,
                a.AuditId, a.GraphNode, a.ToolName, a.ModelIdentity, a.Provider,
                a.StartedAt, a.CompletedAt, a.Success,
                a.InputJson, a.OutputJson,
                a.PromptTokens, a.CompletionTokens, a.CostUsd, a.LatencyMs
        FROM    {T('AgentAuditLog')} a
        LEFT JOIN {T('CallEvaluation')} e
               ON e.AuditId = a.AuditId AND e.InvestigationId = a.InvestigationId
        WHERE   a.InvestigationId = :investigation_id
        ORDER BY a.AuditId, e.Grader
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


def rollup_by_investigation(conversation_id: str) -> list[dict]:
    """Per-grader sums for each investigation in a conversation.

    The TURN level. A conversation is a sequence of exchanges and each one got
    its own answer, so a single conversation-wide figure hides which exchange was
    the bad one - which is the thing a reader is looking for when they open this
    at all.

    Grouped by investigation because that is what a turn maps to: one user
    message, one pipeline run, one answer handed back.
    """
    return fetch_all(
        f"""
        SELECT  e.InvestigationId,
                e.Grader,
                SUM(e.Grounded) AS Grounded,
                SUM(e.Total)    AS Total,
                COUNT(*)        AS Calls
        FROM    {T('CallEvaluation')} e
        WHERE   e.InvestigationId IN (
                    SELECT DISTINCT t.InvestigationId
                    FROM {T('ConversationTurn')} t
                    WHERE t.ConversationId = :conversation_id
                      AND t.InvestigationId IS NOT NULL
                )
        GROUP BY e.InvestigationId, e.Grader
        ORDER BY e.InvestigationId, e.Grader
        """,
        {"conversation_id": conversation_id},
        max_rows=1000,
    )


def turns_for_conversation(conversation_id: str) -> list[dict]:
    """The exchange itself: what was asked, what came back, and which run it was.

    ConversationTurn stores a one-line assistant summary rather than the full
    report - history exists to resolve references, not to re-read reports, and
    the report stays on the Investigation row. So this returns the summary and
    the investigation id, and a caller wanting the full text follows the latter.
    """
    return fetch_all(
        f"""
        SELECT  t.TurnId, t.Role, t.Message, t.InvestigationId, t.CreatedAt
        FROM    {T('ConversationTurn')} t
        WHERE   t.ConversationId = :conversation_id
        ORDER BY t.TurnId
        """,
        {"conversation_id": conversation_id},
        max_rows=500,
    )


def recent_conversations(limit: int = 50) -> list[dict]:
    """Conversations to choose from, worst score first.

    Ordered by number_fidelity ascending rather than by recency, because the
    reason to open this screen is to find a bad answer. A list sorted by time
    puts the most recent conversation first whether or not anything is wrong with
    it, and the one worth reading is then somewhere below the fold.

    Conversations with no stored verdicts sort last rather than first: an
    ungraded conversation has no score, and NULL is not zero.
    """
    return fetch_all(
        f"""
        SELECT  c.ConversationId,
                c.StartedAt,
                c.LastActivityAt,
                (SELECT COUNT(*) FROM {T('ConversationTurn')} t
                  WHERE t.ConversationId = c.ConversationId)              AS Turns,
                nf.Grounded                                               AS NumberGrounded,
                nf.Total                                                  AS NumberTotal
        FROM    {T('Conversation')} c
        OUTER APPLY (
            SELECT  SUM(e.Grounded) AS Grounded, SUM(e.Total) AS Total
            FROM    {T('CallEvaluation')} e
            JOIN    {T('ConversationTurn')} t2 ON t2.InvestigationId = e.InvestigationId
            WHERE   t2.ConversationId = c.ConversationId
              AND   e.Grader = 'number_fidelity'
        ) nf
        ORDER BY
            CASE WHEN nf.Total IS NULL OR nf.Total = 0 THEN 1 ELSE 0 END,
            CASE WHEN nf.Total > 0 THEN CAST(nf.Grounded AS FLOAT) / nf.Total END ASC,
            c.LastActivityAt DESC
        OFFSET 0 ROWS FETCH NEXT :limit ROWS ONLY
        """,
        {"limit": limit},
        max_rows=200,
    )
