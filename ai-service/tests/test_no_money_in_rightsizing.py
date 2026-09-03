"""Right-sizing and consolidation report capacity, never currency.

Praveen, on being shown an estimated_monthly_savings of 0.00: "because its our
own data center.. we pay for them, whether we use it or not". That is the whole
argument. The capacity is bought either way, so powering a node down returns
cores rather than money, and a "saving" no budget will ever see is a number
that cannot survive contact with finance.

The figures had been removed from the SCREEN once already - ClusterRightSizing.tsx
said so in a comment, "they are only hidden" - and were still computed, still
returned by the API, and still the key the API sorted "best candidate" by. A
value that is only hidden comes back. These tests are the structural version of
the rule, so it cannot.

Companion to test_no_cost_in_prompts.py, which holds the other half: money that
exists must not reach a prompt. This half says it must not exist here at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.agent_contracts import RightSizingExplanation
from app.models.rightsizing import (
    ApplicationRightSizingResult,
    ClusterRightSizingResult,
    ConsolidationCandidate,
)
from app.services.rightsizing import rank_right_sizing

#: Same vocabulary app.prompts.templates strips from evidence. Kept in step
#: deliberately: a word worth removing from a prompt is worth not computing.
_MONEY_WORDS = ("cost", "price", "chargeback", "saving", "spend", "budget", "rate_card")


@pytest.mark.parametrize(
    "model",
    [
        ClusterRightSizingResult,
        ApplicationRightSizingResult,
        ConsolidationCandidate,
        RightSizingExplanation,
    ],
)
def test_no_money_field_survives_on_these_models(model):
    offenders = [
        name for name in model.model_fields
        if any(word in name.lower() for word in _MONEY_WORDS)
    ]
    assert not offenders, (
        f"{model.__name__} carries {offenders}. These data centres are owned - "
        "the capacity is paid for whether or not it is used, so right-sizing "
        "returns cores, not currency."
    )


def _r(code: str, delta: int, cores: str = "0") -> dict:
    return {"cluster_code": code, "node_delta": delta, "cpu_cores_delta": cores}


class TestRanking:
    """Replaces a bare `[:5]` slice over whatever order SQL returned."""

    def test_clusters_with_nothing_to_do_sort_last(self):
        """Not-Healthy is not the same as actionable. An Overprovisioned cluster
        whose node count is already floored by N-1 tolerance has node_delta 0
        and nothing anyone can act on - it used to be eligible for a top-5 slot
        purely by classification."""
        ranked = rank_right_sizing([_r("idle", 0), _r("real", -2, "-64")])
        assert [r["cluster_code"] for r in ranked] == ["real", "idle"]

    def test_reductions_come_before_expansions(self):
        ranked = rank_right_sizing([_r("needs-more", 3), _r("frees-one", -1, "-32")])
        assert [r["cluster_code"] for r in ranked] == ["frees-one", "needs-more"]

    def test_the_biggest_reduction_leads(self):
        ranked = rank_right_sizing([_r("small", -1, "-8"), _r("big", -4, "-128")])
        assert [r["cluster_code"] for r in ranked] == ["big", "small"]

    def test_cores_break_a_tie_on_node_count(self):
        """Two clusters can free the same number of nodes and very different
        capacity. Without this the order between them was arbitrary."""
        ranked = rank_right_sizing([_r("thin", -2, "-16"), _r("fat", -2, "-256")])
        assert [r["cluster_code"] for r in ranked] == ["fat", "thin"]

    def test_expansions_are_ordered_by_how_short_they_are(self):
        """They all used to tie at 0.00 savings, so their order was whatever
        the list happened to be."""
        ranked = rank_right_sizing([_r("short-1", 1), _r("short-5", 5)])
        assert [r["cluster_code"] for r in ranked] == ["short-5", "short-1"]

    def test_the_order_is_total_and_repeatable(self):
        rows = [_r("b", -1, "-8"), _r("a", -1, "-8"), _r("c", -1, "-8")]
        assert [r["cluster_code"] for r in rank_right_sizing(rows)] == ["a", "b", "c"]
        assert rank_right_sizing(rows) == rank_right_sizing(list(reversed(rows)))

    def test_a_partial_row_ranks_low_instead_of_raising(self):
        """255 good rows must not be lost to one malformed one."""
        ranked = rank_right_sizing([{"cluster_code": "broken"}, _r("good", -1, "-8")])
        assert [r["cluster_code"] for r in ranked] == ["good", "broken"]

    def test_junk_in_a_numeric_field_does_not_raise(self):
        ranked = rank_right_sizing(
            [{"cluster_code": "junk", "node_delta": -1, "cpu_cores_delta": "n/a"},
             _r("good", -1, "-8")]
        )
        assert {r["cluster_code"] for r in ranked} == {"junk", "good"}
