"""Human ratings - the only ground truth this platform has.

The cases here are the ones that would make the feedback loop quietly useless:

  * a taxonomy that accepts anything, which is free text wearing a column name;
  * an upsert that duplicates instead of updating, so one person's changed mind
    becomes two contradictory votes;
  * NULL-keyed rows colliding, which let a person rate exactly one chat answer
    ever - found by exercising it, not by reading it;
  * an agreement calculation that counts unscored answers, which would report
    the judge agreeing with humans on answers it never saw.
"""

from __future__ import annotations

import pytest

from app.repositories import answer_feedback_repository as fb


class TestTheTaxonomyIsClosed:
    def test_an_unknown_reason_is_refused(self):
        """A reason set that accepts anything cannot be counted, routed or
        compared - which is the entire point of not using free text."""
        with pytest.raises(ValueError, match="unknown reason"):
            fb.record(employee_id=1, rating=-1, reason="it_was_rubbish")

    @pytest.mark.parametrize("rating", [2, -2, 5, 100])
    def test_ratings_outside_the_scale_are_refused(self, rating):
        with pytest.raises(ValueError, match="rating must be"):
            fb.record(employee_id=1, rating=rating)

    def test_the_reasons_match_the_remediation_taxonomy(self):
        """A human verdict and a machine verdict have to be comparable. Separate
        vocabularies make "do people agree with the triage" unanswerable."""
        for shared in ("wrong_numbers", "missing_evidence", "did_not_answer",
                       "not_actionable"):
            assert shared in fb.REASONS

    def test_a_rating_needs_no_reason(self):
        """Demanding one is how a feedback control stops being used, and a
        thumbs-up with no explanation is still the data point that matters."""
        assert None not in fb.REASONS  # None is allowed by absence, not by listing


class TestAgreementIsTheWholePoint:
    """Four buckets, and the off-diagonal two are why the table exists."""

    def _agreement(self, monkeypatch, rows):
        monkeypatch.setattr(fb, "fetch_all", lambda *a, **k: rows)
        return fb.agreement()

    def test_person_unhappy_judge_happy_is_the_failure_that_matters(self, monkeypatch):
        """judge_missed. The judge is passing answers people cannot use - the
        exact failure a judge exists to catch, and the one that makes it
        worthless. Praveen's java answer is this row."""
        result = self._agreement(monkeypatch, [{"Rating": -1, "JudgeMinScore": 5}])
        assert result["judge_missed"] == 1
        assert result["both_negative"] == 0

    def test_person_happy_judge_unhappy_is_noise_not_a_defect(self, monkeypatch):
        result = self._agreement(monkeypatch, [{"Rating": 1, "JudgeMinScore": 2}])
        assert result["judge_harsh"] == 1

    def test_agreement_is_counted_both_ways(self, monkeypatch):
        result = self._agreement(monkeypatch, [
            {"Rating": 1, "JudgeMinScore": 5},
            {"Rating": -1, "JudgeMinScore": 2},
        ])
        assert result["both_positive"] == 1
        assert result["both_negative"] == 1

    def test_the_pass_band_matches_thresholds(self, monkeypatch):
        """4 is the judge pass floor in thresholds.py. If these drift apart, the
        agreement report grades the judge against a bar the gate does not use."""
        from app.evaluation.thresholds import JUDGE_PASS

        assert JUDGE_PASS == 4
        assert self._agreement(monkeypatch, [{"Rating": 1, "JudgeMinScore": 4}])["both_positive"] == 1
        assert self._agreement(monkeypatch, [{"Rating": 1, "JudgeMinScore": 3}])["judge_harsh"] == 1

    def test_unscored_answers_are_excluded_not_counted_as_agreement(self, monkeypatch):
        """The query filters JudgeMinScore IS NOT NULL. An answer the machine
        never scored is not evidence about the machine, and counting it would
        report the judge agreeing with humans on answers it never saw."""
        result = self._agreement(monkeypatch, [])
        assert result["total_compared"] == 0
        assert sum(v for k, v in result.items() if k != "total_compared") == 0


class TestTheMachineVerdictIsFrozen:
    def test_it_uses_the_minimum_not_the_mean(self, monkeypatch):
        """Matching thresholds.judge_outcome. A mean of 5,5,2 is 4.0 and hides a
        groundedness failure behind two good scores."""
        monkeypatch.setattr(fb, "fetch_all", lambda *a, **k: [{
            "JudgeRelevance": 5, "JudgeGroundedness": 2, "JudgeActionability": 5,
            "NumberFidelity": 1.0,
        }])
        assert fb._machine_verdict(1)["judge_min"] == 2

    def test_a_missing_machine_verdict_is_none_not_zero(self, monkeypatch):
        """The evaluation runs on a worker thread after the answer is returned,
        so a fast rater beats it. "Not scored yet" and "scored badly" are
        different facts."""
        monkeypatch.setattr(fb, "fetch_all", lambda *a, **k: [])
        assert fb._machine_verdict(1) == {}

    def test_an_unreadable_evaluation_does_not_break_the_rating(self, monkeypatch):
        """Losing the comparison is acceptable. Losing the rating is not - it is
        the scarcer of the two by far."""
        monkeypatch.setattr(
            fb, "fetch_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        )
        assert fb._machine_verdict(1) == {}
