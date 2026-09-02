"""The gate's numbers, and the ways a gate quietly stops meaning anything.

Every threshold here was set deliberately. These tests exist so that changing
one is a decision somebody makes on purpose rather than a side effect of
refactoring, and so the two ways a gate goes soft are caught:

  * a FAIL that stops blocking
  * an unmeasurable check scored as a failure, which makes a broken instrument
    read as broken output and trains people to widen the gate
"""

from __future__ import annotations

import pytest

from app.evaluation.thresholds import Outcome, combine, grade_outcome, judge_outcome


class TestFabricationHasNoAcceptableRate:
    @pytest.mark.parametrize("grader", ["number_fidelity", "entity_fidelity"])
    @pytest.mark.parametrize("rate", [0.99, 0.95, 0.8, 0.5, 0.0])
    def test_anything_below_100_fails(self, grader, rate):
        """One invented cluster in twenty still sends somebody to a data centre
        that was never a candidate. Rounding is already forgiven inside the
        grader, so anything reaching 'ungrounded' has failed a tolerance check
        already - a warn band here would excuse it twice."""
        assert grade_outcome(grader, rate).outcome is Outcome.FAIL

    @pytest.mark.parametrize("grader", ["number_fidelity", "entity_fidelity"])
    def test_exactly_100_passes(self, grader):
        assert grade_outcome(grader, 1.0).outcome is Outcome.PASS


class TestCompletenessBands:
    @pytest.mark.parametrize("rate, expected", [
        (1.00, Outcome.PASS),
        (0.96, Outcome.PASS),
        (0.95, Outcome.WARN),   # ">95" is strict - 95 itself warns
        (0.90, Outcome.WARN),
        (0.85, Outcome.WARN),
        (0.84, Outcome.FAIL),
        (0.00, Outcome.FAIL),
    ])
    def test_bands(self, rate, expected):
        assert grade_outcome("completeness", rate).outcome is expected


class TestNotMeasuredIsNotZero:
    """The distinction the whole evaluation layer rests on.

    A truncated prompt, unrecoverable evidence, a property that did not apply -
    none of those are evidence that the answer was bad. Scoring them as failures
    is how a broken instrument reads as broken output, and the response to that
    is always to widen the gate rather than fix the instrument.
    """

    @pytest.mark.parametrize("grader", ["number_fidelity", "entity_fidelity", "completeness"])
    def test_none_does_not_fail(self, grader):
        assert grade_outcome(grader, None).outcome is Outcome.PASS
        assert "not measured" in grade_outcome(grader, None).reason

    def test_a_judge_with_no_verdict_does_not_fail(self):
        assert judge_outcome(None, None, None).outcome is Outcome.PASS


class TestJudgeUsesTheWorstDimension:
    def test_a_mean_of_four_can_still_fail(self):
        """5, 5, 2 averages to 4.0 - the same as 4, 4, 4 - and one of them has a
        groundedness failure hiding behind two good scores."""
        assert judge_outcome(5, 2, 5).outcome is Outcome.FAIL
        assert judge_outcome(4, 4, 4).outcome is Outcome.PASS

    def test_groundedness_two_fails(self):
        """'A reader could be misled' is the rubric's own words for 2."""
        assert judge_outcome(5, 2, 5).outcome is Outcome.FAIL

    def test_relevance_two_fails(self):
        """Praveen's java answer: every deterministic check green, and it never
        addressed the question."""
        assert judge_outcome(2, 5, 5).outcome is Outcome.FAIL

    def test_actionability_is_looser_by_one_notch(self):
        """A correct, grounded, slightly terse answer is a style complaint.
        Gating on it fills the retry queue with noise."""
        assert judge_outcome(5, 5, 2).outcome is Outcome.WARN
        assert judge_outcome(5, 5, 3).outcome is Outcome.PASS
        # but the same 3 on groundedness is only a warning, not a pass
        assert judge_outcome(5, 3, 5).outcome is Outcome.WARN


class TestTheTwoByTwo:
    CLEAN = [grade_outcome("number_fidelity", 1.0), grade_outcome("entity_fidelity", 1.0),
             grade_outcome("completeness", 1.0)]
    BROKEN = [grade_outcome("number_fidelity", 0.8), grade_outcome("entity_fidelity", 0.5),
              grade_outcome("completeness", 0.67)]

    def test_both_pass(self):
        outcome, _, retry = combine(self.CLEAN, judge_outcome(5, 5, 5))
        assert outcome is Outcome.PASS and retry is False

    def test_python_passes_judge_fails_queues_but_does_not_block(self):
        """Figures correct, answer useless. The answer is delivered - it is not
        wrong - and it goes to the remediation queue."""
        outcome, _, retry = combine(self.CLEAN, judge_outcome(2, 5, 5))
        assert outcome is Outcome.WARN, "a judge opinion must never block a deploy"
        assert retry is True

    def test_python_fails_judge_passes_still_blocks(self):
        """Fluent and wrong - the most dangerous output this platform can
        produce, and precisely the case a judge waves through. The grader wins."""
        outcome, reason, retry = combine(self.BROKEN, judge_outcome(5, 5, 5))
        assert outcome is Outcome.FAIL
        assert "number_fidelity" in reason
        assert retry is True

    def test_a_deterministic_failure_is_never_overridden(self):
        for judge in (judge_outcome(5, 5, 5), judge_outcome(1, 1, 1), judge_outcome(None, None, None)):
            assert combine(self.BROKEN, judge)[0] is Outcome.FAIL

    def test_only_a_deterministic_failure_blocks(self):
        from app.evaluation.thresholds import Verdict

        assert Verdict(Outcome.FAIL, "").blocks is True
        assert Verdict(Outcome.WARN, "").blocks is False
        assert Verdict(Outcome.PASS, "").blocks is False
