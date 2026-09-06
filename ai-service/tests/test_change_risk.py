"""Change risk: a hard freeze rule, and a soft score for churn and failures.

The two read the same data and mean different things. Scheduled churn and a poor
failure rate make a cluster a worse choice; an active freeze makes it not a
choice at all. Keeping them separate is what stops a high capacity score
outvoting a decision somebody made deliberately.
"""

from __future__ import annotations

from decimal import Decimal

from app.rules.eligibility import rule_011_change_freeze
from app.scoring.subscores import change_risk_subscore


class _Ctx:
    def __init__(self, change_risk=None):
        self.change_risk = change_risk


class TestChangeFreezeRule:
    def test_no_change_record_passes(self):
        """An estate that does not do change management must not find every
        cluster ineligible. Absence of evidence is not a freeze."""
        assert rule_011_change_freeze(_Ctx(None)).passed is True

    def test_a_record_without_a_freeze_passes(self):
        result = rule_011_change_freeze(_Ctx({"upcoming_changes": 5, "freeze_until": None}))
        assert result.passed is True

    def test_an_active_freeze_fails_and_says_until_when(self):
        """The reason has to carry the date. "Under a change freeze" leaves the
        engineer with no idea whether to wait an hour or a month."""
        result = rule_011_change_freeze(_Ctx({"freeze_until": "2026-10-01T00:00:00"}))
        assert result.passed is False
        assert "2026-10-01" in result.reason
        assert result.evidence.get("freeze_until")

    def test_it_is_a_hard_rule_not_a_penalty(self):
        """RULE-011 sits in evaluate_all, so a frozen cluster is Rejected rather
        than merely ranked lower. A freeze is a decision, and a capacity score
        should not be able to outvote it."""
        import inspect

        from app.rules import eligibility

        assert "rule_011_change_freeze(ctx)" in inspect.getsource(eligibility.evaluate_all)


class TestChangeRiskSubscore:
    def test_no_record_scores_full(self):
        """"Nothing known against it" - the weakest claim here, and deliberately
        not treated as proof of stability. Scoring it lower would penalise every
        cluster the change process has never touched."""
        assert change_risk_subscore(None) == Decimal("100.00")

    def test_a_clean_history_scores_full(self):
        assert change_risk_subscore(
            {"upcoming_changes": 0, "recent_changes": 40, "recent_failures": 0}
        ) == Decimal("100.00")

    def test_a_rate_on_real_volume_beats_a_rate_on_almost_none(self):
        """The reason the rate is smoothed. Four failures in forty changes is a
        pattern; four in five is a bad week, and a raw rate ranks them the wrong
        way round - 10% vs 80% understates how little the second sample proves.
        """
        many = change_risk_subscore({"upcoming_changes": 0, "recent_changes": 40, "recent_failures": 4})
        few = change_risk_subscore({"upcoming_changes": 0, "recent_changes": 5, "recent_failures": 4})
        assert many > few

    def test_a_single_bad_change_is_not_treated_as_certainty(self):
        """One failure out of one is a 100% raw failure rate. Smoothing keeps it
        as weak evidence rather than the worst cluster in the estate."""
        one_of_one = change_risk_subscore(
            {"upcoming_changes": 0, "recent_changes": 1, "recent_failures": 1}
        )
        assert one_of_one > Decimal("75")

    def test_upcoming_churn_lowers_the_score_on_its_own(self):
        """A forward-looking fact, independent of history: landing a workload in
        the middle of scheduled maintenance is bad even on a cluster that has
        never had a change fail."""
        assert change_risk_subscore(
            {"upcoming_changes": 2, "recent_changes": 40, "recent_failures": 0}
        ) < Decimal("100")

    def test_churn_penalty_is_capped(self):
        """Past a point, fifteen planned changes and fifty are both simply
        "heavily churned" and the difference stops informing the decision."""
        many = change_risk_subscore({"upcoming_changes": 15, "recent_changes": 0, "recent_failures": 0})
        absurd = change_risk_subscore({"upcoming_changes": 50, "recent_changes": 0, "recent_failures": 0})
        assert many == absurd

    def test_the_score_never_leaves_its_range(self):
        worst = change_risk_subscore(
            {"upcoming_changes": 99, "recent_changes": 10, "recent_failures": 10}
        )
        assert Decimal("0") <= worst <= Decimal("100")


class TestWeighting:
    def test_the_weights_still_sum_to_one(self):
        from app.scoring.weights import get_weights

        assert sum(get_weights().values()) == Decimal("1.00")

    def test_change_risk_carries_weight_but_historical_was_not_zeroed(self):
        """Both are kept because they are not independent: change failures in
        the seed are generated from the same cluster stress that drives
        incidents. Taking historical's whole weight would have swapped a direct
        measurement for a correlated proxy."""
        w = get_weights_dict()
        assert w["change_risk"] > 0
        assert w["historical"] > 0

    def test_change_risk_reads_higher_is_safer(self):
        """Unlike `risk`, which the engine inverts. Two conventions in one
        formula is how a sign error survives review."""
        import inspect

        from app.scoring import engine

        source = inspect.getsource(engine.compute_overall_score)
        assert 'w["change_risk"] * subscores.change_risk' in source


def get_weights_dict():
    from app.scoring.weights import get_weights

    return get_weights()
