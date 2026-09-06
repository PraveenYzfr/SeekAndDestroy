"""A golden run could be shown to be BETTER and never whether it cost more.

sad.EvalRun exists to answer "same 100 queries, what changed?" - it records
CasesPassed, HardCheckRate, JudgeMeanScore, the git sha and the model config,
and recorded nothing about spend, tokens or time. So for a model swap it could
answer at most half the question, and "worse but a third of the price" and
"worse for no saving" were indistinguishable.
"""
from __future__ import annotations

from app.repositories import eval_run_repository as repo
from app.repositories.base import T, execute, fetch_one


def _audit_row(*, cost, tin, tout, started):
    execute(
        f"INSERT INTO {T('AgentAuditLog')} "
        f"(ToolName, GraphNode, StartedAt, Success, CostUsd, PromptTokens, CompletionTokens) "
        f"VALUES ('test', 'test', :s, 1, :c, :i, :o)",
        {"s": started, "c": cost, "i": tin, "o": tout},
    )


class TestARunRecordsWhatItSpent:
    def test_cost_and_tokens_are_summed_over_the_run_window(self):
        run_id = repo.start("golden", triggered_by="test")
        started = fetch_one(
            f"SELECT StartedAt FROM {T('EvalRun')} WHERE EvalRunId = :id", {"id": run_id}
        )["StartedAt"]

        _audit_row(cost=0.0100, tin=100, tout=50, started=started)
        _audit_row(cost=0.0025, tin=20, tout=10, started=started)

        repo.finish(run_id, status="Passed", totals={"total": 2, "passed": 2})

        row = repo.get(run_id)
        assert float(row["CostUsd"]) == 0.0125
        assert row["TokensIn"] == 120 and row["TokensOut"] == 60
        assert row["UnpricedCalls"] == 0
        assert row["DurationMs"] is not None, "wall clock is what a person waited"

    def test_an_unpriced_call_makes_the_cost_a_visible_floor(self):
        """THE POINT OF UnpricedCalls. deepseek was 37 of 63 live calls with none
        of them priced. Without this column a run over that estate stores a small,
        plausible, confident number missing most of its own traffic - and a
        quietly-40%-short total is far harder to catch than a zero."""
        run_id = repo.start("golden", triggered_by="test")
        started = fetch_one(
            f"SELECT StartedAt FROM {T('EvalRun')} WHERE EvalRunId = :id", {"id": run_id}
        )["StartedAt"]

        _audit_row(cost=0.0100, tin=100, tout=50, started=started)
        _audit_row(cost=None, tin=999, tout=999, started=started)  # unpriced

        repo.finish(run_id, status="Passed", totals={"total": 2, "passed": 2})

        row = repo.get(run_id)
        assert row["UnpricedCalls"] == 1, (
            "the cost is a FLOOR and nothing says so"
        )
        assert float(row["CostUsd"]) == 0.0100

    def test_a_failed_spend_read_does_not_lose_the_verdict(self, monkeypatch):
        """The scores are the point; the spend is commentary on them. Losing a
        run's verdict because an accounting query failed would be the same
        inversion as a grader failing the answer it grades."""
        def _boom(*a, **k):
            raise RuntimeError("accounting is down")

        monkeypatch.setattr(repo, "fetch_one", _boom)
        run_id = repo.start("golden", triggered_by="test")
        repo.finish(run_id, status="Passed", totals={"total": 1, "passed": 1})

        monkeypatch.undo()
        row = repo.get(run_id)
        assert row["Status"] == "Passed", "the verdict survived"
        assert row["CostUsd"] is None, "and the missing cost is absent, not zero"
