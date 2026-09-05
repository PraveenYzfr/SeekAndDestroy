"""An unparseable data classification must not widen the estate.

Found on production by the Fine Tune session, which logged a real investigation
emitting:

    graph.capacity_extraction_coerced  platform=None  data_classification=None

data_classification fell back to Internal. RULE-005 admits a cluster when
cluster_level >= data_level over Public 0 < Internal 1 < Confidential 2 <
Restricted 3 - so Internal, being level 1 of 4, is the SECOND MOST PERMISSIVE
value available. An unparseable classification therefore did not merely guess,
it guessed in the direction that widens the eligible set, and a workload whose
real classification was Restricted was offered clusters certified only for
Internal.

availability_tier -> Tier-2 had the same direction against RULE-004, where
availability_satisfies is rank(candidate) <= rank(required) and Tier-1 is
rank 0.

These tests pin the DIRECTION rather than the literal default, because the
defect was never "the value is wrong" - it was "the value is wrong in the
direction that admits more infrastructure".
"""

from __future__ import annotations

from app.graph.nodes import _clarification_for, _coerce_enum
from app.models.enums import (
    CLASSIFICATION_LEVEL,
    AVAILABILITY_RANK,
    AvailabilityTier,
    DataClassification,
    availability_satisfies,
    classification_permits,
)


class TestTheFallbackIsTheStrictestValue:
    def test_an_unparseable_classification_becomes_the_most_restrictive(self):
        value, ok = _coerce_enum(None, DataClassification, DataClassification.RESTRICTED)
        assert ok is False
        assert value == DataClassification.RESTRICTED
        assert CLASSIFICATION_LEVEL[value] == max(CLASSIFICATION_LEVEL.values())

    def test_an_unparseable_tier_becomes_the_strongest(self):
        value, ok = _coerce_enum("Standard", AvailabilityTier, AvailabilityTier.TIER_1)
        assert ok is False
        assert AVAILABILITY_RANK[value] == min(AVAILABILITY_RANK.values())

    def test_a_stated_value_is_still_honoured(self):
        """The guard must not override an engineer who did say."""
        assert _coerce_enum("Internal", DataClassification, DataClassification.RESTRICTED) == (
            DataClassification.INTERNAL, True,
        )


class TestTheDirectionOfTheFailure:
    """The point of the change, expressed as estate access rather than values."""

    def test_the_old_default_admitted_clusters_the_new_one_refuses(self):
        clusters = ["Public", "Internal", "Confidential", "Restricted"]
        old = [c for c in clusters if classification_permits(c, DataClassification.INTERNAL)]
        new = [c for c in clusters if classification_permits(c, DataClassification.RESTRICTED)]
        assert set(new) < set(old), "the strict default must admit strictly fewer clusters"
        assert new == ["Restricted"]

    def test_the_same_holds_for_availability(self):
        tiers = ["Tier-1", "Tier-2", "Tier-3"]
        old = [t for t in tiers if availability_satisfies(t, AvailabilityTier.TIER_2)]
        new = [t for t in tiers if availability_satisfies(t, AvailabilityTier.TIER_1)]
        assert set(new) < set(old)


class TestItAsksAsWellAsFailingClosed:
    """Praveen's instruction was ask, with the strict value as the fallback if
    nobody answers - so both halves must be present."""

    def test_a_coerced_classification_produces_a_question(self):
        p = _clarification_for(["data_classification"], "Where can I host a 32 core app?")
        assert p is not None
        assert p["field"] == "data_classification"
        assert p["assumed"] == DataClassification.RESTRICTED
        assert [o["label"] for o in p["options"]] == [
            "Restricted", "Confidential", "Internal", "Public",
        ]

    def test_each_option_carries_a_complete_follow_up(self):
        """A bare "Restricted" would be resolved against the previous turn by
        the conversation layer. Sending the original sentence plus the stated
        value needs nothing from it."""
        p = _clarification_for(["data_classification"], "Where can I host a 32 core app?")
        q = p["options"][0]["query"]
        assert q.startswith("Where can I host a 32 core app?")
        assert "data classification is Restricted" in q

    def test_a_harmless_coercion_asks_nothing(self):
        """platform and environment do not widen the estate, so interrupting
        the engineer over them would be noise."""
        assert _clarification_for(["platform", "environment"], "anything") is None

    def test_every_invented_field_is_still_reported(self):
        p = _clarification_for(["data_classification", "platform"], "q")
        assert p["also_assumed"] == ["platform"]
