""""Give me from a different DC" must produce a genuinely different shortlist,
not the same one repeated with a re-narrated report on top.

Runs against the live seeded database like the rest of the suite (see
conftest.py). The motivating incident: Praveen rejected a hosting
recommendation, asked for a different data center three different ways, and
got back either a report built from unrelated incidents or a refusal - never
an actual re-run excluding the DC he had just been offered. Routing that
message correctly (app.graph.conversation) is only half the fix; this tests
the other half, that an exclusion asked for is an exclusion applied.
"""

from __future__ import annotations

import pytest

from app.repositories import application_repository, cluster_repository
from app.services import placement


@pytest.fixture(scope="module")
def crm_requirement():
    app = application_repository.get_by_code("APP-CRM")
    return placement.requirement_for_application(app)


def test_every_candidate_carries_the_data_center_it_was_scored_in(crm_requirement):
    """The field a follow-up needs to know what to exclude next time - see
    app.services.refinement.rejection_reasons, which already reads it and,
    before this, always got None because nothing ever set it."""
    ranked = placement.find_and_score_candidates(crm_requirement, top_n=3)
    assert ranked
    for candidate in ranked:
        cluster = cluster_repository.get_by_id(candidate.cluster_id)
        assert candidate.data_center == cluster.DataCenter


def test_search_excludes_the_named_data_centers():
    all_clusters = cluster_repository.search(limit=400)
    data_centers = sorted({c.DataCenter for c in all_clusters if c.DataCenter})
    assert len(data_centers) >= 2, "test needs at least two real data centers in the seed"

    excluded = data_centers[0]
    filtered = cluster_repository.search(exclude_data_centers=[excluded], limit=400)
    assert filtered, "excluding one DC out of several must not empty the estate"
    assert all(c.DataCenter != excluded for c in filtered)
    # And nothing was silently dropped beyond that one DC - the rest of the
    # estate must still be there, not narrowed by a filter that over-matched.
    assert len(filtered) == len([c for c in all_clusters if c.DataCenter != excluded])


def test_search_with_no_exclusion_behaves_exactly_as_before():
    """"No preference stated" and "exclude everything" must stay
    distinguishable - passing None (the default) must not filter anything,
    the same as before this parameter existed."""
    with_none = cluster_repository.search(exclude_data_centers=None, limit=400)
    with_empty = cluster_repository.search(exclude_data_centers=[], limit=400)
    without_param = cluster_repository.search(limit=400)
    assert {c.ClusterId for c in with_none} == {c.ClusterId for c in without_param}
    assert {c.ClusterId for c in with_empty} == {c.ClusterId for c in without_param}


def test_find_and_score_candidates_excludes_a_data_center_end_to_end(crm_requirement):
    baseline = placement.find_and_score_candidates(crm_requirement)
    data_centers = {c.data_center for c in baseline if c.data_center}
    assert len(data_centers) >= 2, "test needs candidates spanning at least two DCs"

    excluded = sorted(data_centers)[0]
    rescoped = placement.find_and_score_candidates(crm_requirement, exclude_data_centers=[excluded])
    assert all(c.data_center != excluded for c in rescoped)
    # A genuine re-run, not an empty one: excluding one of several DCs must
    # still leave real candidates, which is the whole point of the fix -
    # the engineer asked for somewhere else to put the workload, and there
    # has to still BE a somewhere else in the answer.
    assert any(c.eligibility_status == "Eligible" for c in rescoped)


def test_excluding_every_known_data_center_leaves_nothing_eligible(crm_requirement):
    """The honest failure mode: if the engineer has now ruled out every DC
    with capacity, the re-run must say so by producing no eligible
    candidates - not silently ignore the exclusion and show the excluded
    DC's clusters anyway."""
    all_clusters = cluster_repository.search(limit=400)
    every_dc = sorted({c.DataCenter for c in all_clusters if c.DataCenter})
    rescoped = placement.find_and_score_candidates(crm_requirement, exclude_data_centers=every_dc)
    assert not any(c.eligibility_status == "Eligible" for c in rescoped)
