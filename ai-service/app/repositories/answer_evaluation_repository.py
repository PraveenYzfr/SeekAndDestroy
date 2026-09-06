"""Verdicts on delivered answers.

Writes are best-effort by design (see :func:`record`): an evaluation is a
comment on work that has already been handed to the user, so failing to store
it must never fail the thing it was commenting on.

Reads are for two questions and nothing else - how has quality moved, and show
me the bad ones. Anything wanting a rate over a window should use the Prometheus
histograms instead; this table is where the individual answer behind a bad rate
is found.
"""

from __future__ import annotations

import json

import structlog

from app.models.entities import AnswerEvaluation
from app.repositories.base import T, execute, fetch_all

logger = structlog.get_logger(__name__)

_COLUMNS = (
    "InvestigationId", "ConversationId", "Question",
    "NumberFidelity", "EntityFidelity", "Completeness", "UngroundedJson",
    # WHY a fidelity score is absent. NULL when the score has a value; see
    # migration 024. A NULL rate becomes a PASS in thresholds.py, so without
    # these an auto-PASS can only be trusted, never read.
    "NumberFidelityAbsence", "EntityFidelityAbsence",
    "GradedCalls", "UngradeableCalls",
    "JudgeProvider", "JudgeModel",
    "JudgeRelevance", "JudgeGroundedness", "JudgeActionability",
    "JudgeConfident", "JudgeSelfJudged", "JudgeJustification", "JudgeError",
    "DurationMs",
)


#: Exported so the writer can build a row of the same shape whatever path it
#: took. A dict whose keys depend on which branch ran is one every reader has to
#: probe with .get(), and the first reader to forget turns a missing verdict into
#: a KeyError somewhere far away from the branch that caused it.
COLUMNS = _COLUMNS


def record(values: dict) -> None:
    """Store one verdict. Never raises.

    The answer this row grades has already been returned to the user. A failure
    here - a missing table on an un-migrated environment, a connection blip -
    means the verdict is lost, and losing a verdict is strictly better than
    turning a completed investigation into an error after the fact. The loss is
    logged at warning so it is visible rather than silent.
    """
    params = {c: values.get(c) for c in _COLUMNS}
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    try:
        execute(
            f"INSERT INTO {T('AnswerEvaluation')} ({columns}) VALUES ({placeholders})", params
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer_evaluation.persist_failed", error=str(exc)[:300])
        # Counted as well as logged. Swallowing the exception is right - this
        # grades an answer already handed to a user - but a log line is not a
        # signal anybody watches, and that is not a hypothetical: this table
        # shipped without an INSERT grant and every write failed silently until
        # somebody queried it by hand.
        try:
            from app.observability.metrics import evaluation_persist_failures_total

            evaluation_persist_failures_total.labels(table="AnswerEvaluation").inc()
        except Exception:  # noqa: BLE001
            pass


def recent(limit: int = 50) -> list[AnswerEvaluation]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('AnswerEvaluation')} ORDER BY AnswerEvaluationId DESC",
        {"limit": int(limit)},
    )
    return [AnswerEvaluation(**row) for row in rows]


def for_investigation(investigation_id: int) -> list[AnswerEvaluation]:
    rows = fetch_all(
        f"SELECT * FROM {T('AnswerEvaluation')} WHERE InvestigationId = :id "
        f"ORDER BY AnswerEvaluationId DESC",
        {"id": int(investigation_id)},
    )
    return [AnswerEvaluation(**row) for row in rows]


def worst(limit: int = 20, *, max_score: int = 3) -> list[AnswerEvaluation]:
    """The answers a judge scored at or below ``max_score`` on any dimension.

    Self-judged verdicts are excluded, for the same reason they are excluded
    from headline scores: a model grading its own work is evidence about the
    grader as much as the answer, and a review queue built from it wastes the
    reviewer's attention.
    """
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('AnswerEvaluation')} "
        f"WHERE ISNULL(JudgeSelfJudged, 0) = 0 AND ("
        f"  JudgeRelevance <= :max_score OR JudgeGroundedness <= :max_score "
        f"  OR JudgeActionability <= :max_score) "
        f"ORDER BY AnswerEvaluationId DESC",
        {"limit": int(limit), "max_score": int(max_score)},
    )
    return [AnswerEvaluation(**row) for row in rows]


def ungrounded_tokens(row: AnswerEvaluation) -> list[str]:
    """The offending figures, decoded. Stored as JSON text so the column stays
    one shape; unparseable content returns empty rather than raising, because a
    malformed audit detail must not break the screen that displays it."""
    if not row.UngroundedJson:
        return []
    try:
        parsed = json.loads(row.UngroundedJson)
    except (ValueError, TypeError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []
