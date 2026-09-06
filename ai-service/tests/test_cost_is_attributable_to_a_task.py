"""Spend has to carry the task, because a missing dimension cannot be recovered.

WHY THIS FILE EXISTS
--------------------
Ranking operations by TOKENS gets the answer wrong. Measured over one 100-case
golden run on production:

    generate_final_report    50.2% of tokens    33% of spend
    JudgeVerdict             24.7% of tokens    51% of spend

The judge runs on a model whose OUTPUT price is 8.3x the cheapest in use, so the
operation that burns the most tokens is not the one that costs the most money.
Any panel that ranks by tokens and is read as a cost ranking is wrong by one
place, silently.

sad_llm_task_tokens_total has carried {provider, model, task, kind} for a while.
sad_llm_cost_usd_total carried {provider, model} only - so the cheap question
was answerable from metrics and the expensive one was not.

THE PART THAT MAKES THIS A TEST AND NOT A COMMENT
-------------------------------------------------
A label is not a query concern. sad_llm_tokens_total{provider,kind} records
groq's prompt tokens as a single number that has ALREADY summed GroundedAnswer
and FinalRecommendationReport together at write time. No PromQL recovers that
split, because it was never stored. You can only fix it going forward.

So a regression here is not "a panel looks wrong for an afternoon" - it is a
permanent hole in the period during which the label was missing. That is worth a
test that fails loudly rather than a convention.

DO NOT "FIX" A FAILURE HERE BY INFERRING TASK FROM MODEL. It works only while
each task happens to use a distinct model, which is today's configuration and
not a property: the moment two tasks share a model, or one fails over to another
provider, the inference attributes spend to the wrong operation and looks fine.
"""

from __future__ import annotations

import pytest

from app.observability import metrics


class TestTheDimensionExists:
    def test_cost_is_labelled_by_task(self):
        assert "task" in metrics.llm_cost_usd_total._labelnames, (
            "spend cannot be attributed to an operation, and no query can fix that "
            "after the fact - see the module docstring"
        )

    def test_cost_still_carries_provider_and_model(self):
        """Adding task must not cost the two labels the existing panels and the
        UNPRICED alert match on."""
        for label in ("provider", "model"):
            assert label in metrics.llm_cost_usd_total._labelnames

    def test_calls_are_labelled_by_task(self):
        """The denominator. Without it, 'expensive because it runs often' and
        'expensive because each run is huge' are indistinguishable, and they
        have opposite fixes."""
        assert "task" in metrics.llm_task_calls_total._labelnames

    def test_calls_carry_outcome_so_a_rate_is_possible(self):
        assert "outcome" in metrics.llm_task_calls_total._labelnames

    def test_tokens_and_cost_agree_on_their_label_vocabulary(self):
        """Both are joined by `task` in the dashboard table. If one called it
        `task` and the other `tool`, joinByField would produce an empty table
        and read as 'no traffic'."""
        for name in ("provider", "model", "task"):
            assert name in metrics.llm_task_tokens_total._labelnames
            assert name in metrics.llm_cost_usd_total._labelnames


class TestTheEmitSitePopulatesIt:
    """A label that exists and is never populated is worse than no label: the
    series appears, so the panel renders, and every row says "unknown"."""

    def _series(self, counter, label):
        return {
            s.labels.get(label)
            for m in counter.collect()
            for s in m.samples
            if s.name.endswith("_total")
        }

    def test_a_priced_call_is_attributed_to_its_task(self, monkeypatch):
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "_task_of", lambda audit_id: "JudgeVerdict")

        class Cost:
            cost = 0.0125
            input_per_million = 0.30
            output_per_million = 2.50

        ar._observe_cost(1, "gemini", "gemini-3.5-flash-lite", Cost(), True)
        assert "JudgeVerdict" in self._series(metrics.llm_cost_usd_total, "task")
        assert "JudgeVerdict" in self._series(metrics.llm_task_calls_total, "task")

    def test_an_unpriced_call_is_still_attributed(self, monkeypatch):
        """UNPRICED is about the MODEL being unknown, not the task. Dropping the
        task here would make the one thing you need to chase - which operation
        is going unpriced - the one thing not recorded."""
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "_task_of", lambda audit_id: "RightSizingExplanation")

        class Unpriced:
            cost = None
            input_per_million = None
            output_per_million = None

        ar._observe_cost(2, "groq", "some-new-model", Unpriced(), True)
        tasks = self._series(metrics.llm_cost_usd_total, "task")
        assert "RightSizingExplanation" in tasks
        assert "UNPRICED" in self._series(metrics.llm_cost_usd_total, "model")

    def test_a_failed_call_is_counted(self, monkeypatch):
        """It still consumed a provider round trip. A call counter that omits
        failures makes an outage look like idleness."""
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "_task_of", lambda audit_id: "CandidateExplanation")

        class Cost:
            cost = 0.0

        ar._observe_cost(3, "groq", "openai/gpt-oss-20b", Cost(), False)
        assert "error" in self._series(metrics.llm_task_calls_total, "outcome")


class TestTheLookupNeverBreaksTheCall:
    """This file's own rule: recording what a call cost must not be able to fail
    it. Every one of these is the do-nothing path, which is where the last three
    guards in this repo had their defects."""

    def test_the_task_lookup_returns_unknown_rather_than_raising(self, monkeypatch):
        from app.repositories import audit_repository as ar

        def boom(*a, **k):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(ar, "fetch_all", boom)
        assert ar._task_of(1) == "unknown"

    def test_a_missing_audit_row_is_unknown_not_none(self, monkeypatch):
        """None would split one series in two, and the gap reads as the task
        having stopped running rather than as a failed lookup."""
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "fetch_all", lambda *a, **k: [])
        assert ar._task_of(1) == "unknown"

    def test_a_null_toolname_is_unknown(self, monkeypatch):
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "fetch_all", lambda *a, **k: [{"ToolName": None}])
        assert ar._task_of(1) == "unknown"

    def test_the_llm_prefix_is_stripped(self, monkeypatch):
        """"llm:JudgeVerdict" is a storage detail. A label carrying it reads as
        noise on every panel and every alert - and would not join against
        sad_llm_task_tokens_total, which already strips it."""
        from app.repositories import audit_repository as ar

        monkeypatch.setattr(ar, "fetch_all", lambda *a, **k: [{"ToolName": "llm:JudgeVerdict"}])
        assert ar._task_of(1) == "JudgeVerdict"

    def test_observing_cost_never_raises(self, monkeypatch):
        from app.repositories import audit_repository as ar

        def boom(*a, **k):
            raise RuntimeError("metrics registry exploded")

        monkeypatch.setattr(ar, "_task_of", boom)
        ar._observe_cost(1, "groq", "m", type("C", (), {"cost": 1.0})(), True)


class TestTheDashboardCanActuallyBuildTheTable:
    """The panels were written against these exact metric and label names. A
    rename here renders an empty table, which looks like no traffic."""

    def _dashboard(self):
        import json
        from pathlib import Path

        p = Path(__file__).resolve().parents[2] / "docker" / "grafana" / "dashboards" / "seekanddestroy.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_the_spend_by_operation_panel_groups_by_task(self):
        panels = {p["id"]: p for p in self._dashboard()["panels"]}
        assert 112 in panels, "the Spend by operation panel is gone"
        expr = panels[112]["targets"][0]["expr"]
        assert "sum by (task)" in expr and "sad_llm_cost_usd_total" in expr

    def test_the_spend_panel_survives_an_idle_platform(self):
        """A labelled Counter emits no series until its first increment, so an
        idle hour renders 'No data' and is indistinguishable from a broken
        exporter. Same defect as the judge panels."""
        panels = {p["id"]: p for p in self._dashboard()["panels"]}
        assert "or vector(0)" in panels[112]["targets"][0]["expr"]

    def test_the_table_joins_on_the_label_the_metrics_actually_carry(self):
        panels = {p["id"]: p for p in self._dashboard()["panels"]}
        assert 113 in panels, "the cost-per-operation table is gone"
        join = next(t for t in panels[113]["transformations"] if t["id"] == "joinByField")
        assert join["options"]["byField"] == "task"

    @pytest.mark.parametrize("metric", [
        "sad_llm_task_calls_total", "sad_llm_task_tokens_total", "sad_llm_cost_usd_total",
    ])
    def test_the_table_reads_metrics_that_exist(self, metric):
        panels = {p["id"]: p for p in self._dashboard()["panels"]}
        exprs = " ".join(t["expr"] for t in panels[113]["targets"])
        assert metric in exprs
