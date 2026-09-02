"""Cost must not reach the model, because whatever reaches it can be narrated.

Cost was removed from every screen. It kept arriving anyway, inside sentences:

    "The top-ranked candidate is cmh-p225 with an overall score of 98.33 and an
     estimated monthly cost of 5497.01."

Hiding a column hides a column. The candidate objects handed to the model still
carried estimated_monthly_cost, so it reported the figure in prose - a path no UI
change can close, because the number is generated rather than rendered.

with_evidence is the single point every prompt passes through, which is why the
scrub lives there rather than at twelve call sites that would drift apart.
"""

from __future__ import annotations

from app.prompts.templates import _strip_money, with_evidence

CANDIDATE = {
    "candidates": [
        {
            "cluster_code": "cmh-p225",
            "overall_score": 98.33,
            "estimated_monthly_cost": 5497.01,
            "subscores": {"cost": 72, "capacity": 91, "resiliency": 80},
            "snapshot": {"available_cpu_cores": 48, "available_memory_gb": 256},
        }
    ]
}


class TestMoneyNeverReachesTheModel:
    def test_the_reported_sentence_cannot_be_written(self):
        prompt = with_evidence("Explain the recommendation", CANDIDATE)
        assert "5497.01" not in prompt
        assert "estimated_monthly_cost" not in prompt

    def test_the_cost_subscore_goes_too(self):
        """A sub-score named cost is still a number the model will call a cost."""
        assert "cost" not in _strip_money(CANDIDATE)["candidates"][0]["subscores"]

    def test_nested_money_at_any_depth_is_removed(self):
        deep = {"a": {"b": [{"c": {"monthly_cost_per_node": 12.5, "cores": 8}}]}}
        assert _strip_money(deep) == {"a": {"b": [{"c": {"cores": 8}}]}}

    def test_every_money_word_is_covered(self):
        node = {
            "estimated_monthly_cost": 1, "unit_price": 2, "chargeback_code": 3,
            "projected_savings": 4, "monthly_spend": 5, "budget_cap": 6,
            "rate_card_id": 7, "keep_me": 8,
        }
        assert _strip_money(node) == {"keep_me": 8}


class TestEverythingElseSurvives:
    def test_capacity_and_scores_are_untouched(self):
        prompt = with_evidence("Explain", CANDIDATE)
        for kept in ("cmh-p225", "98.33", "available_cpu_cores", "48", "resiliency"):
            assert kept in prompt, f"{kept} was stripped and should not have been"

    def test_a_number_is_not_stripped_for_looking_like_money(self):
        """Matched on the key, never the value. A filter that judged by the number
        would eventually strike a core count or a score."""
        node = {"available_cpu_cores": 5497.01, "overall_score": 99.99}
        assert _strip_money(node) == node

    def test_scalars_and_empty_structures_pass_through(self):
        assert _strip_money("text") == "text"
        assert _strip_money(7) == 7
        assert _strip_money(None) is None
        assert _strip_money({}) == {}
        assert _strip_money([]) == []
