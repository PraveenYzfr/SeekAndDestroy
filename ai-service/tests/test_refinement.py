"""Next steps when the shortlist is thin: what is blocking, and what to offer.

The offers are numbers, not advice. "Try reducing CPU" needs no data; "15 cores
would make 7 more clusters eligible" is a claim about this estate, and these
tests exist because a wrong count is worse than no count - it sends an engineer
down a path that does not work and costs them the round trip to find out.
"""

from __future__ import annotations

from app.services import refinement


def _candidate(status, failed_rules=(), free=None, data_center=None):
    """A candidate score as placement.evaluate_candidate produces it."""
    rules = [{"rule_id": "RULE-001", "name": "Environment compatibility", "passed": True}]
    for rule_id, name in failed_rules:
        rules.append({"rule_id": rule_id, "name": name, "passed": False})
    return {
        "cluster_code": "test-01",
        "eligibility_status": status,
        "rule_results": rules,
        "snapshot": free or {},
        "data_center": data_center,
    }


CAPACITY = ("RULE-003", "Capacity requirement")
HEADROOM = ("RULE-009", "Capacity headroom")
PLATFORM = ("RULE-002", "Platform compatibility")


class TestBlockingReasons:
    def test_reasons_are_ordered_by_how_often_they_blocked(self):
        """"most of them failed on capacity" has to be visible without reading
        each candidate - that is the whole point of not printing every rule."""
        candidates = [
            _candidate("Rejected", [CAPACITY]),
            _candidate("Rejected", [CAPACITY]),
            _candidate("Rejected", [PLATFORM]),
        ]
        reasons = refinement.blocking_reasons(candidates)
        assert reasons[0].rule_id == "RULE-003"
        assert reasons[0].count == 2

    def test_eligible_candidates_contribute_nothing(self):
        assert refinement.blocking_reasons([_candidate("Eligible")]) == []

    def test_both_capacity_rules_are_negotiable(self):
        """RULE-003 is "is there enough free now", RULE-009 is "would the
        projection still fit". Both yield to a smaller request. Marking only
        RULE-009 negotiable hid the most useful offer available: on real data
        RULE-003 blocked 51 of 60 candidates."""
        reasons = {r.rule_id: r for r in refinement.blocking_reasons(
            [_candidate("Rejected", [CAPACITY]), _candidate("Rejected", [HEADROOM])]
        )}
        assert reasons["RULE-003"].negotiable is True
        assert reasons["RULE-009"].negotiable is True

    def test_compatibility_rules_are_not_negotiable(self):
        """No amount of shrinking makes a Windows cluster run a Linux workload,
        and offering it would send the engineer somewhere that cannot work."""
        reasons = refinement.blocking_reasons([_candidate("Rejected", [PLATFORM])])
        assert reasons[0].negotiable is False


class TestSizeOptions:
    def test_it_counts_only_candidates_a_smaller_request_would_actually_free(self):
        """A cluster failing capacity AND platform does not become eligible by
        asking for fewer cores. Counting it would make the offer a lie the
        engineer only discovers after taking it."""
        candidates = [
            _candidate("Rejected", [CAPACITY], {"available_cpu_cores": 16}),
            _candidate("Rejected", [CAPACITY, PLATFORM], {"available_cpu_cores": 16}),
        ]
        options = refinement.size_options(candidates, {"cpu_cores": 20})
        assert options and options[0].would_make_eligible == 1

    def test_an_option_that_unlocks_nothing_is_not_offered(self):
        """Noise at exactly the moment the engineer is already stuck."""
        candidates = [_candidate("Rejected", [CAPACITY], {"available_cpu_cores": 1})]
        assert refinement.size_options(candidates, {"cpu_cores": 20}) == []

    def test_the_label_reads_as_a_person_would_write_it(self):
        """Decimal.normalize() renders 20 as 2E+1, and "15 cores instead of
        2E+1" is not an offer anybody acts on."""
        candidates = [_candidate("Rejected", [CAPACITY], {"available_cpu_cores": 16})]
        label = refinement.size_options(candidates, {"cpu_cores": 20})[0].label
        assert "2E+1" not in label
        assert "instead of 20" in label

    def test_one_option_per_dimension(self):
        """Three shrinking sizes for the same dimension asks the engineer to
        compare our arithmetic instead of making a capacity decision."""
        candidates = [_candidate("Rejected", [CAPACITY], {"available_cpu_cores": 100})]
        options = refinement.size_options(candidates, {"cpu_cores": 20})
        assert len([o for o in options if o.dimension == "cpu_cores"]) == 1


class TestNextSteps:
    def test_a_full_shortlist_offers_nothing(self):
        """The common case is an engineer picking one and leaving. It should
        stay the quiet one - no prompt, no suggestions."""
        steps = refinement.next_steps([_candidate("Eligible") for _ in range(3)], {}, shown=3)
        assert steps["sufficient"] is True
        assert steps["choices"] == []

    def test_more_results_are_offered_as_the_next_slice(self):
        """"Show me the next 3" continues the same ranking rather than starting
        a new search - the next three may be on the same clusters or different
        ones, which is the ranking's business, not a mode to choose."""
        steps = refinement.next_steps([_candidate("Eligible") for _ in range(7)], {}, shown=3)
        more = [c for c in steps["choices"] if c["action"] == "show_more"]
        assert more and more[0]["next_offset"] == 3
        assert "4" in more[0]["detail"]

    def test_paging_past_the_end_stops_offering_more(self):
        steps = refinement.next_steps([_candidate("Eligible") for _ in range(4)], {}, shown=3, offset=3)
        assert [c for c in steps["choices"] if c["action"] == "show_more"] == []

    def test_with_nothing_eligible_it_names_the_hard_constraint_once(self):
        """Rather than every rule every candidate failed - which is the
        detailed summary this replaced."""
        steps = refinement.next_steps(
            [_candidate("Rejected", [PLATFORM]) for _ in range(5)], {}, shown=3
        )
        change = [c for c in steps["choices"] if c["action"] == "change_constraints"]
        assert change and "platform compatibility (5)" in change[0]["detail"]

    def test_every_refinement_carries_what_it_would_buy(self):
        """An offer without a count is advice. With one it is a decision."""
        candidates = [_candidate("Rejected", [CAPACITY], {"available_cpu_cores": 16}) for _ in range(3)]
        steps = refinement.next_steps(candidates, {"cpu_cores": 20}, shown=3)
        refine = [c for c in steps["choices"] if c["action"] == "refine_requirement"]
        assert refine and all("would make" in c["detail"] for c in refine)


# =============================================================================
# data_center_choice: "give me from a different DC" must answer with real
# availability, not a guess and not the same shortlist repeated
# =============================================================================
class TestDataCenterChoice:
    def test_groups_eligible_candidates_by_data_center(self):
        candidates = [
            _candidate("Eligible", data_center="atl"),
            _candidate("Eligible", data_center="atl"),
            _candidate("Eligible", data_center="cmh"),
            _candidate("Rejected", data_center="phx"),
        ]
        result = refinement.data_center_choice(candidates, excluded=["phx"])
        assert result["excluded_data_centers"] == ["phx"]
        assert result["has_genuine_alternative"] is True
        by_dc = {row["data_center"]: row["eligible_count"] for row in result["available_data_centers"]}
        assert by_dc == {"atl": 2, "cmh": 1}
        # A rejected candidate must never inflate a count the engineer would
        # act on - phx has zero ELIGIBLE candidates even though it appears
        # in the input, and it is excluded anyway so it must not appear at all.
        assert "phx" not in by_dc

    def test_most_available_data_center_is_listed_first(self):
        candidates = [_candidate("Eligible", data_center="cmh")] + [
            _candidate("Eligible", data_center="atl") for _ in range(3)
        ]
        result = refinement.data_center_choice(candidates)
        assert result["available_data_centers"][0]["data_center"] == "atl"

    def test_no_genuine_alternative_when_nothing_eligible_survives(self):
        """The control: excluding the only DC with capacity must say so
        plainly, not offer an empty choice as if it were a real one."""
        candidates = [_candidate("Rejected", [CAPACITY], data_center="atl")]
        result = refinement.data_center_choice(candidates, excluded=["cmh"])
        assert result["has_genuine_alternative"] is False
        assert result["available_data_centers"] == []

    def test_candidate_with_no_data_center_is_silently_skipped_not_counted_as_unknown(self):
        """A candidate scored before this field existed (or from a code path
        that never sets it) must not corrupt the grouping with a None/blank
        bucket - it is simply absent from a breakdown it cannot inform."""
        candidates = [_candidate("Eligible", data_center=None), _candidate("Eligible", data_center="atl")]
        result = refinement.data_center_choice(candidates)
        assert result["available_data_centers"] == [{"data_center": "atl", "eligible_count": 1}]

    def test_excluded_defaults_to_empty_list_not_none(self):
        result = refinement.data_center_choice([_candidate("Eligible", data_center="atl")])
        assert result["excluded_data_centers"] == []
