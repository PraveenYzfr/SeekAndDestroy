"""Human ratings of delivered answers - the only ground truth here.

Every other quality signal in this platform is machine-generated: fidelity is
arithmetic, completeness is field presence, the judge is one model's opinion of
another's work. This is the one place a person says whether the answer actually
helped, and it is therefore the only thing that can tell you whether the machine
signals are worth trusting.

WRITES RAISE. Unlike answer_evaluation_repository, which swallows failures
because it comments on work already delivered, a rating is a deliberate act by a
person. Silently dropping it means they clicked, saw nothing happen, and stopped
bothering - and the data that would have told us the judge is wrong never
arrives.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.repositories.base import T, execute, fetch_all, fetch_one

logger = structlog.get_logger(__name__)

#: The same vocabulary the remediation triage uses, so a human verdict and a
#: machine verdict are directly comparable rather than living in separate
#: languages. Enforced by CK_AnswerFeedback_Reason as well - this copy exists to
#: reject a bad value with a useful message instead of a constraint violation.
REASONS = (
    "wrong_numbers", "wrong_entity", "missing_evidence",
    "did_not_answer", "not_actionable", "too_slow", "other",
)


def record(*, employee_id: int, rating: int, investigation_id: int | None = None,
           conversation_id: str | None = None, reason: str | None = None,
           comment: str | None = None) -> None:
    """Store or update one person's rating of one answer.

    Upsert rather than insert: a person may change their mind, and forcing them
    to live with a first impression makes the second one - usually the better
    informed one - unrecordable.

    The machine's own verdict on the same answer is copied in at rating time.
    Denormalised deliberately: graders get fixed and judges get repointed, and a
    human-vs-machine comparison is meaningless if the machine half changes
    afterwards. This records what the machine said WHEN the person disagreed.
    """
    if rating not in (-1, 0, 1):
        raise ValueError(f"rating must be -1, 0 or 1, not {rating!r}")
    if reason is not None and reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}; expected one of {REASONS}")

    machine = _machine_verdict(investigation_id)
    params = {
        "inv": investigation_id, "conv": conversation_id, "emp": int(employee_id),
        "rating": int(rating), "reason": reason, "comment": (comment or "")[:2000] or None,
        "judge": machine.get("judge_min"), "nf": machine.get("number_fidelity"),
    }
    # An answer with no investigation id has no identity to upsert ON - NULL =
    # NULL is false - so it is a plain insert, and the filtered unique index
    # deliberately does not constrain those rows. Trying to MERGE them produced
    # a constraint violation from a statement whose whole purpose was to avoid
    # one.
    if investigation_id is None:
        execute(
            f"INSERT INTO {T('AnswerFeedback')} "
            f"(InvestigationId, ConversationId, EmployeeId, Rating, Reason, Comment, "
            f" JudgeMinScore, NumberFidelity) "
            f"VALUES (:inv, :conv, :emp, :rating, :reason, :comment, :judge, :nf)",
            params,
        )
        return

    # MERGE rather than "try insert, catch, update": two people rating the same
    # answer at the same moment would race the catch, and the loser's rating
    # would be lost with no error anyone sees.
    execute(
        f"MERGE {T('AnswerFeedback')} AS target "
        f"USING (SELECT :inv AS InvestigationId, :emp AS EmployeeId) AS src "
        f"ON target.InvestigationId = src.InvestigationId "
        f"   AND target.EmployeeId = src.EmployeeId "
        f"WHEN MATCHED THEN UPDATE SET Rating = :rating, Reason = :reason, "
        f"   Comment = :comment, UpdatedAt = SYSUTCDATETIME() "
        f"WHEN NOT MATCHED THEN INSERT "
        f"   (InvestigationId, ConversationId, EmployeeId, Rating, Reason, Comment, "
        f"    JudgeMinScore, NumberFidelity) "
        f"   VALUES (:inv, :conv, :emp, :rating, :reason, :comment, :judge, :nf);",
        params,
    )


def _machine_verdict(investigation_id: int | None) -> dict:
    """What the platform thought of this answer, for the comparison.

    Missing is fine and common - the evaluation runs on a worker thread after
    the answer is returned, so a fast rater can beat it. Recorded as NULL rather
    than as a zero, because "the machine had not scored this yet" and "the
    machine scored it badly" are different facts.
    """
    if not investigation_id:
        return {}
    try:
        rows = fetch_all(
            f"SELECT TOP (1) JudgeRelevance, JudgeGroundedness, JudgeActionability, "
            f"NumberFidelity FROM {T('AnswerEvaluation')} "
            f"WHERE InvestigationId = :id ORDER BY AnswerEvaluationId DESC",
            {"id": int(investigation_id)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer_feedback.machine_verdict_unavailable", error=str(exc)[:200])
        return {}
    if not rows:
        return {}
    row = rows[0]
    scores = [row.get(k) for k in
              ("JudgeRelevance", "JudgeGroundedness", "JudgeActionability")]
    present = [s for s in scores if s is not None]
    # The MINIMUM, matching thresholds.judge_outcome. A mean would hide the
    # dimension that failed, which is the whole reason the minimum is used there.
    return {"judge_min": min(present) if present else None,
            "number_fidelity": row.get("NumberFidelity")}


def for_investigation(investigation_id: int) -> list[dict]:
    return fetch_all(
        f"SELECT * FROM {T('AnswerFeedback')} WHERE InvestigationId = :id "
        f"ORDER BY AnswerFeedbackId DESC",
        {"id": int(investigation_id)},
    )


def mine(investigation_id: int, employee_id: int) -> dict | None:
    """This person's own rating, so the control renders in the state they left."""
    return fetch_one(
        f"SELECT * FROM {T('AnswerFeedback')} "
        f"WHERE InvestigationId = :id AND EmployeeId = :emp",
        {"id": int(investigation_id), "emp": int(employee_id)},
    )


def recent(limit: int = 50, *, rating: int | None = None) -> list[dict]:
    where = "WHERE Rating = :rating " if rating is not None else ""
    params: dict[str, Any] = {"limit": int(limit)}
    if rating is not None:
        params["rating"] = int(rating)
    return fetch_all(
        f"SELECT TOP (:limit) * FROM {T('AnswerFeedback')} {where}"
        f"ORDER BY AnswerFeedbackId DESC", params
    )


def agreement() -> dict:
    """Where the humans and the judge disagree - the reason this table exists.

    Four buckets. The off-diagonal ones are the interesting half:

        both_positive    the judge liked it and so did the person
        both_negative    both disliked it - the judge is earning its cost
        judge_missed     PERSON UNHAPPY, JUDGE HAPPY. The judge is passing
                         answers people cannot use, which is the failure mode a
                         judge is supposed to catch and the one that makes it
                         worthless
        judge_harsh      person happy, judge unhappy. Cheaper to be wrong about,
                         but it fills the remediation queue with work nobody
                         needed

    Rows where the machine never scored the answer are excluded rather than
    counted as agreement - an unscored answer is not evidence about the judge.
    """
    rows = fetch_all(
        f"SELECT Rating, JudgeMinScore FROM {T('AnswerFeedback')} "
        f"WHERE JudgeMinScore IS NOT NULL"
    )
    buckets = {"both_positive": 0, "both_negative": 0, "judge_missed": 0, "judge_harsh": 0}
    for row in rows:
        # 4 and 5 are the pass band in thresholds.py; anything lower is the
        # judge expressing a reservation.
        judge_ok = (row["JudgeMinScore"] or 0) >= 4
        human_ok = (row["Rating"] or 0) > 0
        if human_ok and judge_ok:
            buckets["both_positive"] += 1
        elif not human_ok and not judge_ok:
            buckets["both_negative"] += 1
        elif not human_ok and judge_ok:
            buckets["judge_missed"] += 1
        else:
            buckets["judge_harsh"] += 1
    buckets["total_compared"] = len(rows)
    return buckets
