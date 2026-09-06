"""Follow-ups, and what they refer to.

The failure this covers: every chat message was an independent investigation.
"give me the options again" had nothing to point at, so it classified as a
general question, retrieved nothing, and answered "I don't have enough
grounded information" - a correct answer to a question nobody asked, while the
options it was asking for sat in the previous turn.

The detection tests are the important half. Resolution is deterministic on
purpose (the same trust boundary as routing: the LLM narrates, it does not
decide), which means its mistakes are silent - a query wrongly read as a
follow-up answers about the wrong infrastructure, confidently.
"""

from __future__ import annotations

import pytest

from app.graph.conversation import (
    ABOUT_PREVIOUS,
    INHERIT_SUBJECT,
    RECALL,
    PriorInvestigation,
    carry_subject,
    grounding_documents,
    looks_like_follow_up,
    resolve,
)
from app.graph.nodes import _capacity_requirement_from_regex, classify_investigation_type
from app.models.enums import InvestigationType

# =============================================================================
# Detection
# =============================================================================


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("give me the options again", RECALL),
        ("show me those options again", RECALL),
        ("what were the candidates?", RECALL),
        ("repeat that", RECALL),
        ("why was that rejected?", ABOUT_PREVIOUS),
        ("why not the second one?", ABOUT_PREVIOUS),
        ("what's the difference between them?", ABOUT_PREVIOUS),
        ("why did you pick that one", ABOUT_PREVIOUS),
        ("what about staging?", INHERIT_SUBJECT),
        ("and 128 GB RAM?", INHERIT_SUBJECT),
        ("in staging?", INHERIT_SUBJECT),
        ("try tier-1 instead", INHERIT_SUBJECT),
    ],
)
def test_a_follow_up_is_recognised_as_one(query, expected):
    assert looks_like_follow_up(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "Find the best clusters for hosting APP-CRM",
        "Which clusters are underutilized?",
        "I need 64 cores, 512 GB RAM and 4 TB storage",
        "show clusters",
        "hi",
    ],
)
def test_an_ordinary_request_is_not_dragged_into_the_previous_one(query):
    assert looks_like_follow_up(query) is None


def test_naming_an_application_ends_the_reference():
    """"as it" and "the same" are referential words, but this query says
    exactly which application it is about. Resolving it against the previous
    investigation would answer about the application the engineer just
    replaced - and would look right, because the prose would name a real
    cluster.
    """
    assert looks_like_follow_up("find hosting for APP-CRM in the same data center as it") is None


def test_a_named_forecast_is_not_a_recall():
    """"again" is a recall word, but this names the thing to run against.
    Reading it as "show me the previous shortlist" answers a question about
    CL-NYC-03 with results that may not mention CL-NYC-03 at all.
    """
    assert looks_like_follow_up("forecast capacity for CL-NYC-03 again") is None


def test_a_complete_question_is_not_a_continuation_just_because_it_opens_with_a_preposition():
    """"in staging?" continues the last request; "in production, which clusters
    are underutilized?" is a whole question. Carrying a subject into the second
    turns a right-sizing question into a placement request for an application
    nobody mentioned.
    """
    assert looks_like_follow_up("in production, which clusters are underutilized?") is None
    assert looks_like_follow_up("in staging?") == INHERIT_SUBJECT


# =============================================================================
# Carrying the subject forward
# =============================================================================


def _capacity_prior() -> PriorInvestigation:
    return PriorInvestigation(
        investigation_id=1, investigation_type="Capacity", user_query="I need 8 cores and 32 GB RAM",
        requirement={"cpu_cores": 8.0, "memory_gb": 32.0, "storage_gb": 500.0},
        candidate_scores=[{"cluster_code": "CL-A", "eligibility_status": "Eligible"}],
    )


def test_a_new_figure_beats_the_carried_one():
    """Extraction takes the *first* match for each dimension, so the user's
    words have to come before the carried subject. Reversed, "and with 128 GB
    RAM?" would be sized at the previous 32 GB and nothing anywhere would say
    the new number had been dropped.
    """
    carried = carry_subject("and with 128 GB RAM?", _capacity_prior())
    extracted = _capacity_requirement_from_regex(carried)["capacity_requirements"]
    assert extracted["memory_gb"] == 128.0    # what was just asked for
    assert extracted["cpu_cores"] == 8.0      # carried from the previous turn
    assert extracted["storage_gb"] == 500.0


def test_a_carried_subject_still_routes_to_the_same_investigation_type():
    """The carried wording is not decoration - it has to classify. A follow-up
    that degrades to a general question is the original bug wearing a hat.
    """
    app_prior = PriorInvestigation(
        investigation_id=1, investigation_type="Hosting",
        user_query="Find hosting for APP-CRM", application_code="APP-CRM",
    )
    assert classify_investigation_type(carry_subject("what about staging?", app_prior)) == InvestigationType.HOSTING
    assert classify_investigation_type(carry_subject("and 128 GB RAM?", _capacity_prior())) == InvestigationType.CAPACITY


def test_a_carried_figure_is_written_the_way_it_was_typed():
    """8.0 is stored as a float; "8.0 cores" is a needless difference from
    what the engineer wrote, re-read by the same regexes.
    """
    carried = carry_subject("what about staging?", _capacity_prior())
    assert "8 cores" in carried
    assert "8.0 cores" not in carried


# =============================================================================
# Resolution
# =============================================================================


def test_a_reference_with_nothing_to_refer_to_says_so():
    """The old behaviour was to run it as a fresh investigation and report that
    the context was empty. Saying "I have nothing to show you yet" is both
    truer and shorter.
    """
    resolution = resolve("give me the options again", None)
    assert resolution.kind == RECALL
    assert resolution.reply is not None
    assert "earlier result" in resolution.reply


def test_recalling_a_turn_that_produced_no_shortlist_says_that_instead():
    prior = PriorInvestigation(
        investigation_id=7, investigation_type="Question", user_query="why is nyc-03 busy?",
    )
    resolution = resolve("show me those options again", prior)
    assert resolution.reply is not None
    assert "not a shortlist" in resolution.reply


def test_a_continuation_with_no_subject_to_inherit_becomes_a_question_about_the_previous_turn():
    """Better to answer from what was said than to invent a subject: a
    continuation of a question is a question.
    """
    prior = PriorInvestigation(
        investigation_id=7, investigation_type="Question", user_query="why is nyc-03 busy?",
    )
    assert resolve("what about staging?", prior).kind == ABOUT_PREVIOUS


def test_an_ordinary_query_resolves_to_itself():
    resolution = resolve("Find the best clusters for hosting APP-CRM", _capacity_prior())
    assert resolution.kind is None
    assert resolution.resolved_query == "Find the best clusters for hosting APP-CRM"
    assert resolution.prior is None


# =============================================================================
# Grounding
# =============================================================================


def test_grounding_carries_the_reason_a_candidate_was_rejected():
    """"why was that rejected?" is answerable only if the rule that failed
    travels with the candidate. A vector search over the estate cannot supply
    it - it has no idea which run is meant.
    """
    prior = PriorInvestigation(
        investigation_id=3, investigation_type="Hosting", user_query="Find hosting for APP-CRM",
        application_code="APP-CRM",
        candidate_scores=[
            {
                "cluster_code": "CL-NYC-03", "eligibility_status": "Rejected", "rank": 4,
                "overall_score": None,
                "rule_results": [
                    {"name": "Availability tier", "passed": False,
                     "reason": "Tier-2 cluster cannot host a Tier-1 workload"},
                    {"name": "Platform", "passed": True, "reason": "Kubernetes matches"},
                ],
            }
        ],
    )
    docs = grounding_documents(prior)
    text = " ".join(d["text"] for d in docs)

    assert docs[0]["entity_type"] == "PriorInvestigation"
    assert "CL-NYC-03" in text
    assert "Tier-2 cluster cannot host a Tier-1 workload" in text
    assert "Kubernetes matches" not in text, "only the rules that failed explain a rejection"
    assert "APP-CRM" in text


def test_grounding_says_which_options_were_actually_shown():
    """"why not the second one?" is only answerable if the context records
    which candidates the engineer saw, and in what order. Without the
    positions, a real DeepSeek answer to that question began "the evidence does
    not explicitly identify which cluster that one refers to" - grounded,
    honest and no use at all.
    """
    prior = PriorInvestigation(
        investigation_id=5, investigation_type="Hosting", user_query="Find hosting for APP-CRM",
        application_code="APP-CRM",
        candidate_scores=[
            {"cluster_code": f"CL-{i}", "eligibility_status": "Eligible", "rank": i, "overall_score": 90 - i}
            for i in range(1, 6)
        ],
    )
    docs = grounding_documents(prior)
    headline, candidates = docs[0]["text"], docs[1:]

    assert "1. CL-1" in headline and "2. CL-2" in headline
    assert "shown to the engineer as option 2" in candidates[1]["text"]
    # Ranked past the shortlist, so it was never on screen - saying otherwise
    # would invite an answer about a cluster the engineer never saw.
    assert "not shown in the shortlist" in candidates[4]["text"]


def test_grounding_scores_are_not_similarity_scores():
    """These documents are not a search result - they are the thing the
    question is literally about, and must not be ranked below a vector hit.
    """
    prior = PriorInvestigation(
        investigation_id=3, investigation_type="Hosting", user_query="q",
        candidate_scores=[{"cluster_code": "CL-A", "eligibility_status": "Eligible", "rank": 1}],
    )
    assert all(d["score"] == 1.0 for d in grounding_documents(prior))


# =============================================================================
# End to end, against the live seeded database
#
# One real investigation is shared by every test below: the follow-ups being
# tested are cheap, but the investigation they follow costs a real LLM call
# per narration, and running it four times would burn the daily budget to
# prove the same thing four times.
# =============================================================================


@pytest.fixture(scope="module")
def chat(auth_employee_id):
    """A conversation with one hosting investigation already in it."""
    from app.graph.graph import run_investigation
    from app.repositories import conversation_repository

    conversation_id = conversation_repository.create(auth_employee_id)
    opening = run_investigation(
        query="Find the best clusters for hosting APP-CRM.",
        created_by=auth_employee_id,
        conversation_id=conversation_id,
    )
    return {"conversation_id": conversation_id, "opening": opening}


def _investigation_count(conversation_id: str) -> int:
    from app.repositories.base import T, fetch_one

    row = fetch_one(
        f"SELECT COUNT(*) AS n FROM {T('Investigation')} WHERE ConversationId = :id",
        {"id": conversation_id},
    )
    return int(row["n"])


def test_the_opening_message_starts_a_conversation(chat):
    assert chat["opening"]["conversation_id"] == chat["conversation_id"]
    assert chat["opening"]["investigation_id"] is not None


def test_a_recall_shows_the_same_investigation_rather_than_running_another(chat, auth_employee_id):
    """The point of "again" is that it is the same answer. Re-running would
    create a second Investigation row and, because utilization moves between
    requests, could hand back different numbers under the word "again".
    """
    from app.graph.graph import run_investigation

    before = _investigation_count(chat["conversation_id"])
    recalled = run_investigation(
        query="give me the options again",
        created_by=auth_employee_id,
        conversation_id=chat["conversation_id"],
    )

    assert recalled["investigation_id"] == chat["opening"]["investigation_id"]
    assert recalled["recall_of_investigation_id"] == chat["opening"]["investigation_id"]
    assert _investigation_count(chat["conversation_id"]) == before


def test_a_recalled_shortlist_is_still_decidable(chat, auth_employee_id):
    """A shortlist still awaiting a decision must come back live, not as a
    table that no longer does anything - the engineer asked to see it again
    because they are about to choose from it.
    """
    from app.graph.graph import run_investigation

    if chat["opening"]["status"] != "AwaitingReview":
        pytest.skip("opening investigation did not pause for review")

    recalled = run_investigation(
        query="show me those options again",
        created_by=auth_employee_id,
        conversation_id=chat["conversation_id"],
    )
    assert recalled["status"] == "AwaitingReview"
    assert recalled["review_payload"]["options"], "a recalled shortlist with no options is not a shortlist"


def test_a_question_about_the_previous_results_is_answered_from_them(chat, auth_employee_id):
    """This is the original failure. "why was that rejected?" used to retrieve
    nothing and answer that it had no grounded information; the answer was in
    the previous turn the whole time.
    """
    from app.graph.graph import get_investigation_state, run_investigation

    answer = run_investigation(
        query="why was that rejected?",
        created_by=auth_employee_id,
        conversation_id=chat["conversation_id"],
    )
    assert answer["investigation_id"] != chat["opening"]["investigation_id"]

    state = get_investigation_state(answer["investigation_id"])
    assert state["prior_investigation_id"] == chat["opening"]["investigation_id"]
    entity_types = {doc.get("entity_type") for doc in state["retrieved_context"]}
    assert "PriorInvestigation" in entity_types
    assert state["retrieved_context"][0]["entity_type"] == "PriorInvestigation", (
        "the previous investigation must not be displaced by a similarity hit"
    )


def test_both_sides_of_the_exchange_are_recorded(chat):
    from app.repositories import conversation_repository

    turns = conversation_repository.recent_turns(chat["conversation_id"], limit=50)
    roles = [t.Role for t in turns]

    assert roles[0] == "User"
    assert "Assistant" in roles
    assert turns[0].Message == "Find the best clusters for hosting APP-CRM."
    assert any(t.InvestigationId == chat["opening"]["investigation_id"] for t in turns)


def test_a_greeting_still_creates_no_investigation(chat, auth_employee_id):
    """quick_reply must keep working inside a conversation: "thanks" is not a
    follow-up and must not become a row.
    """
    from app.graph.graph import run_investigation

    before = _investigation_count(chat["conversation_id"])
    reply = run_investigation(
        query="thanks", created_by=auth_employee_id, conversation_id=chat["conversation_id"]
    )
    assert reply["investigation_id"] is None
    assert _investigation_count(chat["conversation_id"]) == before


def test_a_conversation_cannot_be_continued_by_another_employee(chat):
    """A conversation carries what someone asked and what they were shown.
    Serving one to a different employee because they supplied its id would be
    a plain information leak, which is why the ids are server-generated.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.repositories import employee_repository
    from app.security.jwt_service import create_local_token

    # A real, active employee: an unknown employee_id is rejected at
    # authentication (401), which would prove nothing about ownership. The
    # check being tested here is the next one along - authenticated, active,
    # and still not entitled to this conversation.
    colleague = employee_repository.get_by_id(2)
    assert colleague is not None and colleague.IsActive, "expected E1002 in the seed data"
    other = create_local_token(
        employee_id=colleague.EmployeeId, employee_number=colleague.EmployeeNumber,
        display_name=colleague.DisplayName, email=colleague.Email,
    )
    response = TestClient(app).post(
        "/api/investigations",
        json={"query": "give me the options again", "conversation_id": chat["conversation_id"]},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert response.status_code == 403
