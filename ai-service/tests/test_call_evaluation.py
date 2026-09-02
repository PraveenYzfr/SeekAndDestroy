"""Per-call verdicts: stored whole, versioned, and never able to break a run.

sad.AnswerEvaluation records one row per delivered answer. This records one per
(call, grader), because an answer scoring 0.91 could be one call at 0.55 and four
at 1.0 - and "which call invented that figure" is the question a bad answer
actually raises.
"""

from __future__ import annotations

import json

import pytest

from app.repositories import call_evaluation_repository as repo


class _Recorder:
    """Stands in for the database, capturing what would be written."""

    def __init__(self, fail_on=None):
        self.rows: list[dict] = []
        self.fail_on = fail_on

    def __call__(self, sql, params):
        if self.fail_on and self.fail_on in str(params.get("Grader")):
            raise RuntimeError("simulated write failure")
        self.rows.append(params)
        return 1


def test_the_denominator_is_stored_not_just_the_rate(monkeypatch):
    """A rate without its denominator is not a measurement. 100% over three
    mentions and 100% over four hundred are different claims, and 2/3 rounded to
    0.6667 cannot be turned back into either."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record_many([
        {"audit_id": 7, "investigation_id": 3, "grader": "number_fidelity",
         "grounded": 2, "total": 3, "ungrounded": ["98.5"]},
    ])
    row = rec.rows[0]
    assert row["Grounded"] == 2
    assert row["Total"] == 3
    assert float(row["Rate"]) == pytest.approx(0.6667, abs=1e-4)


def test_nothing_to_score_is_null_not_zero(monkeypatch):
    """Zero is a score. "There was nothing to score" is not, and collapsing them
    puts a perfect-looking 0% into every average."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record_many([
        {"audit_id": 8, "investigation_id": None, "grader": "completeness",
         "grounded": 0, "total": 0, "ungrounded": []},
    ])
    assert rec.rows[0]["Rate"] is None
    assert rec.rows[0]["Total"] == 0


def test_the_offending_tokens_are_kept(monkeypatch):
    """So "which number was invented" needs no re-run of the grader."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record_many([
        {"audit_id": 9, "investigation_id": 1, "grader": "number_fidelity",
         "grounded": 1, "total": 2, "ungrounded": ["100", "42.5"]},
    ])
    assert json.loads(rec.rows[0]["UngroundedJson"]) == ["100", "42.5"]


def test_a_clean_call_stores_no_token_blob(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record_many([
        {"audit_id": 10, "investigation_id": 1, "grader": "entity_fidelity",
         "grounded": 4, "total": 4, "ungrounded": []},
    ])
    assert rec.rows[0]["UngroundedJson"] is None


def test_the_grader_version_travels_with_the_verdict(monkeypatch):
    """The same calls produced 0.9764, 0.8891 and 0.9740 in one night as the
    rules changed. A score without its rules cannot be compared to another."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record_many([
        {"audit_id": 11, "investigation_id": 1, "grader": "number_fidelity",
         "grounded": 1, "total": 1, "ungrounded": []},
    ])
    assert rec.rows[0]["GraderVersion"] == repo.GRADER_VERSION


def test_one_failed_write_does_not_lose_the_others(monkeypatch):
    """Storing a verdict must never fail the run that produced it, and one bad
    row must not take the batch with it."""
    rec = _Recorder(fail_on="entity_fidelity")
    monkeypatch.setattr(repo, "execute", rec)
    written = repo.record_many([
        {"audit_id": 1, "investigation_id": 1, "grader": "number_fidelity",
         "grounded": 1, "total": 1, "ungrounded": []},
        {"audit_id": 1, "investigation_id": 1, "grader": "entity_fidelity",
         "grounded": 1, "total": 1, "ungrounded": []},
        {"audit_id": 1, "investigation_id": 1, "grader": "completeness",
         "grounded": 1, "total": 1, "ungrounded": []},
    ])
    assert written == 2
    assert [r["Grader"] for r in rec.rows] == ["number_fidelity", "completeness"]


def test_re_grading_under_the_same_rules_is_a_no_op(monkeypatch):
    """The unique key is (AuditId, Grader, GraderVersion). A duplicate is the
    design working, not a failure, so it must not be logged as one."""
    def duplicate(sql, params):
        raise RuntimeError("Violation of UNIQUE KEY constraint 'UQ_CallEvaluation'")

    monkeypatch.setattr(repo, "execute", duplicate)
    assert repo.record_many([
        {"audit_id": 1, "investigation_id": 1, "grader": "number_fidelity",
         "grounded": 1, "total": 1, "ungrounded": []},
    ]) == 0


def test_the_written_count_is_returned_so_silence_is_visible(monkeypatch):
    """"graded 40, stored 0" has to be sayable. That exact silence hid a missing
    INSERT grant on sad.AnswerEvaluation for hours - every write "succeeded"."""
    rec = _Recorder(fail_on="number_fidelity")
    monkeypatch.setattr(repo, "execute", rec)
    assert repo.record_many([
        {"audit_id": 1, "investigation_id": 1, "grader": "number_fidelity",
         "grounded": 1, "total": 1, "ungrounded": []},
    ]) == 0
