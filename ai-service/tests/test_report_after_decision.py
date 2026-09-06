"""The report kept recommending the cluster the reviewer rejected.

Reported from production. The reviewer approved den-p097 / den-p097-NODE-03 and
the report on screen still read:

    Top recommendation: Cluster cmh-p225 (Columbus-DC1)
    Next steps: Allocate the required 4 CPU cores ... on cluster cmh-p225.
                Update the application deployment manifest to target the
                cmh-p225 cluster code.

The shortlist was correct - den-p097 said Approved, everything else Superseded.
The REPORT was not, because it is written once during the investigation and
never revisited. So the platform issued written instructions to build on a
cluster its reviewer had just declined.
"""
from __future__ import annotations

from app.graph.graph import _report_after_decision

REPORT = {
    "title": "Capacity Investigation Report",
    "executive_summary": "The top-ranked cluster cmh-p225 offers the highest score (98.33).",
    "top_recommendation": "Cluster cmh-p225 (Columbus-DC1)",
    "risks": ["cmh-p225 has a moderate risk score of 33.4"],
    "next_steps": ["Allocate 4 CPU cores on cluster cmh-p225.",
                   "Update the manifest to target the cmh-p225 cluster code."],
    "human_action_required": "Review",
}


class TestAnApprovalCorrectsTheReport:
    def _approved(self):
        return _report_after_decision(
            REPORT, decision="Approve", cluster="den-p097", host="den-p097-NODE-03")

    def test_the_recommendation_becomes_what_was_chosen(self):
        assert self._approved()["top_recommendation"] == "den-p097 / den-p097-NODE-03"

    def test_the_next_steps_no_longer_name_the_rejected_cluster(self):
        """Next steps are INSTRUCTIONS. A stale one is worse than none: it tells
        somebody to build on the wrong box, in writing, under an approval."""
        steps = " ".join(self._approved()["next_steps"])
        assert "cmh-p225" not in steps
        assert "den-p097" in steps

    def test_the_summary_says_the_analysis_predates_the_choice(self):
        """The model's prose is KEPT AND LABELLED, not edited. Rewriting its
        sentences would produce text nobody authored and nobody checked."""
        summary = self._approved()["executive_summary"]
        assert summary.startswith("REVIEWER SELECTED den-p097 / den-p097-NODE-03.")
        assert "written before that choice" in summary
        assert "cmh-p225" in summary, "the original reasoning is retained, not erased"

    def test_the_risks_are_untouched(self):
        """They describe candidates, not the decision, and are still true."""
        assert self._approved()["risks"] == REPORT["risks"]

    def test_choosing_the_ranked_winner_adds_no_contradiction_note(self):
        """Nothing was overridden, so there is nothing to explain."""
        same = _report_after_decision(
            REPORT, decision="Approve",
            cluster="Cluster cmh-p225 (Columbus-DC1)", host=None)
        assert "written before that choice" not in same["executive_summary"]


class TestARejectionLeavesNoRecommendationStanding:
    def test_nothing_is_recommended_after_a_rejection(self):
        out = _report_after_decision(REPORT, decision="Reject", cluster=None, host=None)
        assert out["top_recommendation"] is None
        assert "No placement was approved" in " ".join(out["next_steps"])
        assert "cmh-p225" not in " ".join(out["next_steps"])


class TestItCannotBreakTheDecision:
    def test_a_missing_report_is_returned_unchanged(self):
        assert _report_after_decision(None, decision="Approve",
                                      cluster="x", host=None) is None

    def test_the_original_object_is_not_mutated(self):
        """The stored state keeps what was actually generated at the time."""
        before = dict(REPORT)
        _report_after_decision(REPORT, decision="Approve", cluster="den-p097", host=None)
        assert REPORT == before
