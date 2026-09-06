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


# ---------------------------------------------------------------------------
# The prompt has to survive the way out
# ---------------------------------------------------------------------------
# It did not, first time. The prompt was generated during extraction, stored on
# state, added to _summarize, tested and deployed - and never reached the
# caller, because run_investigation has TWO return envelopes and the interrupt
# one is rebuilt by hand. A coerced classification happens on a placement
# request, and a placement request interrupts for review, so the single path it
# had to survive was the one that does not go through _summarize.
#
# graph.py already carried a comment warning about this for rejection_prompt.
# The warning was correct and sat on the wrong return.


def test_both_return_envelopes_carry_the_clarification_prompt():
    """Asserted against the source of both envelopes rather than by running the
    graph, because the defect was structural - a key missing from a dict
    literal - and a mocked graph run would have passed while production
    dropped it."""
    import inspect

    from app.graph import graph as graph_module

    src = inspect.getsource(graph_module)

    summarize = src[src.index("def _summarize"):]
    summarize = summarize[: summarize.index("\ndef ")]
    assert '"clarification_prompt"' in summarize, "the Completed envelope drops it"

    interrupt = src[src.index('if result.get("__interrupt__"):'):]
    interrupt = interrupt[: interrupt.index("return answered({**_summarize")]
    assert '"clarification_prompt"' in interrupt, (
        "the AwaitingReview envelope drops it - which is the ONLY path a "
        "coerced classification actually takes"
    )


def test_it_asks_about_classification_before_tier():
    """Both coerced: the question must be the consequential one. `coerced` is
    built in field order, so taking the first element asked about the tier -
    observed on production, inv 140 - while the classification it had also
    invented went unmentioned."""
    p = _clarification_for(
        ["environment", "platform", "availability_tier", "data_classification"], "q"
    )
    assert p["field"] == "data_classification"
    assert "availability_tier" in p["also_assumed"]


# ---------------------------------------------------------------------------
# A defined metric that nothing observes is a dead metric
# ---------------------------------------------------------------------------
# sad_investigation_duration_seconds was defined in metrics.py and observed
# nowhere for several hours - 0 series on production. A labelled Histogram
# emits nothing until it records once, so a panel for it reads "No data",
# which is indistinguishable from a broken scrape or an undeployed build.
#
# It got there by half a recovery: the observe() call was swept into someone
# else's commit, removed to restore production, and never put back when the
# definition landed. Nothing failed, no test broke, and the only symptom was a
# panel that had never worked looking exactly like a panel that had just
# stopped.


def test_every_timing_metric_is_actually_observed():
    """Defined AND used. Asserted over the source rather than by scraping
    /metrics, because an unobserved Histogram is invisible in a scrape - the
    absence is precisely what makes this class of defect quiet."""
    import inspect

    from app.graph import graph as graph_module
    from app.observability import metrics as metrics_module
    from app.repositories import audit_repository

    users = inspect.getsource(graph_module) + inspect.getsource(audit_repository)

    for name in ("investigation_duration_seconds", "llm_duration_seconds"):
        assert hasattr(metrics_module, name), f"{name} is not defined"
        assert f"{name}.labels" in users, (
            f"{name} is defined but never observed - it will emit zero series "
            "and its panel will read 'No data' forever"
        )
