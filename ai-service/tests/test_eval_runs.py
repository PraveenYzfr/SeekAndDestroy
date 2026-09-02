"""The gate, and the ways a gate quietly stops gating.

A regression gate is a thing everybody trusts and nobody re-reads. These tests
are the cases where it would keep printing "Passed" while meaning nothing:

  * a growing suite failing its own gate, because new cases look like regressions;
  * a shrinking suite passing, because deleted cases cannot fail;
  * an errored case counted as a pass, so an outage reads as quality;
  * "worse than baseline" and "broke a rule" collapsed into one verdict.

None of these break the gate loudly. Each one makes it agree with you.
"""

from __future__ import annotations

import pytest

from app.repositories import eval_run_repository as repo


def _cases(mapping: dict[str, str]) -> list[dict]:
    return [{"CaseId": k, "Outcome": v} for k, v in mapping.items()]


@pytest.fixture
def two_runs(monkeypatch):
    """Compare run 2 against run 1 without touching a database."""

    def _compare(now: dict, before: dict) -> dict:
        stored = {1: _cases(before), 2: _cases(now)}
        monkeypatch.setattr(repo, "cases", lambda run_id: stored[run_id])
        return repo.compare(2, 1)

    return _compare


class TestWhatCountsAsARegression:
    def test_a_case_that_passed_and_now_fails_is_a_regression(self, two_runs):
        result = two_runs({"a": "Failed"}, {"a": "Passed"})
        assert result["regressed"] == ["a"]

    def test_a_new_case_is_not_a_regression(self, two_runs):
        """The one that turns a growing suite against itself.

        A case absent from the baseline cannot have got worse - there is nothing
        to have got worse than. Counting it as a regression means every addition
        to the golden set fails the gate, and the fix people reach for is
        widening the gate.
        """
        result = two_runs({"a": "Passed", "new": "Failed"}, {"a": "Passed"})
        assert result["regressed"] == []
        assert result["added"] == ["new"]

    def test_a_removed_case_is_reported_not_ignored(self, two_runs):
        """Deleting a failing case makes a gate pass. That is the cheapest way
        to defeat one, so a disappearance has to be visible rather than silently
        excluded from the comparison."""
        result = two_runs({"a": "Passed"}, {"a": "Passed", "gone": "Failed"})
        assert result["removed"] == ["gone"]
        assert result["regressed"] == []

    def test_a_case_that_was_already_failing_is_not_a_new_regression(self, two_runs):
        """Still broken is not newly broken. Reporting it again on every run
        trains people to ignore the list."""
        result = two_runs({"a": "Failed"}, {"a": "Failed"})
        assert result["regressed"] == []
        assert result["unchanged"] == ["a"]

    def test_a_fix_is_reported_too(self, two_runs):
        result = two_runs({"a": "Passed"}, {"a": "Failed"})
        assert result["fixed"] == ["a"]

    def test_skipped_does_not_count_as_a_pass(self, two_runs):
        """A case that could not run tells you nothing about quality. Treating a
        provider outage as a passing case is how an incident reads as health."""
        result = two_runs({"a": "Skipped"}, {"a": "Passed"})
        assert result["regressed"] == [], "skipped is not a failure either"
        assert result["unchanged"] == ["a"]


class TestFailedAndRegressedAreDifferentAnswers:
    def test_a_hard_failure_outranks_a_clean_comparison(self):
        """An absolute rule was broken. That is a bug regardless of how the run
        compares to a baseline that may itself have been broken."""
        status, reason = repo.verdict_for({"regressed": []}, hard_failures=3)
        assert status == "Failed"
        assert "3" in reason

    def test_worse_than_baseline_without_a_hard_failure_is_regressed(self):
        """Inside every absolute limit and worse than before. That may be a trade
        somebody chose - it needs a different response from a broken rule, so it
        gets a different status."""
        status, reason = repo.verdict_for({"regressed": ["a", "b"]}, hard_failures=0)
        assert status == "Regressed"
        assert "a" in reason

    def test_clean_is_passed(self):
        status, _ = repo.verdict_for({"regressed": []}, hard_failures=0)
        assert status == "Passed"

    def test_the_two_statuses_are_never_the_same_string(self):
        """If these ever collapse, the gate can no longer tell a bug from a
        deliberate trade, and both get whatever response the louder one gets."""
        failed, _ = repo.verdict_for({"regressed": ["x"]}, hard_failures=1)
        regressed, _ = repo.verdict_for({"regressed": ["x"]}, hard_failures=0)
        assert failed != regressed


class TestTheRunRecordsItsOwnConfiguration:
    def test_models_are_captured(self, monkeypatch):
        """A score without its configuration is a number with no experiment
        attached. "0.94" means nothing; "0.94, narration on groq" is a result."""
        monkeypatch.setattr(
            "app.agents.llm_factory.resolve_all_roles",
            lambda: [{"role": "narration", "provider": "groq", "model": "gpt-oss-20b"}],
        )
        assert repo.current_models() == {"narration": "groq/gpt-oss-20b"}

    def test_unreadable_roles_are_recorded_as_an_error_not_omitted(self, monkeypatch):
        """An absent ModelsJson and one saying "we could not read this" are
        different claims about the run."""
        monkeypatch.setattr(
            "app.agents.llm_factory.resolve_all_roles",
            lambda: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        assert "_error" in repo.current_models()


class TestTheBaselineIsChosen:
    def test_last_passing_is_offered_not_applied(self, monkeypatch):
        """last_passing is a CANDIDATE. A gate that always compares to the
        previous run permits unlimited drift in small steps: every run passes
        against a slightly worse one and no single comparison ever fails.

        This test pins the shape - it returns a row, it does not set anything.
        """
        monkeypatch.setattr(repo, "fetch_all", lambda *a, **k: [{"EvalRunId": 7}])
        assert repo.last_passing("golden") == {"EvalRunId": 7}

    def test_no_previous_run_yields_no_baseline(self, monkeypatch):
        """The first run of a suite has no baseline and must not be recorded as
        though it passed one."""
        monkeypatch.setattr(repo, "fetch_all", lambda *a, **k: [])
        assert repo.last_passing("golden") is None
