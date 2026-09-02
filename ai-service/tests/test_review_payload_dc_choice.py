"""app.graph.nodes.build_review_payload's data_center_choice field.

The second half of "give me from a different DC": excluding a data center is
only a genuine answer if the reviewer can also see what excluding it left
behind - "here are the DCs that still have room" when there is a real
alternative, and "nothing does" when there is not, rather than a shortlist
that just happens to be shorter with no explanation why.

Praveen, on the original failure: "should have given me the genuine next set
or asked me which DC you prefer and told me these are the DCs best choice."
The exclusion itself (tests/test_data_center_exclusion.py) is the first half;
this is the second.
"""

from __future__ import annotations

from app.graph.nodes import build_review_payload


def _candidate(status, data_center=None):
    return {
        "cluster_code": f"test-{data_center or 'none'}-{status}",
        "eligibility_status": status,
        "rule_results": [],
        "snapshot": None,
        "projected": None,
        "data_center": data_center,
        "top_nodes": [],
    }


def test_absent_when_no_exclusion_was_applied():
    """An ordinary first ask has nothing to report here - the field must not
    appear at all, not appear empty. build_review_payload is also reused by
    the RECALL path (app.graph.graph._recall) with a state dict that never
    carries exclude_data_centers, and that path must see exactly this."""
    state = {
        "candidate_scores": [_candidate("Eligible", "atl")],
        "investigation_id": 1,
        "investigation_type": "Hosting",
        "confidence": "High",
    }
    payload = build_review_payload(state)
    assert payload["data_center_choice"] is None


def test_present_with_the_genuine_alternative_when_one_exists():
    state = {
        "candidate_scores": [
            _candidate("Eligible", "atl"),
            _candidate("Eligible", "atl"),
            _candidate("Rejected", "cmh"),
        ],
        "investigation_id": 1,
        "investigation_type": "Hosting",
        "confidence": "High",
        "exclude_data_centers": ["cmh"],
    }
    payload = build_review_payload(state)
    choice = payload["data_center_choice"]
    assert choice is not None
    assert choice["excluded_data_centers"] == ["cmh"]
    assert choice["has_genuine_alternative"] is True
    assert choice["available_data_centers"] == [{"data_center": "atl", "eligible_count": 2}]


def test_honest_when_excluding_leaves_nothing_eligible():
    """The common outcome on this estate (verified live: two DCs, three
    eligible clusters total for a Tier-1 workload) - must read as a plain
    statement, not an empty list the reviewer has to interpret."""
    state = {
        "candidate_scores": [_candidate("Rejected", "atl")],
        "investigation_id": 1,
        "investigation_type": "Hosting",
        "confidence": "Low",
        "exclude_data_centers": ["cmh"],
    }
    payload = build_review_payload(state)
    choice = payload["data_center_choice"]
    assert choice["has_genuine_alternative"] is False
    assert choice["available_data_centers"] == []
