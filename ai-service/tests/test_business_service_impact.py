"""Tests for app.insights.business_service_impact.

compare_leaders is tested against constructed inputs rather than only real
data - the corpus's business-service leaderboard is a moving target (a
reseed can change which service has the most incidents), but "does this
function correctly report no change when the leaders are the same" is a
property that must hold regardless of what the corpus looks like tonight.
"""

from __future__ import annotations

from app.insights.business_service_impact import (
    CRITICALITY_WEIGHTS,
    compare_leaders,
    severity_weighted_impact,
    volume_vs_severity_leader,
)
from app.repositories.base import T, fetch_all


# =============================================================================
# compare_leaders: pure logic, deterministic inputs - the "must fire" and
# "must NOT fire" cases e7 asked for, independent of the live corpus
# =============================================================================
def test_compare_leaders_fires_when_leaders_differ():
    overall = {"business_service": "Payments Core", "incident_count": 100}
    filtered = {"business_service": "Wealth Ledger", "incident_count": 8}
    result = compare_leaders(overall, filtered)
    assert result["leader_changes"] is True
    assert result["overall_leader"] == "Payments Core"
    assert result["filtered_leader"] == "Wealth Ledger"


def test_compare_leaders_control_does_not_fire_when_leaders_match():
    """The control: a same-service case must report leader_changes=False,
    not just default to True as if any comparison were an inversion."""
    same = {"business_service": "Payments Core", "incident_count": 100}
    result = compare_leaders(same, same)
    assert result["leader_changes"] is False


def test_compare_leaders_does_not_fire_when_filtered_side_is_empty():
    """No incidents at all at the filtered severity is not evidence of an
    inversion - it is an absence of data, and must not be reported as a
    change of leader."""
    overall = {"business_service": "Payments Core", "incident_count": 100}
    result = compare_leaders(overall, None)
    assert result["leader_changes"] is False
    assert result["filtered_leader"] is None
    assert result["filtered_leader_count"] == 0


def test_compare_leaders_does_not_fire_when_both_sides_empty():
    result = compare_leaders(None, None)
    assert result["leader_changes"] is False


# =============================================================================
# severity_weighted_impact: count and weight always separate, never blended
# =============================================================================
def test_severity_weighted_impact_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT bs.Name AS ServiceName, bsvc.Criticality, COUNT(*) AS N "
        f"FROM {T('Incident')} i "
        f"JOIN {T('CiRelationship')} bsrel ON bsrel.ChildCiId = i.CmdbCiId AND bsrel.TypeId = 4 "
        f"JOIN {T('ConfigurationItem')} bs ON bs.CiId = bsrel.ParentCiId AND bs.ClassName = 'cmdb_ci_service' "
        f"JOIN {T('CiBusinessService')} bsvc ON bsvc.CiId = bs.CiId "
        f"GROUP BY bs.Name, bsvc.Criticality"
    )
    expected = {(r["ServiceName"], r["Criticality"]): r["N"] for r in expected_rows}

    actual_rows = severity_weighted_impact()
    actual = {(r["business_service"], r["criticality"]): r["incident_count"] for r in actual_rows}
    assert actual == expected


def test_severity_weighted_impact_never_conflates_count_and_weighted_score():
    """e7's warning made concrete: every row must carry both figures
    distinctly, and the weighted figure must equal count * weight exactly -
    never a number that could be mistaken for either alone."""
    rows = severity_weighted_impact()
    assert rows, "expected at least one business service with incidents"
    for row in rows:
        assert row["weighted_impact"] == row["incident_count"] * row["criticality_weight"]
        # A row is either a recognised tier (weight_unknown False, weight > 0
        # for any of the four real tiers) or explicitly flagged unknown -
        # never a silent 0.0 with no explanation.
        if row["criticality_weight"] == 0.0:
            assert row["weight_unknown"] is True


def test_severity_weighted_impact_platinum_outweighs_bronze_at_equal_count():
    """Sanity check on the weighting direction itself, using the documented
    default weights rather than the live corpus's actual distribution
    (which will not reliably produce two services with equal counts)."""
    weight_platinum = CRITICALITY_WEIGHTS["Platinum"]
    weight_bronze = CRITICALITY_WEIGHTS["Bronze"]
    assert weight_platinum > weight_bronze
    same_count = 10
    assert same_count * weight_platinum > same_count * weight_bronze


def test_severity_weighted_impact_can_be_scoped_to_one_severity():
    all_rows = severity_weighted_impact()
    sev1_rows = severity_weighted_impact(severity="Sev1")
    total_all = sum(r["incident_count"] for r in all_rows)
    total_sev1 = sum(r["incident_count"] for r in sev1_rows)
    assert 0 <= total_sev1 <= total_all


# =============================================================================
# volume_vs_severity_leader: the real end-to-end wiring, against live data
# =============================================================================
def test_volume_vs_severity_leader_runs_end_to_end():
    result = volume_vs_severity_leader("Sev1")
    assert "leader_changes" in result
    assert result["top_severity"] == "Sev1"
    # Whatever the live corpus's shape, a reported leader must have a
    # positive count - a "leader" with zero incidents is a contradiction.
    if result["overall_leader"] is not None:
        assert result["overall_leader_count"] > 0
    if result["filtered_leader"] is not None:
        assert result["filtered_leader_count"] > 0
