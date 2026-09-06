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

SCORES = [
    {"cluster_code": "cmh-p225", "rank": 1, "overall_score": 98.33,
     "projected": {"projected_headroom_percent": 89.55},
     "subscores": {"risk": 33.4, "historical": 90.0},
     "snapshot": {"lifecycle_status": "Deprecated"}},
    {"cluster_code": "phx-p167", "rank": 2, "overall_score": 97.37,
     "projected": {"projected_headroom_percent": 90.47},
     "subscores": {"risk": 33.07, "historical": 88.0},
     "snapshot": {"lifecycle_status": "Deprecated"}},
    {"cluster_code": "den-p097", "rank": 3, "overall_score": 96.22,
     "projected": {"projected_headroom_percent": 29.22},
     "subscores": {"risk": 3.42, "historical": 76.0},
     "snapshot": {"lifecycle_status": "Active"}},
]

REPORT = {
    "title": "Capacity Investigation Report",
    "executive_summary": "The top-ranked cluster cmh-p225 offers the highest score (98.33).",
    "top_recommendation": "Cluster cmh-p225 (Columbus-DC1)",
    # The five lines the reviewer actually saw - one per shortlisted cluster,
    # four of them about candidates he did not take.
    "risks": [
        "Cluster cmh-p225: moderate risk score of 33.4 and a lifecycle status of Deprecated.",
        "Cluster phx-p167: moderate risk score of 33.07 and a lifecycle status of Deprecated.",
        "Cluster den-p097: low risk score of 3.42 but a historical utilization score of 76.0.",
        "Cluster nyc-p006: low risk score of 3.45 with a historical utilization score of 26.0.",
        "Cluster den-p114: low risk score of 3.75 and limited capacity headroom (21.43%).",
    ],
    "next_steps": ["Allocate 4 CPU cores on cluster cmh-p225.",
                   "Update the manifest to target the cmh-p225 cluster code."],
    "human_action_required": "Review",
}


class TestAnApprovalCorrectsTheReport:
    def _approved(self):
        return _report_after_decision(
            REPORT, decision="Approve", cluster="den-p097",
            host="den-p097-NODE-03", candidate_scores=SCORES)

    def test_it_is_called_a_selection_not_a_recommendation(self):
        """It was headed "Top recommendation" over something the reviewer chose
        himself - the platform taking credit for his decision."""
        out = self._approved()
        assert out["top_recommendation"] is None
        assert out["your_selection"] == "den-p097 / den-p097-NODE-03"
        assert out["platform_top_choice"] == "cmh-p225"
        assert out["selected_rank"] == 3
        assert out["executive_summary"].startswith("YOU SELECTED den-p097 / den-p097-NODE-03.")

    def test_it_states_the_rank_and_the_platforms_own_choice(self):
        summary = self._approved()["executive_summary"]
        assert "ranked #3 of 3" in summary
        assert "SeekAndDestroy ranked cmh-p225 first" in summary

    def test_the_trade_off_is_computed_in_both_directions(self):
        """The reviewer traded headroom and score for a cluster that is not
        Deprecated. Saying only that his pick scored lower would be true and
        misleading."""
        summary = self._approved()["executive_summary"]
        assert "96.22 vs 98.33 - 2.11 worse" in summary
        assert "29.22% vs 89.55% - 60.33% worse" in summary
        assert "3.42 vs 33.40 - 29.98 better" in summary, (
            "operational risk ACCUMULATES penalties - lower is better. Calling "
            "3.42 worse than 33.40 inverts the one figure that justifies the "
            "choice."
        )
        assert "cmh-p225 is Deprecated" in summary

    def test_the_risk_direction_matches_the_scoring_module(self):
        """Pinned against the source of truth rather than my reading of it."""
        import inspect

        from app.scoring import subscores

        assert "higher = worse" in inspect.getsource(subscores)

    def test_the_next_steps_no_longer_name_the_rejected_cluster(self):
        """Next steps are INSTRUCTIONS. A stale one is worse than none: it tells
        somebody to build on the wrong box, in writing, under an approval."""
        steps = " ".join(self._approved()["next_steps"])
        assert "cmh-p225" not in steps
        assert "den-p097" in steps

    def test_the_multi_cluster_prose_is_gone(self):
        """It explained all five clusters and concluded the ranked winner
        "provides the most robust environment" - an argument for a candidate the
        reviewer had already declined. The shortlist and findings remain on
        screen; the summary is now about his choice."""
        summary = self._approved()["executive_summary"]
        assert "most robust environment" not in summary
        assert "considered five clusters" not in summary

    def test_only_the_chosen_candidates_risks_are_kept(self):
        """Five risk lines for a shortlist of five told the reviewer about four
        clusters he had declined, and buried the one fact about the box he is
        going to build on."""
        risks = self._approved()["risks"]
        assert len(risks) == 1
        assert "den-p097" in risks[0]
        assert not any("cmh-p225" in r for r in risks)

    def test_choosing_the_ranked_winner_adds_no_contradiction_note(self):
        """Nothing was overridden, so there is nothing to explain."""
        same = _report_after_decision(
            REPORT, decision="Approve", cluster="cmh-p225", host=None,
            candidate_scores=SCORES)
        assert "YOUR CHOICE VERSUS" not in same["executive_summary"]
        assert "also the platform's top-ranked candidate" in same["executive_summary"]


class TestARejectionLeavesNoRecommendationStanding:
    def test_nothing_is_recommended_after_a_rejection(self):
        out = _report_after_decision(REPORT, decision="Reject", cluster=None,
                                     host=None, candidate_scores=SCORES)
        assert out["top_recommendation"] is None
        assert "No placement was approved" in " ".join(out["next_steps"])
        assert "cmh-p225" not in " ".join(out["next_steps"])


class TestItCannotBreakTheDecision:
    def test_a_missing_report_is_returned_unchanged(self):
        assert _report_after_decision(None, decision="Approve", cluster="x",
                                      host=None, candidate_scores=SCORES) is None

    def test_the_original_object_is_not_mutated(self):
        """The stored state keeps what was actually generated at the time."""
        before = dict(REPORT)
        _report_after_decision(REPORT, decision="Approve", cluster="den-p097",
                               host=None, candidate_scores=SCORES)
        assert REPORT == before
