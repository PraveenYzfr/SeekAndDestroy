"""Scenario B (free-text, no named app) capacity extraction: regex under the
offline mock LLM (which has no real NLU), real LangChain structured
extraction once a real provider is configured. See app.graph.nodes.

THESE TESTS WERE PATCHING A FUNCTION THE CODE NO LONGER CALLS.

They monkeypatched nodes.get_chat_model. Role-based model selection replaced
every call site with get_chat_model_for_role(role), and nodes.py kept importing
the old name without using it. monkeypatch.setattr only fails on an attribute
that does not EXIST, and this one still did - so the patch applied cleanly,
bound nothing, and the tests silently resolved the real configured provider.

With SAD_LLM__PROVIDER=deepseek that meant the offline test made a LIVE, BILLED
API call, and its result decided the assertion:

    deepseek answers    -> extracted is not None -> method "llm"  -> TEST FAILS
    deepseek errors     -> caught, returns None  -> method "regex" -> TEST PASSES

So it passed only when the API was broken, which is why it looked intermittent
across runs on the same code and the same machine.

Patch get_chat_model_for_role, and note it takes a role argument - a zero-arg
lambda would raise TypeError rather than silently doing nothing, which is the
better failure but still a failure.
"""

from __future__ import annotations

from app.agents.mock_llm import MockChatModel
from app.graph import nodes
from app.models.agent_contracts import CapacityRequirement
from app.models.enums import InvestigationType


def test_capacity_extraction_uses_regex_under_mock_llm(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: MockChatModel())
    state = {
        "user_query": "I need 8 CPU, 32 GB RAM and 500 GB storage for a production Kubernetes workload.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "regex"
    assert result["capacity_requirements"]["cpu_cores"] == 8.0
    assert result["capacity_requirements"]["memory_gb"] == 32.0
    assert result["capacity_requirements"]["storage_gb"] == 500.0


class _FakeRealChatModel:
    """Stands in for a real (non-mock) BaseChatModel so the LLM extraction
    branch can be exercised without a live provider."""


def test_capacity_extraction_uses_llm_chain_when_real_provider_configured(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: _FakeRealChatModel())
    extracted = CapacityRequirement(
        environment="Production", cpu_cores=16.0, memory_gb=64.0, storage_gb=1000.0,
        platform="VMware", availability_tier="Tier-1", data_classification="Confidential",
        preferred_location="Atlanta-DC1", expected_growth_percent=15.0,
    )
    monkeypatch.setattr(nodes, "extract_capacity_requirement", lambda llm, query: extracted)
    state = {
        "user_query": "We need a new Confidential, Tier-1 environment in Atlanta for a growing workload.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "llm"
    assert result["capacity_requirements"]["cpu_cores"] == 16.0
    assert result["requirement"]["platform"] == "VMware"
    assert result["requirement"]["preferred_location"] == "Atlanta-DC1"


def test_capacity_extraction_falls_back_to_regex_on_llm_failure(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: _FakeRealChatModel())

    def _boom(llm, query):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(nodes, "extract_capacity_requirement", _boom)
    state = {
        "user_query": "I need 4 CPU, 16 GB RAM and 200 GB storage.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "regex"
    assert result["capacity_requirements"]["cpu_cores"] == 4.0


# ---------------------------------------------------------------------------
# The contract used to reject the model's correct answer
# ---------------------------------------------------------------------------
# Production, nine calls, 100% failure, 68s average. The payload below is the
# real one, copied verbatim from sad.AgentAuditLog for the query Praveen was
# actually running - "Where can I host a Tier-1 production Java app needing 32
# cores and 128 GB?". It states no storage and no data classification, because
# the question does not mention either, so the model returned null for both.
#
# CapacityRequirement declared them non-nullable, so pydantic rejected it. The
# repair retry in app.agents.structured then asked the model to fix JSON that
# was never malformed; it returned the same object, was rejected again, and the
# whole extraction fell through to regex - which supplied _CAPACITY_DEFAULTS,
# the exact numbers the LLM path would have used. Two model calls, a minute of
# latency, and an identical result.


def test_unstated_dimensions_parse_as_null_rather_than_failing():
    """The real production completion must parse. This is the regression."""
    payload = (
        '{"environment": "production", "cpu_cores": 32, "memory_gb": 128, '
        '"storage_gb": null, "platform": "Java", "availability_tier": "Tier-1", '
        '"data_classification": null, "preferred_location": null, '
        '"expected_growth_percent": 0.0, "required_by_days": null}'
    )
    parsed = CapacityRequirement.model_validate_json(payload)
    assert parsed.cpu_cores == 32
    assert parsed.memory_gb == 128
    # Null survives as null. It is NOT defaulted here - the contract's job is to
    # carry "not stated" faithfully, and inventing 500 GB at parse time would
    # make an assumed figure indistinguishable from a stated one.
    assert parsed.storage_gb is None
    assert parsed.data_classification is None


def test_defaults_for_unstated_dimensions_are_declared_not_hidden(monkeypatch):
    """Resolution happens in the graph node, and says what it assumed."""
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: _FakeRealChatModel())
    extracted = CapacityRequirement(
        environment="production", cpu_cores=32.0, memory_gb=128.0, storage_gb=None,
        platform="Java", availability_tier="Tier-1", data_classification=None,
    )
    monkeypatch.setattr(nodes, "extract_capacity_requirement", lambda llm, query: extracted)
    state = {
        "user_query": "Where can I host a Tier-1 production Java app needing 32 cores and 128 GB?",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    caps = result["capacity_requirements"]

    # The extraction now SUCCEEDS - this is the whole point. Previously this
    # query produced extraction_method "regex" after two failed model calls.
    assert caps["extraction_method"] == "llm"
    assert caps["cpu_cores"] == 32.0
    assert caps["memory_gb"] == 128.0

    # Storage was never stated, so it is defaulted AND declared.
    assert caps["storage_gb"] == nodes._CAPACITY_DEFAULTS["storage_gb"]
    assert caps["assumed_defaults"] == ["storage_gb"]
    assert "cpu_cores" not in caps["assumed_defaults"]

    # A null classification lands on the documented fallback rather than
    # exploding - _coerce_enum already accepted None, which is why the contract
    # was the only thing standing in the way.
    assert result["requirement"]["data_classification"] == "Internal"


# ---------------------------------------------------------------------------
# Confidence must not outrun the evidence
# ---------------------------------------------------------------------------
# Praveen was shown a report asserting, in consecutive sentences, that it had
# "no top candidates, no forecast results, and no capacity calculations" and
# that "overall evidence confidence is High". assess_risk_and_confidence
# returned "High" unconditionally for Question and Forecast investigations.


def _confidence(state: dict) -> str:
    return nodes.assess_risk_and_confidence(state)["confidence"]


def test_a_question_with_no_evidence_is_not_confident():
    state = {
        "investigation_type": InvestigationType.QUESTION,
        "user_query": "give me from a different DC",
        "retrieved_context": [],
        "forecast_results": {},
        "candidate_scores": [],
    }
    assert _confidence(state) == "Low"


def test_a_grounded_question_is_capped_at_medium():
    """High is reserved for a scored candidate above the threshold.

    Retrieval returning documents says the answer was grounded in something. It
    does not say the something answers the question, and spending the word
    "High" on both makes it mean two things in one report.
    """
    state = {
        "investigation_type": InvestigationType.QUESTION,
        "user_query": "what changed on nyc-05 last quarter?",
        "retrieved_context": [{"text": "CHG0030638 nyc-05 BackedOut"}],
        "forecast_results": {},
        "candidate_scores": [],
    }
    assert _confidence(state) == "Medium"


def test_an_informational_answer_still_needs_no_approval():
    """Confidence changed; the review flag deliberately did not. There is no
    recommendation being proposed, so there is nothing to approve."""
    state = {
        "investigation_type": InvestigationType.FORECAST,
        "user_query": "forecast cmh-03",
        "retrieved_context": [],
        "forecast_results": {"cmh-03": {"projected": 78}},
        "candidate_scores": [],
    }
    out = nodes.assess_risk_and_confidence(state)
    assert out["confidence"] == "Medium"
    assert out["human_review_required"] is False


def test_a_scored_recommendation_can_still_be_high():
    """The measured path is untouched: High means the top eligible candidate
    scored at or above scoring.min_confident_score."""
    state = {
        "investigation_type": InvestigationType.HOSTING,
        "user_query": "find hosting for APP-CRM",
        "candidate_scores": [{"eligibility_status": "Eligible", "overall_score": 91.0}],
    }
    out = nodes.assess_risk_and_confidence(state)
    assert out["confidence"] == "High"
    assert out["human_review_required"] is True


def test_right_sizing_needs_no_human_review():
    """Live-verified broken: a right-sizing question fell through to the
    default branch, which reads candidate_scores - a key right-sizing never
    populates (its results live in capacity_calculations) - found it empty,
    and required review with nothing to review. The reviewer saw "choose one
    cluster and host, then approve" with zero options, and the real findings
    (which clusters were flagged and why) sat computed in state and were
    never reached, because generate_final_report only runs on the
    no-review-required branch this state never took."""
    state = {
        "investigation_type": InvestigationType.RIGHT_SIZING,
        "user_query": "which clusters are underutilized?",
        "candidate_scores": [],
        "capacity_calculations": {"right_sizing": [{"cluster_code": "atl-03", "classification": "Overprovisioned"}]},
    }
    out = nodes.assess_risk_and_confidence(state)
    assert out["human_review_required"] is False
    assert out["confidence"] == "Medium"


def test_consolidation_needs_no_human_review():
    state = {
        "investigation_type": InvestigationType.CONSOLIDATION,
        "user_query": "consolidate workloads in production",
        "candidate_scores": [],
        "capacity_calculations": {"consolidation": [{"application_code": "APP-CRM", "feasible": True}]},
    }
    out = nodes.assess_risk_and_confidence(state)
    assert out["human_review_required"] is False
    assert out["confidence"] == "Medium"


def test_right_sizing_with_no_calculations_at_all_is_low_not_confident():
    """The honest floor: no capacity_calculations at all (an empty estate, or
    a failure upstream) must read as Low, not inherit Medium from a key that
    happens to exist but is empty."""
    state = {
        "investigation_type": InvestigationType.RIGHT_SIZING,
        "user_query": "which clusters are underutilized?",
        "candidate_scores": [],
        "capacity_calculations": {},
    }
    out = nodes.assess_risk_and_confidence(state)
    assert out["human_review_required"] is False
    assert out["confidence"] == "Low"


# ---------------------------------------------------------------------------
# "give me from a different DC" must actually exclude the data centre
# ---------------------------------------------------------------------------
from app.graph.conversation import PriorInvestigation, excluded_data_centers, resolve


def _prior_offering(*data_centers: str) -> PriorInvestigation:
    return PriorInvestigation(
        investigation_id=1,
        investigation_type="Hosting",
        user_query="find hosting for APP-CRM",
        application_code="APP-CRM",
        candidate_scores=[
            {"cluster_code": f"c{i}", "data_center": dc, "eligibility_status": "Eligible"}
            for i, dc in enumerate(data_centers)
        ],
    )


def test_asking_for_a_different_dc_excludes_only_the_site_it_was_offered():
    """_prior_offering lists the shortlist in rank order, so the FIRST entry is
    the recommendation. Denver here, deliberately not Atlanta - an expectation
    that happens to match the alphabetically-first site would pass whether the
    code read the ranking or just sorted."""
    prior = _prior_offering("Denver-DC1", "Atlanta-DC1", "Denver-DC1")
    excluded = excluded_data_centers("give me from a different DC", prior)
    assert excluded == ["Denver-DC1"]
    # Atlanta was ranked below it and never offered, so it stays available.
    assert "Atlanta-DC1" not in excluded


def test_a_rescope_that_names_no_location_excludes_nothing():
    """"what other options?" wants a different ANSWER, not necessarily a
    different site. Excluding a data centre nobody mentioned would drop
    candidates for a reason never stated and never shown."""
    prior = _prior_offering("Denver-DC1", "Atlanta-DC1")
    assert excluded_data_centers("what other options?", prior) == []


def test_it_excludes_nothing_when_the_previous_turn_had_no_candidates():
    prior = PriorInvestigation(
        investigation_id=1, investigation_type="Question", user_query="anything",
    )
    assert excluded_data_centers("what other DCs?", prior) == []


def test_the_exclusion_reaches_the_resolution():
    """End of the chain that graph.py hands to new_state."""
    prior = _prior_offering("Denver-DC1")
    resolution = resolve("give me from a different DC", prior)
    assert resolution.kind == "InheritSubject"
    assert resolution.exclude_data_centers == ["Denver-DC1"]


def test_an_ordinary_turn_carries_no_exclusion():
    prior = _prior_offering("Denver-DC1")
    assert resolve("why was that rejected?", prior).exclude_data_centers == []


def test_the_location_gate_matches_a_real_data_centre_name():
    """The rejection button in Chat.tsx sends "...but not in Denver-DC1", and
    that is the PRIMARY way this feature is reached. \bdc\b does not match
    inside "DC1" - no word boundary between C and 1 - so the click meant to
    trigger the whole feature would have excluded nothing, silently."""
    prior = _prior_offering("Denver-DC1", "Atlanta-DC1")
    assert excluded_data_centers("Show other options, but not in Denver-DC1.", prior) == ["Denver-DC1"]


def test_naming_one_data_centre_excludes_only_that_one():
    """A reviewer who named ONE site meant that one, not every site they were
    shown - the live-verified bug this replaces: an earlier version treated
    any location-flavoured text as license to exclude the WHOLE previous
    pool (including candidates rejected for reasons that had nothing to do
    with location), which on a real request excluded all eight data centres
    in the estate from a single-site objection and returned nothing."""
    prior = PriorInvestigation(
        investigation_id=1, investigation_type="Hosting", user_query="find hosting for APP-CRM",
        application_code="APP-CRM",
        candidate_scores=[
            {"cluster_code": "atl-03", "data_center": "Atlanta-DC1", "eligibility_status": "Eligible"},
            {"cluster_code": "den-03", "data_center": "Denver-DC1", "eligibility_status": "Eligible"},
            {"cluster_code": "den-p096", "data_center": "Denver-DC1", "eligibility_status": "Eligible"},
            # The pool a broad environment/platform scan turns up: rejected
            # for reasons unrelated to location, spanning data centres the
            # engineer was never offered anything in.
            {"cluster_code": "phx-01", "data_center": "Phoenix-DC1", "eligibility_status": "Rejected"},
            {"cluster_code": "cmh-02", "data_center": "Columbus-DC1", "eligibility_status": "Rejected"},
        ],
    )
    assert excluded_data_centers(
        "Show other options, but not in the Atlanta-DC1 data center.", prior
    ) == ["Atlanta-DC1"]


def test_generic_rescope_with_no_named_dc_excludes_only_what_was_offered():
    """The exact reproduction of the live bug, at the scale that triggered
    it: "give me from a different DC" with nothing named must exclude the
    data centres that were actually OFFERED (eligible), never the ones a
    broad environment/platform scan rejected clusters in for unrelated
    reasons. A version that used the whole pool here would return every one
    of the five data centres below and starve the very next request."""
    prior = PriorInvestigation(
        investigation_id=1, investigation_type="Hosting", user_query="find hosting for APP-CRM",
        application_code="APP-CRM",
        candidate_scores=[
            {"cluster_code": "atl-03", "data_center": "Atlanta-DC1", "eligibility_status": "Eligible"},
            {"cluster_code": "den-03", "data_center": "Denver-DC1", "eligibility_status": "Eligible"},
        ] + [
            {"cluster_code": f"rej-{i}", "data_center": dc, "eligibility_status": "Rejected"}
            for i, dc in enumerate(["Phoenix-DC1", "Columbus-DC1", "Dallas-DC1"])
        ],
    )
    excluded = excluded_data_centers("give me from a different DC", prior)
    # Only the site that was actually recommended.
    assert excluded == ["Atlanta-DC1"]
    # Denver survives - ranks 2 and 3 were never offered, and they are the
    # genuine next set Praveen asked for.
    assert "Denver-DC1" not in excluded
    # And never the rejected pool - the original point of c2's fixture.
    assert not {"Phoenix-DC1", "Columbus-DC1", "Dallas-DC1"} & set(excluded)


def test_plurals_and_synonyms_all_open_the_gate():
    prior = _prior_offering("Denver-DC1")
    for phrasing in (
        "what other DCs?",
        "anything in another zone",
        "what other regions",
        "a different site",
        "somewhere in another data centre",
    ):
        assert excluded_data_centers(phrasing, prior) == ["Denver-DC1"], phrasing


def test_a_subject_free_rescope_still_opens_no_gate():
    """The counter-case that keeps the gate meaningful."""
    prior = _prior_offering("Denver-DC1")
    for phrasing in ("what other options?", "give me another one", "show me something else"):
        assert excluded_data_centers(phrasing, prior) == [], phrasing


# ---------------------------------------------------------------------------
# The exclusion must not empty the shortlist
# ---------------------------------------------------------------------------
# Measured on the live estate, APP-CRM's eligible shortlist:
#     rank 1  atl-03    Atlanta-DC1  91.38   <- the recommendation
#     rank 2  den-03    Denver-DC1   85.30
#     rank 3  den-p096  Denver-DC1   81.84
# Excluding every eligible DC removes the whole shortlist and returns nothing,
# on Praveen's own phrasing. He rejected ONE recommendation; ranks 2 and 3 were
# never offered and are the "genuine next set" he asked for.


def _ranked_prior() -> PriorInvestigation:
    return PriorInvestigation(
        investigation_id=7, investigation_type="Hosting",
        user_query="find hosting for APP-CRM", application_code="APP-CRM",
        candidate_scores=[
            {"cluster_code": "atl-03", "data_center": "Atlanta-DC1",
             "eligibility_status": "Eligible", "rank": 1, "overall_score": 91.38},
            {"cluster_code": "den-03", "data_center": "Denver-DC1",
             "eligibility_status": "Eligible", "rank": 2, "overall_score": 85.30},
            {"cluster_code": "den-p096", "data_center": "Denver-DC1",
             "eligibility_status": "Eligible", "rank": 3, "overall_score": 81.84},
            # The rejected pool spans the whole estate and must never be excluded.
            {"cluster_code": "nyc-01", "data_center": "New York-DC1",
             "eligibility_status": "Rejected", "rank": None, "overall_score": None},
            {"cluster_code": "cmh-02", "data_center": "Columbus-DC1",
             "eligibility_status": "Rejected", "rank": None, "overall_score": None},
        ],
    )


def test_it_excludes_only_the_data_centre_that_was_recommended():
    assert excluded_data_centers("give me from a different DC", _ranked_prior()) == ["Atlanta-DC1"]


def test_the_genuine_next_set_survives():
    """The whole point: ranks 2 and 3 were never offered, so they must remain."""
    excluded = excluded_data_centers("give me from a different DC", _ranked_prior())
    survivors = [
        c for c in _ranked_prior().candidate_scores
        if c["eligibility_status"] == "Eligible" and c["data_center"] not in excluded
    ]
    assert [c["cluster_code"] for c in survivors] == ["den-03", "den-p096"]


def test_naming_a_site_still_wins_over_the_ranking():
    """c2's case 1 is unchanged - the rejection button names one site."""
    assert excluded_data_centers(
        "Show other options, but not in the Denver-DC1 data center.", _ranked_prior()
    ) == ["Denver-DC1"]


def test_an_unranked_shortlist_falls_back_to_score():
    prior = PriorInvestigation(
        investigation_id=8, investigation_type="Hosting", user_query="x",
        candidate_scores=[
            {"cluster_code": "a", "data_center": "Denver-DC1",
             "eligibility_status": "Eligible", "rank": None, "overall_score": 70.0},
            {"cluster_code": "b", "data_center": "Atlanta-DC1",
             "eligibility_status": "Eligible", "rank": None, "overall_score": 88.0},
        ],
    )
    assert excluded_data_centers("a different DC", prior) == ["Atlanta-DC1"]


# ---------------------------------------------------------------------------
# The report contract demanded an id the model cannot know
# ---------------------------------------------------------------------------
# On the 100-case golden run, 29 final reports failed to parse and 28 failed on
# investigation_id alone. Every one carried a complete report - title, summary,
# risks, next steps - discarded because an identifier the platform already held
# was null. That is what "Report narration unavailable" was on screen.
from app.models.agent_contracts import FinalRecommendationReport


def test_a_report_parses_when_the_model_omits_the_id():
    """The real completion shape from the golden run."""
    payload = (
        '{"investigation_id": null,'
        ' "title": "Spare capacity investigation for atl-p063",'
        ' "executive_summary": "The evidence contains no entries for atl-p063.",'
        ' "top_recommendation": null, "alternatives_considered": [],'
        ' "risks": ["No record of host atl-p063 was found."],'
        ' "next_steps": ["Confirm whether the host name is correct."],'
        ' "human_action_required": "Verify the identifier."}'
    )
    report = FinalRecommendationReport.model_validate_json(payload)
    assert report.investigation_id is None
    assert report.title.startswith("Spare capacity")
    # The content was never the problem - it was complete all along.
    assert report.risks and report.next_steps


def test_the_platform_stamps_the_id_rather_than_trusting_it(monkeypatch):
    """Asking a model to carry an identifier is a way to let it change one.

    The caller already has the id; the model's echo is at best redundant and at
    worst a number a language model altered - which is what
    assert_no_number_drift exists to prevent everywhere else.
    """
    from app.agents import chains

    stamped = FinalRecommendationReport(
        investigation_id=999,           # a WRONG id, as if echoed badly
        title="t", executive_summary="s", human_action_required="h",
    )
    monkeypatch.setattr(chains, "run_structured", lambda *a, **k: stamped)
    monkeypatch.setattr(chains, "assert_no_number_drift", lambda *a, **k: None)

    out = chains.generate_final_report(
        llm=object(), investigation_id=7, title="t", evidence={},
    )
    assert out.investigation_id == 7


# ---------------------------------------------------------------------------
# Asking twice must not walk in a circle
#
# Live-verified defect, production, one conversation of four turns: turn 3
# ("give me from a different DC") excluded Columbus and offered Phoenix; turn 4
# ("what other DCs?") excluded Phoenix ONLY and offered Columbus back - the
# site the engineer had ruled out one turn earlier, listed as though it were a
# fresh choice. Exclusions are derived per turn, so they have to accumulate.
# ---------------------------------------------------------------------------


def test_a_second_rescope_keeps_the_first_exclusion():
    """Turn 4 of the production sequence. The prior turn was itself a re-scope
    that had already ruled Columbus out; asking again must rule out Phoenix AS
    WELL, not INSTEAD."""
    prior = PriorInvestigation(
        investigation_id=2,
        investigation_type="Capacity",
        user_query="give me from a different DC",
        candidate_scores=[
            {"cluster_code": "phx-p167", "data_center": "Phoenix-DC1", "eligibility_status": "Eligible"},
            {"cluster_code": "den-p096", "data_center": "Denver-DC1", "eligibility_status": "Eligible"},
        ],
        exclude_data_centers=["Columbus-DC1"],
    )
    excluded = excluded_data_centers("what other DCs?", prior)
    assert excluded == ["Columbus-DC1", "Phoenix-DC1"]


def test_naming_a_site_still_keeps_what_was_already_ruled_out():
    """The named-site branch is reached from the Chat.tsx rejection button,
    which names one site. Naming a second one adds to the set; it does not
    reset a conversation's history of rejections."""
    prior = PriorInvestigation(
        investigation_id=3,
        investigation_type="Capacity",
        user_query="give me from a different DC",
        candidate_scores=[
            {"cluster_code": "den-p096", "data_center": "Denver-DC1", "eligibility_status": "Eligible"},
        ],
        exclude_data_centers=["Columbus-DC1"],
    )
    assert excluded_data_centers(
        "not in the Denver-DC1 data center", prior
    ) == ["Columbus-DC1", "Denver-DC1"]


def test_carried_exclusions_survive_a_turn_with_nothing_eligible():
    """When the previous turn had no eligible candidate there is no new site to
    add - but the ones already ruled out stay ruled out. Dropping them would
    silently re-offer them."""
    prior = PriorInvestigation(
        investigation_id=4,
        investigation_type="Capacity",
        user_query="give me from a different DC",
        candidate_scores=[],
        exclude_data_centers=["Columbus-DC1", "Phoenix-DC1"],
    )
    assert excluded_data_centers("what other DCs?", prior) == [
        "Columbus-DC1",
        "Phoenix-DC1",
    ]


def test_an_ordinary_first_ask_still_excludes_nothing():
    """No carried exclusions and no location word: unchanged behaviour. An
    empty list means no exclusion the whole way down to the SQL, where an empty
    NOT IN would exclude everything."""
    prior = _prior_offering("Denver-DC1", "Atlanta-DC1")
    assert excluded_data_centers("what other options?", prior) == []
