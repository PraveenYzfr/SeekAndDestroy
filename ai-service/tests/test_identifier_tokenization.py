"""Identifiers are not measurements, and the grader kept reading them as arithmetic.

Found by the first real scorecard rather than by reading code. number_fidelity
came back 0.9764 over 719 observations, and every ungrounded token in the failure
list was a short digit fragment - "096", "12", "11". Audit row 23 held the sentence
that produced "096":

    "Den-p096 is recommended for this new capacity request."

The cluster is den-p096. The model capitalised it because it began a sentence, the
case-sensitive entity pattern did not match, the tokenizer reached the bare digits,
and the grader reported an invented number. The model had quoted the code
correctly and was marked down for punctuation.

Three shapes, one bug:

    den-p096   capitalised at a sentence start   -> "096"
    RULE-011   a rule id                          -> "-011", ungroundable
    Tier-1     an availability label              -> "-1",   ungroundable

None of them is a quantity. A negative number in particular can never be grounded
by anything, so every sentence citing the rule that blocked a placement, or naming
the tier a workload needs, was counted as containing a fabricated figure.
"""

from __future__ import annotations

import pytest

from app.evaluation.graders import _numbers_in, number_fidelity


class TestIdentifiersYieldNoNumbers:
    @pytest.mark.parametrize(
        "text",
        [
            "den-p096 is recommended",
            "Den-p096 is recommended",      # the actual failure
            "DEN-P096 IS RECOMMENDED",
            "cmh-p212-NODE-04 failed",
            "APP-CRM was placed",
            "App-crm was placed",
        ],
    )
    def test_a_cluster_or_app_code_is_not_a_number(self, text):
        assert _numbers_in(text) == []

    @pytest.mark.parametrize("text", ["RULE-011 blocked it", "RULE-012 fired", "rule-003 failed"])
    def test_a_rule_id_is_not_a_number(self, text):
        assert _numbers_in(text) == []

    @pytest.mark.parametrize(
        "text", ["Tier-1 workload", "a Tier 1 app", "Tier1", "Sev1 incident", "Sev-2 raised", "P1 outage"]
    )
    def test_a_classification_label_is_not_a_number(self, text):
        assert _numbers_in(text) == []


class TestMeasurementsStillCounted:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("48 cores", ["48"]),
            ("98.34 overall score", ["98.34"]),
            ("headroom fell to 18%", ["18"]),
            ("spans 2 data centres", ["2"]),
            ("1,234 incidents", ["1,234"]),
        ],
    )
    def test_real_figures_survive(self, text, expected):
        assert _numbers_in(text) == expected

    def test_the_sentence_from_audit_23(self):
        """End to end on the real prose: the score is graded, the cluster is not."""
        text = (
            "Den-p096 is recommended for this new capacity request. "
            "It shows strong fit with 98.34 overall."
        )
        assert _numbers_in(text) == ["98.34"]

    def test_stripping_an_identifier_does_not_hide_an_invented_figure(self):
        """The risk of widening the strip list: a fabricated number parked next to
        an identifier must still be caught."""
        evidence = {"candidates": [{"cluster_code": "den-p096", "overall_score": 98.34}]}
        r = number_fidelity("Den-p096 scored 71.2 overall.", evidence)
        assert "71.2" in r.ungrounded

    def test_a_correctly_quoted_score_grounds(self):
        evidence = {"candidates": [{"cluster_code": "den-p096", "overall_score": 98.34}]}
        r = number_fidelity("Den-p096 scored 98.34 overall.", evidence)
        assert r.ungrounded == []
        assert r.total == 1
