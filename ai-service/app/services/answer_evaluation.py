"""Grade every answer this platform delivers, and keep the verdict.

WHY THIS EXISTS
---------------
All three quality checks already worked and none of them ran on a real answer.

    assert_no_number_drift   ran live, on every narration, and wrote a log line
    graders.number_fidelity  ran offline, over the audit table, on demand
    evaluation.judge         ran only from the golden-set runner

So the platform could prove its arithmetic was sound on a fixed set of test
questions and could say nothing at all about the report an engineer was given
this morning. This module closes that: every final answer is graded, the verdict
is stored, and the rates reach Prometheus.

WHY IT RUNS AFTER THE ANSWER, NOT BEFORE
-----------------------------------------
Evaluation is deliberately NOT in the request path.

The deterministic graders are pure arithmetic over rows that already exist -
microseconds - but the judge is another LLM call, and p95 for an investigation
is already ~98s. Adding a synchronous judge would make every user wait for a
verdict none of them asked for, to produce a score only an operator reads.

Worse, it would couple correctness to availability: a judge provider having a
bad afternoon would slow, and on timeout fail, investigations that were
themselves perfectly fine. A grader must not be able to break the thing it
grades.

So the answer is returned first and graded on a worker thread afterwards. The
cost is that a verdict lands a few seconds after the answer - which matters to
nobody, because nothing blocks on it.

SAMPLING, AND WHAT IT COSTS
---------------------------
Deterministic grading is ALWAYS run. It is free and it is the half that catches
a fabricated number, so sampling it would be saving nothing at the price of the
signal that matters most.

The judge is sampled, defaulting to every answer (rate 1.0) because that is what
was asked for. The dial exists so the trade-off is visible and adjustable rather
than hard-coded: one extra model call per investigation, on the judge role,
which can be pointed at a cheap model independently of every other role.

Setting the rate below 1.0 makes the score a SAMPLE. That is a legitimate
choice, and this module records GradedCalls so a rate computed from the table
knows what it was computed over.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Prose is joined from the report and capped before it reaches a judge prompt.
#: A report is bounded, but a caller could hand this anything, and an unbounded
#: string becomes an unbounded prompt - the one part of this module that could
#: cost real money if it went wrong.
_MAX_PROSE_CHARS = 12_000
_MAX_QUESTION_CHARS = 2_000
_MAX_UNGROUNDED = 12

#: Report fields that are prose. Deliberately a list rather than "every string
#: in the payload": ids, cluster codes and status values are strings too, and
#: asking a judge whether "cmh-p225" is actionable is not a question.
_PROSE_FIELDS = (
    "executive_summary", "summary", "recommendation", "reasoning", "rationale",
    "risks", "next_steps", "human_action_required", "answer", "narrative",
)


def evaluate_async(
    *,
    question: str,
    result: dict,
    conversation_id: str | None = None,
) -> None:
    """Grade ``result`` on a worker thread. Returns immediately.

    Daemon thread rather than a task queue: this is a single fire-and-forget
    call with no ordering requirement and no retry semantics worth having - a
    lost verdict is a lost verdict, and standing up a broker to guarantee
    delivery of a comment on finished work would be infrastructure nobody needs.
    Daemon so a shutdown is never held open by a grader mid-flight.
    """
    if not _enabled():
        return
    try:
        thread = threading.Thread(
            target=_safe_evaluate,
            kwargs={"question": question, "result": result, "conversation_id": conversation_id},
            name="answer-eval",
            daemon=True,
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001
        # Thread creation itself failing is exhaustion, not a bug here. Counted
        # and dropped: the answer is already out the door.
        logger.warning("answer_evaluation.spawn_failed", error=str(exc)[:200])


def _safe_evaluate(**kwargs: Any) -> None:
    try:
        evaluate(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer_evaluation.failed", error=str(exc)[:300], exc_info=False)


def _count_not_applicable(kind: str) -> None:
    from app.observability.metrics import judge_not_applicable_total

    judge_not_applicable_total.labels(kind=kind).inc()


def evaluate(
    *,
    question: str,
    result: dict,
    conversation_id: str | None = None,
) -> dict | None:
    """Grade one delivered answer and store the verdict.

    Returns the stored row's values, or None when there was nothing gradeable -
    a greeting has no evidence and no figures, and inventing a perfect score for
    it would inflate every average this table feeds.
    """
    started = time.perf_counter()
    investigation_id = result.get("investigation_id")
    prose = _prose_from(result.get("final_report") or result.get("rejection_prompt") or {})
    if not prose:
        return None

    # NO INVESTIGATION MEANS NOTHING TO GRADE, AND THAT IS NOT A JUDGE FAILURE.
    #
    # Four answers leave this platform without an investigation behind them: a
    # greeting, a capability refusal, a recall of a previous shortlist, and an
    # estate count. None of them narrates evidence, so there is no evidence to
    # check narration against - the question this table exists to answer does
    # not apply.
    #
    # It used to be graded anyway. The guard below was written to stop that:
    #
    #     if deterministic["GradedCalls"] == 0 and not judged: return None
    #
    # and it never fired, because _judge returns {"JudgeError": ...} on
    # unrecoverable evidence and a non-empty dict is truthy. So every refusal
    # wrote a row carrying nothing but an error, and ticked
    # judge_failures_total{reason="no_evidence"}. Five of the first twelve rows
    # in this table were correct refusals filed as judge failures, and they are
    # what JudgeNotProducingVerdicts alerts on - the interception working
    # reported as the judge broken.
    #
    # Returning here rather than patching the truthiness test, because the test
    # was the wrong instrument: it asks "did anything come back" when the
    # question is "was there anything to ask about". Patching it would have
    # suppressed the row and left the counter tick in place.
    #
    # NOT the same as an investigation whose evidence cannot be recovered -
    # that IS a judge failure, it keeps its no_evidence label, and it is a real
    # signal that an audit row went missing. Conflating the two is what made
    # the alert unreadable.
    if investigation_id is None:
        _count_not_applicable(str(result.get("investigation_type") or "Conversation"))
        return None

    deterministic = _grade_deterministic(investigation_id)
    judged = _judge(question, prose, investigation_id) if _should_judge() else {}

    if deterministic["GradedCalls"] == 0 and not judged:
        # Nothing measurable and no verdict. Writing a row of nulls would make
        # the table look busier than the evaluation actually was.
        return None

    from app.repositories import answer_evaluation_repository

    # Every column, every path. The judge half returns a partial dict - a
    # failure carries only an error, a success carries only scores - and merging
    # those straight in produced a row whose KEYS depended on which branch ran.
    # Every reader then has to probe with .get(), and the first one to forget
    # raises a KeyError far away from the branch that caused it. Filling the
    # full column set with None here means "no verdict" is a value rather than
    # an absent key, which is the same distinction the table itself makes.
    values: dict[str, Any] = {column: None for column in answer_evaluation_repository.COLUMNS}
    values.update({
        "InvestigationId": investigation_id,
        "ConversationId": conversation_id,
        "Question": (question or "")[:_MAX_QUESTION_CHARS] or None,
        "DurationMs": int((time.perf_counter() - started) * 1000),
        **deterministic,
        **judged,
    })

    answer_evaluation_repository.record(values)
    _observe(values)
    return values


# ---------------------------------------------------------------------------
# Deterministic half - arithmetic over evidence that already exists
# ---------------------------------------------------------------------------


def _grade_deterministic(investigation_id: int | None) -> dict:
    """Fidelity over every model call this investigation made.

    Grades the AUDIT ROWS rather than the assembled report, and that choice is
    load-bearing. Fidelity is only meaningful against the evidence a specific
    sentence was written from, and the audit row is the only place that pairing
    survives - the finished report has been through several nodes by the time it
    is returned and no longer carries the evidence any one of its sentences came
    from. Grading the report against a merged bag of evidence would ground
    figures that were never available to the model that wrote them.
    """
    empty = {
        "NumberFidelity": None, "EntityFidelity": None, "Completeness": None,
        "UngroundedJson": None, "GradedCalls": 0, "UngradeableCalls": 0,
    }
    if not investigation_id:
        return empty

    from app.evaluation.graders import grade_call, was_truncated
    from app.repositories.base import T, fetch_all

    try:
        rows = fetch_all(
            f"SELECT AuditId, GraphNode, ToolName, InputJson, OutputJson "
            f"FROM {T('AgentAuditLog')} WHERE InvestigationId = :id AND OutputJson IS NOT NULL",
            {"id": int(investigation_id)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer_evaluation.audit_read_failed", error=str(exc)[:200])
        return empty

    totals: dict[str, list[int]] = {}
    ungrounded: list[str] = []
    graded = 0
    ungradeable = 0

    for row in rows:
        if was_truncated(row.get("InputJson")):
            ungradeable += 1
            continue
        grades = grade_call(row.get("InputJson"), row.get("OutputJson"), row.get("ToolName") or "")
        if not grades:
            ungradeable += 1
            continue
        graded += 1
        for grade in grades:
            bucket = totals.setdefault(grade.name, [0, 0])
            bucket[0] += grade.grounded
            bucket[1] += grade.total
            ungrounded.extend(str(u) for u in grade.ungrounded)

    def rate(name: str) -> float | None:
        grounded, total = totals.get(name, [0, 0])
        return None if total == 0 else round(grounded / total, 4)

    return {
        "NumberFidelity": rate("number_fidelity"),
        "EntityFidelity": rate("entity_fidelity"),
        # Measurable without evidence, and kept for exactly that reason: it is
        # the only deterministic score still available when a prompt was
        # truncated, so a call with unrecoverable evidence is not left with no
        # objective score at all.
        "Completeness": rate("completeness"),
        "UngroundedJson": json.dumps(ungrounded[:_MAX_UNGROUNDED]) if ungrounded else None,
        "GradedCalls": graded,
        "UngradeableCalls": ungradeable,
    }


# ---------------------------------------------------------------------------
# Judge half - an opinion, labelled as one
# ---------------------------------------------------------------------------


def _judge(question: str, prose: str, investigation_id: int | None) -> dict:
    """Run the LLM judge over the delivered prose.

    The evidence handed to the judge is the evidence recorded on the report
    call, recovered from the audit row - NOT the report itself. Giving a judge
    the answer as its own evidence asks whether the answer agrees with itself,
    which every answer does.
    """
    from app.evaluation.judge import judge_answer

    evidence, author = _evidence_and_author_for(investigation_id)
    if evidence is None:
        # No recoverable evidence means groundedness is unanswerable, and a
        # judge asked anyway will answer confidently from nothing. Recorded as
        # a failure with a reason rather than as a low score.
        _count_failure("no_evidence")
        return {"JudgeError": "evidence for the answer could not be recovered"}

    try:
        # The author is passed, and until now it was NOT - judge_answer was
        # called with the evidence alone. Its contract says that means
        # self-judging "cannot be determined; it is then reported as False", so
        # every verdict in production came back self_judged=False whatever model
        # wrote the answer.
        #
        # Two consequences, and the second is the bad one. The disclosure this
        # platform makes a point of - that a model grading its own work grades it
        # high - was never made on a real answer. And the exclusion built to act
        # on it never fired, so same-model verdicts were exported as though they
        # were independent.
        #
        # Read from the AUDIT ROW rather than by re-resolving the role, because
        # the role can be repointed between an answer being written and this
        # grading it. The audit row records the model that actually answered.
        verdict = judge_answer(
            question, prose, evidence,
            author_provider=author.get("provider"),
            author_model=author.get("model"),
        )
    except Exception as exc:  # noqa: BLE001 - judge_answer already swallows, this is belt and braces
        _count_failure("exception")
        return {"JudgeError": str(exc)[:500]}

    base = {"JudgeProvider": verdict.judge_provider, "JudgeModel": verdict.judge_model}
    if verdict.verdict is None:
        _count_failure("no_verdict")
        return {**base, "JudgeError": (verdict.error or "judge returned no verdict")[:500]}

    v = verdict.verdict
    return {
        **base,
        "JudgeRelevance": v.relevance.score,
        "JudgeGroundedness": v.groundedness.score,
        "JudgeActionability": v.actionability.score,
        "JudgeConfident": bool(v.confident),
        "JudgeSelfJudged": bool(verdict.self_judged),
        "JudgeJustification": json.dumps({
            "relevance": v.relevance.justification,
            "groundedness": v.groundedness.justification,
            "actionability": v.actionability.justification,
            "overall": v.overall_comment,
        })[:4000],
    }


def _evidence_and_author_for(investigation_id: int | None) -> tuple[Any | None, dict]:
    """The evidence the final narration was given, and WHO wrote it.

    Both come from the same audit row on purpose. The evidence is what the
    answer must be grounded in; the author is the model that was given that
    evidence. Taking them from different places would let the judge be compared
    against a model that never saw this evidence, which is how self-judging goes
    undetected.

    The author is recorded rather than re-derived from the role. resolve_role
    answers "what would run now", and roles get repointed - so grading a report
    written an hour ago against the CURRENT reporting model would compare the
    judge to a model that did not write it.
    """
    if not investigation_id:
        return None, {}
    from app.evaluation.graders import evidence_from_prompt
    from app.repositories.base import T, fetch_all

    try:
        rows = fetch_all(
            f"SELECT TOP (12) InputJson, Provider, ModelIdentity FROM {T('AgentAuditLog')} "
            f"WHERE InvestigationId = :id AND InputJson IS NOT NULL "
            f"ORDER BY AuditId DESC",
            {"id": int(investigation_id)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer_evaluation.evidence_read_failed", error=str(exc)[:200])
        return None, {}

    for row in rows:
        evidence = evidence_from_prompt(row.get("InputJson"))
        if evidence is not None:
            return evidence, {
                "provider": row.get("Provider"),
                "model": row.get("ModelIdentity"),
            }
    return None, {}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _observe(values: dict) -> None:
    """Push the verdict to Prometheus. Never raises.

    Both halves are exported under separate metrics on purpose. A single
    "quality score" averaging arithmetic with opinion cannot be alerted on: a
    drop could be a fabricated figure or a model being less chatty, and only one
    of those is an incident.
    """
    try:
        from app.observability.metrics import fidelity_score, judge_score

        for column, grader in (
            ("NumberFidelity", "number_fidelity"),
            ("EntityFidelity", "entity_fidelity"),
            ("Completeness", "completeness"),
        ):
            score = values.get(column)
            if score is not None:
                fidelity_score.labels(grader=grader).observe(float(score))

        # A self-judged verdict is stored but not exported: a model grades its
        # own work high, and averaging that with independent verdicts produces a
        # line nobody can read.
        #
        # It IS counted, though. Excluding silently was a mistake - every role
        # defaults to the same model, so in a default configuration the judge is
        # always the author, every verdict is disqualified, and the panels sat
        # empty looking exactly like a judge that had never been wired up. "No
        # data" and "47 verdicts, all disqualified" need different responses.
        self_judged = bool(values.get("JudgeSelfJudged"))
        scored = False
        for column, dimension in (
            ("JudgeRelevance", "relevance"),
            ("JudgeGroundedness", "groundedness"),
            ("JudgeActionability", "actionability"),
        ):
            score = values.get(column)
            if score is None:
                continue
            scored = True
            if not self_judged:
                judge_score.labels(dimension=dimension).observe(float(score))
        if scored and self_judged:
            from app.observability.metrics import judge_excluded_total

            judge_excluded_total.labels(reason="self_judged").inc()
    except Exception:  # noqa: BLE001
        pass


def _count_failure(reason: str) -> None:
    try:
        from app.observability.metrics import judge_failures_total

        judge_failures_total.labels(reason=reason).inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _prose_from(report: Any) -> str:
    """The narrative parts of a report, joined.

    Only the fields named in _PROSE_FIELDS. A judge handed the whole payload
    would be scoring identifiers and status enums as if they were writing.
    """
    if isinstance(report, str):
        return report[:_MAX_PROSE_CHARS]
    if not isinstance(report, dict):
        return ""
    parts: list[str] = []
    for field in _PROSE_FIELDS:
        value = report.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(v).strip() for v in value if str(v).strip())
    return "\n\n".join(parts)[:_MAX_PROSE_CHARS]


def _enabled() -> bool:
    from app.config.settings import get_settings

    return bool(getattr(get_settings().llm, "evaluate_answers", True))


def _should_judge() -> bool:
    """Whether THIS answer gets a judge call.

    Rate 1.0 short-circuits without touching the RNG, so the default path -
    judge everything, which is what was asked for - has no randomness in it at
    all and cannot be made non-deterministic by a seed somewhere else.
    """
    from app.config.settings import get_settings

    rate = float(getattr(get_settings().llm, "judge_sample_rate", 1.0))
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate
