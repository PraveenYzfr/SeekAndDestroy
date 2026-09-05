"""Answers with no investigation behind them are not graded, and not failures.

THE DEFECT
----------
Four kinds of answer leave this platform without an Investigation row: a
greeting, a capability refusal, a recall of a previous shortlist, and an estate
count. None of them narrates evidence, so there is nothing for a groundedness
grader to check narration against.

They were graded anyway. ``evaluate`` had a guard meant to stop exactly that:

    if deterministic["GradedCalls"] == 0 and not judged:
        return None

and it never fired once, because ``_judge`` returns ``{"JudgeError": ...}`` when
evidence cannot be recovered, and a non-empty dict is truthy. So every refusal
wrote an AnswerEvaluation row carrying nothing but an error string, and ticked
``judge_failures_total{reason="no_evidence"}``.

Five of the first twelve rows in that table were correct refusals filed as judge
failures - "hi", "ther e?", "new york one", and two capability refusals. Those
are what ``JudgeNotProducingVerdicts`` alerts on, so the interception working
correctly read as the judge being broken.

WHY THE FIX IS AN EARLY RETURN AND NOT A BETTER TRUTHINESS TEST
---------------------------------------------------------------
Patching ``not judged`` to see through a dict containing only an error would
have suppressed the row and left the counter tick in place - half a fix, and the
half that does not show up in a table nobody reads.

More importantly the test was the wrong instrument. It asks "did anything come
back", when the question is "was there anything to ask about". Those differ
precisely in the case that was broken.

WHAT MUST NOT BE SWEPT UP
-------------------------
An investigation that EXISTS but whose evidence cannot be recovered is a real
judge failure - it means an audit row is missing - and it keeps its
``no_evidence`` label. Row 38 in production was exactly that: ``inv=108``,
``calls=0``, evidence unrecoverable. Blanket-suppressing every "no evidence"
would have hidden a genuine signal to silence a false one.
"""

from __future__ import annotations

import pytest

from app.services import answer_evaluation


def _counter_value(counter, **labels) -> float:
    """Read a prometheus_client Counter child, tolerating absence.

    A labelled Counter emits NO series until .labels(...).inc() has run once,
    so "not present" and "zero" are different states and reading a child into
    existence would itself create the series. Fetched via _metrics rather than
    .labels() for that reason.
    """
    key = tuple(labels[name] for name in counter._labelnames)
    child = counter._metrics.get(key)
    return child._value.get() if child is not None else 0.0


def _answer(investigation_id, *, kind="Conversation", text="Some delivered answer.") -> dict:
    return {
        "investigation_id": investigation_id,
        "investigation_type": kind,
        "final_report": {"executive_summary": text},
    }


class TestAnAnswerWithNoInvestigationIsNotGraded:
    def test_no_row_is_written(self, monkeypatch):
        def explode(*a, **k):
            pytest.fail("an ungradeable answer must not reach the repository")

        from app.repositories import answer_evaluation_repository

        monkeypatch.setattr(answer_evaluation_repository, "record", explode)
        assert answer_evaluation.evaluate(question="hi", result=_answer(None)) is None

    def test_the_judge_is_never_invoked(self, monkeypatch):
        """Not merely "its result is discarded" - it must not be entered at
        all. _judge returns before any provider call today, so the saving is a
        row and a counter tick rather than money; that is worth stating so
        nobody scopes this as a spend reduction and is surprised when the bill
        does not move."""
        monkeypatch.setattr(
            answer_evaluation, "_judge",
            lambda *a, **k: pytest.fail("judge invoked for an answer with nothing to grade"),
        )
        assert answer_evaluation.evaluate(question="hi", result=_answer(None)) is None

    def test_deterministic_grading_is_not_invoked_either(self, monkeypatch):
        monkeypatch.setattr(
            answer_evaluation, "_grade_deterministic",
            lambda *a, **k: pytest.fail("deterministic grading invoked with no investigation"),
        )
        assert answer_evaluation.evaluate(question="hi", result=_answer(None)) is None


class TestItIsCountedAsNotApplicableNotAsAFailure:
    def test_judge_failures_is_not_incremented(self):
        """The whole point. A correct refusal must not read as a broken judge,
        because that counter is what JudgeNotProducingVerdicts alerts on."""
        from app.observability.metrics import judge_failures_total

        before = _counter_value(judge_failures_total, reason="no_evidence")
        answer_evaluation.evaluate(question="hi", result=_answer(None))
        assert _counter_value(judge_failures_total, reason="no_evidence") == before

    def test_not_applicable_is_incremented_instead(self):
        """Suppressing the row without counting anything would make the
        interception invisible. A rise here is real signal - more of what the
        platform says is being answered before it investigates."""
        from app.observability.metrics import judge_not_applicable_total

        before = _counter_value(judge_not_applicable_total, kind="Count")
        answer_evaluation.evaluate(
            question="how many servers do we have",
            result=_answer(None, kind="Count", text="10,943 servers."),
        )
        assert _counter_value(judge_not_applicable_total, kind="Count") == before + 1

    @pytest.mark.parametrize("kind", ["Conversation", "Count"])
    def test_the_kind_is_kept_so_the_two_are_distinguishable(self, kind):
        """A greeting and an estate count are both ungraded for the same
        structural reason and are not the same event. Counting them under one
        label would make a surge in counting questions look like a surge in
        people typing "hi"."""
        from app.observability.metrics import judge_not_applicable_total

        before = _counter_value(judge_not_applicable_total, kind=kind)
        answer_evaluation.evaluate(question="q", result=_answer(None, kind=kind))
        assert _counter_value(judge_not_applicable_total, kind=kind) == before + 1


class TestARealInvestigationIsStillGraded:
    def test_missing_evidence_on_a_real_investigation_stays_a_judge_failure(self, monkeypatch):
        """A real investigation with unrecoverable evidence is still a failure.
        Suppressing every "no evidence" would have hidden it to silence a false
        alarm - that part of this test was right and has not changed.

        THE EXPLANATION HAS. This said "calls=0 means an audit row went
        missing", and production disproved it. The first real firing was
        investigation 124, "which 3 clusters are the best right-sizing
        candidates": ONE audit row, the final report, with no grounded answer
        and no narration behind it. No investigation in that window had zero
        audit rows, so nothing was lost.

        calls=0 does not mean the evidence went missing. It means THE PIPELINE
        NEVER GATHERED ANY - a report written about a selection that was never
        made. That is a defect in the graph, not in the database, and the label
        now says which.
        """
        from app.observability.metrics import judge_failures_total

        monkeypatch.setattr(
            answer_evaluation, "_evidence_and_author_for",
            lambda _id: (None, {}, "no_evidence_gathered"),
        )
        before = _counter_value(judge_failures_total, reason="no_evidence_gathered")
        verdict = answer_evaluation._judge("q", "some prose", 108)

        assert "JudgeError" in verdict
        assert _counter_value(judge_failures_total, reason="no_evidence_gathered") == before + 1

    def test_a_graded_answer_still_reaches_the_repository(self, monkeypatch):
        written: dict = {}
        from app.repositories import answer_evaluation_repository

        monkeypatch.setattr(answer_evaluation_repository, "record", lambda v: written.update(v))
        monkeypatch.setattr(
            answer_evaluation, "_grade_deterministic",
            lambda _id: {"GradedCalls": 2, "UngradeableCalls": 0, "NumberFidelity": 1.0,
                         "EntityFidelity": None, "Completeness": None, "UngroundedJson": None},
        )
        monkeypatch.setattr(answer_evaluation, "_should_judge", lambda: False)

        result = answer_evaluation.evaluate(question="why?", result=_answer(42))
        assert result is not None
        assert written.get("InvestigationId") == 42


class TestTheOldGuardStillCoversWhatItWasFor:
    def test_a_real_investigation_with_nothing_measurable_writes_no_row(self, monkeypatch):
        """The original guard is not dead code - it still catches an
        investigation that produced no gradeable calls and no verdict. Only the
        no-investigation case was taken away from it."""
        monkeypatch.setattr(
            answer_evaluation, "_grade_deterministic",
            lambda _id: {"GradedCalls": 0, "UngradeableCalls": 0, "NumberFidelity": None,
                         "EntityFidelity": None, "Completeness": None, "UngroundedJson": None},
        )
        monkeypatch.setattr(answer_evaluation, "_should_judge", lambda: False)
        assert answer_evaluation.evaluate(question="why?", result=_answer(42)) is None

    def test_an_empty_answer_is_still_refused_before_anything_else(self):
        """No prose means nothing was delivered to grade, checked before the
        investigation id is even looked at."""
        assert answer_evaluation.evaluate(question="q", result={"investigation_id": 42}) is None
