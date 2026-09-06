"""A run that stops still has to say how much it did.

CasesTotal was written only by complete(), so a run that was aborted, killed by
a deploy, or crashed reported ZERO cases however much work it had done:

    run 39  Error  CasesTotal=0  CasesPassed=0     with 51 EvalCaseResult rows

Praveen read that as a run that failed instantly. It had run for ten minutes and
completed just over half the suite - 42 passed, 9 failed. A row saying zero when
the answer is fifty-one is not missing information, it is WRONG information, and
it is the shape this codebase keeps being caught by: absent reported as a value.

Three earlier instances of the same family this week - a judge failure reported
where the honest answer was not-applicable, a Grafana panel reading "No data"
where the honest answer was zero, and a grader scoring 0.05 where the honest
answer was not-measurable.

THE TALLY IS COUNTED, NOT INCREMENTED, so it cannot drift from the rows it
describes. A retried case or a lost UPDATE corrects itself on the next beat
rather than accumulating an error, and the cost is one COUNT over a
hundred-row table inside an UPDATE that was happening anyway.
"""

from __future__ import annotations

import pytest

from app.repositories import eval_run_repository as repo


class TestTheBeatKeepsTheTallyCurrent:
    def test_the_beat_updates_the_counts_not_only_the_timestamp(self, monkeypatch):
        """The whole change. Before this the UPDATE touched HeartbeatAt alone,
        so the case counts sat at whatever complete() had last written - which
        for a run still going is nothing."""
        seen: dict = {}
        monkeypatch.setattr(repo, "execute", lambda sql, params=None: seen.update(sql=sql))
        repo._beat(39)

        sql = seen["sql"]
        assert "HeartbeatAt" in sql
        for column in ("CasesTotal", "CasesPassed", "CasesFailed", "CasesSkipped"):
            assert column in sql, f"{column} is not maintained by the beat"

    def test_it_counts_from_the_case_rows_rather_than_incrementing(self, monkeypatch):
        """Incrementing would let the tally drift from the rows it describes,
        and a drifted count is worse than a missing one because it looks
        answered."""
        seen: dict = {}
        monkeypatch.setattr(repo, "execute", lambda sql, params=None: seen.update(sql=sql))
        repo._beat(39)

        sql = seen["sql"]
        assert "EvalCaseResult" in sql, "the tally must be counted from the case rows"
        assert "+ 1" not in sql and "+1" not in sql, "counts must not be incremented"

    def test_it_only_touches_a_running_row(self, monkeypatch):
        """complete() is the only thing that knows a run FINISHED rather than
        stopped, so its figures must never be overwritten by a late beat."""
        seen: dict = {}
        monkeypatch.setattr(repo, "execute", lambda sql, params=None: seen.update(sql=sql))
        repo._beat(39)

        assert "Status = 'Running'" in seen["sql"]

    def test_the_outcome_match_does_not_depend_on_server_collation(self, monkeypatch):
        """The rows store 'Passed' and 'Failed'. This server's default collation
        is case-insensitive, so a lowercase literal happens to work here - and a
        tally that silently depends on a collation setting reads zero on a
        differently configured box, which is the same defect wearing a different
        hat."""
        seen: dict = {}
        monkeypatch.setattr(repo, "execute", lambda sql, params=None: seen.update(sql=sql))
        repo._beat(39)

        sql = seen["sql"]
        assert "UPPER(Outcome)" in sql
        assert "'PASSED'" in sql and "'FAILED'" in sql

    def test_a_failed_beat_does_not_fail_the_case(self, monkeypatch):
        """A missed beat costs a stale row. Raising here would cost the run,
        which is the thing the beat exists to describe."""
        def explode(*a, **k):
            raise RuntimeError("database gone")

        monkeypatch.setattr(repo, "execute", explode)
        repo._beat(39)  # must not raise
