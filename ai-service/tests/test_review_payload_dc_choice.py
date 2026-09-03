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


# ---------------------------------------------------------------------------
# The review message has to report what the search found
#
# It was a constant, said identically whether three clusters qualified or none
# did. Live-verified consequence on production: "what other options?" re-ran
# the estate, returned the same three clusters and the same sentence, and was
# indistinguishable from the question being ignored.
# ---------------------------------------------------------------------------
from app.graph.nodes import _review_message


def test_an_exhausted_shortlist_says_so():
    """The case that made a repeat look like a non-answer. The count is the
    point: it separates a shortlist from a truncation."""
    msg = _review_message(
        {"eligible_total": 3, "shown": 3, "more_available": 0}, None
    )
    assert "3 clusters qualify" in msg
    assert "that is all of them" in msg


def test_a_truncated_shortlist_says_how_many_are_behind_it():
    msg = _review_message(
        {"eligible_total": 11, "shown": 3, "more_available": 8}, None
    )
    assert "top 3 of 11" in msg


def test_nothing_eligible_names_the_constraint_doing_it():
    """"No results" leaves the reader guessing whether they asked for too much
    or the estate is full."""
    msg = _review_message(
        {
            "eligible_total": 0,
            "shown": 0,
            "more_available": 0,
            "blocking_reasons": [
                {"name": "Capacity headroom", "count": 82},
                {"name": "Availability requirement", "count": 17},
            ],
            "size_options": [{"dimension": "cpu_cores"}],
        },
        None,
    )
    assert "No cluster qualifies" in msg
    assert "capacity headroom (82)" in msg
    assert "Asking for less" in msg


def test_the_end_of_a_rescope_is_stated_plainly():
    """When every remaining site has been ruled out the reply must not read as
    a fresh shortlist arriving."""
    msg = _review_message(
        {"eligible_total": 1, "shown": 1, "more_available": 0},
        {"has_genuine_alternative": False, "available_data_centers": []},
    )
    assert "1 cluster qualifies" in msg
    assert "Every other data centre has now been ruled out." in msg


def test_a_rescope_with_room_left_names_what_remains():
    msg = _review_message(
        {"eligible_total": 2, "shown": 2, "more_available": 0},
        {
            "has_genuine_alternative": True,
            "available_data_centers": [
                {"data_center": "Denver-DC1", "eligible_count": 1},
                {"data_center": "Phoenix-DC1", "eligible_count": 1},
            ],
        },
    )
    assert "Remaining data centre(s): Denver-DC1, Phoenix-DC1." in msg
