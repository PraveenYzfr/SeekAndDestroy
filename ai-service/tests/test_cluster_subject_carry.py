"""A follow-up about a cluster has to keep the cluster.

THE CONVERSATION THIS EXISTS FOR
--------------------------------
An engineer asked about msp-p194 and then asked three ordinary follow-ups:

    explain me more about msp-p194              -> correct answer
    is it stable enough for a production hosting ?
    what are the incidents talk about ?
    you said two incidents ? now threee huh ?

The platform answered with four incidents - INC1009430, INC1004913, INC1002631
on msp-p204, and INC1003924 on dal-p044. msp-p194 has ZERO incidents. It said
"two", then "three", then insisted "the correct count is three, not two". None
of those numbers was right, and the count moved because every turn re-ran
retrieval on a different query.

RETRIEVAL WAS NOT AT FAULT, which is the part worth stating because it is where
the obvious fix would have gone. Hybrid search is on, BM25 stats are fitted, and
msp-p194 tokenises to three sparse terms; given "msp-p194 incidents" it returns
msp-p194 documents at the top. It was never given the code. The follow-ups are
pronouns - "is IT stable" - so the search matched on "stability" and
"production" and returned whatever incident prose was nearest.

carry_subject knew how to carry an application code and a capacity requirement.
A cluster is neither, so there was nothing to carry.

AND ABOUT_PREVIOUS DID NOT SAVE IT. That branch grounds on the previous run's
candidate evidence instead of re-retrieving, which is right for "why was that
rejected?" - but when the previous turn was itself a Question there IS no
candidate list, prior_context_docs comes back empty, and retrieval falls through
to a similarity search over the bare pronoun text.

A KNOWN REMAINING GAP, stated rather than left to be discovered: "what are the
incidents talk about ?" still classifies as kind=None. It carries no referential
pronoun, and _REFERENTIAL_RE deliberately excludes bare "the incidents" because
that phrase begins plenty of complete first questions. Widening it would make
every bare noun-phrase question a follow-up. That turn is still unanchored.
"""

from __future__ import annotations

import pytest

from app.graph import conversation as c


def _prior(query: str = "explain me more about msp-p194", **kw) -> c.PriorInvestigation:
    return c.PriorInvestigation(
        investigation_id=125,
        investigation_type=kw.pop("investigation_type", "Question"),
        user_query=query,
        status="Completed",
        **kw,
    )


class TestTheClusterIsRecognisedAsASubject:
    def test_a_cluster_code_in_the_opening_question_is_the_subject(self):
        assert _prior().cluster_subject == "msp-p194"

    def test_a_conversation_about_nothing_in_particular_has_no_subject(self):
        assert _prior("what clusters are underutilised?").cluster_subject is None

    def test_the_subject_comes_from_the_QUESTION_not_the_candidates(self):
        """A right-sizing run names dozens of clusters and none of them is what
        the conversation is about. The code the engineer typed is."""
        prior = _prior(
            "which 3 clusters are the best right-sizing candidates",
            candidate_scores=[{"cluster_code": "den-p096"}, {"cluster_code": "phx-p167"}],
        )
        assert prior.cluster_subject is None


class TestAPronounFollowUpKeepsTheCluster:
    @pytest.mark.parametrize(
        "query",
        [
            "is it stable enough for a production hosting ?",
            "you said two incidents ? now threee huh ?",
        ],
    )
    def test_the_cluster_is_carried_into_the_resolved_query(self, query):
        resolution = c.resolve(query, _prior())
        assert resolution.resolved_query != query, "the subject was dropped"
        assert "msp-p194" in resolution.resolved_query

    def test_the_users_words_come_first(self):
        """Extraction takes the FIRST match for each dimension, so a carried
        subject appended after the question must not displace what was asked."""
        query = "is it stable enough for a production hosting ?"
        resolved = c.resolve(query, _prior()).resolved_query
        assert resolved.startswith(query)

    def test_it_is_phrased_as_a_topic_not_as_a_placement_request(self):
        """classify_investigation_type reads these tokens. Phrasing the carry as
        "find hosting for" would turn "is it stable?" into a placement run for a
        cluster that already exists - which is precisely the drift that ended
        this conversation with a shortlist of twelve unrelated clusters."""
        resolved = c.resolve("is it stable ?", _prior()).resolved_query
        assert "find hosting" not in resolved.lower()
        assert "about cluster" in resolved


class TestWhatMustNotBeCarried:
    def test_a_question_naming_its_own_cluster_is_not_a_follow_up(self):
        query = "explain me more about msp-p194"
        assert c.resolve(query, _prior()).resolved_query == query

    def test_a_question_naming_its_own_application_is_not_a_follow_up(self):
        query = "find hosting for APP-CRM"
        assert c.resolve(query, _prior()).resolved_query == query

    def test_an_application_subject_still_wins_over_a_cluster(self):
        """An application is the stronger referent: in "find hosting for
        APP-CRM" the clusters are the answer, not the topic."""
        prior = _prior("find hosting for APP-CRM", application_code="APP-CRM")
        resolved = c.resolve("what about in staging?", prior).resolved_query
        assert "APP-CRM" in resolved

    def test_no_prior_means_nothing_is_invented(self):
        resolution = c.resolve("is it stable ?", None)
        assert resolution.resolved_query == "is it stable ?"


class TestThePlatformCanAcceptTheAnswerToItsOwnQuestion:
    """A real conversation, session 60301f49:

        user  get me a best server
        SAD   I need a bit more to work with. Either name the application or
              give me the resources it needs.
        user  for java app
        SAD   I don't have an earlier result in this conversation to refer back to.
        user  its a brand new java app
        SAD   I don't have an earlier result in this conversation to refer back to.

    A closed loop. The engineer answered the platform's own question, twice, and
    was told there was nothing to refer back to - with no way out except
    abandoning the thread and typing one fully-formed sentence.

    THE CAUSE. That clarifying reply comes from quick_reply, which deliberately
    creates NO Investigation row: "hi" should not produce one. So prior is None,
    "for java app" resolves as INHERIT_SUBJECT with nothing to inherit, and
    _NO_REFERENT fires. "No prior INVESTIGATION" and "no prior CONVERSATION" are
    different facts, and the message conflated them.

    Declining to assert a referent that does not exist is not a guess about
    intent. It hands the turn back to quick_reply and capability_reply, which
    answer it properly.
    """

    @pytest.mark.parametrize(
        "query", ["for java app", "its a brand new java app", "and with 128 GB RAM", "what about staging?"]
    )
    def test_a_follow_up_with_no_prior_is_not_refused(self, query):
        resolution = c.resolve(query, None)
        assert resolution.reply is None, "the platform must not claim amnesia about its own question"
        assert resolution.kind is None, "with nothing to refer to, it is an ordinary query"

    def test_the_query_reaches_the_pipeline_unchanged(self):
        """Nothing is inferred or appended - there is no prior to draw on, and
        inventing one would be worse than the refusal it replaces."""
        assert c.resolve("for java app", None).resolved_query == "for java app"

    @pytest.mark.parametrize(
        "query", ["show me those options again", "give me the options again", "repeat that"]
    )
    def test_recall_with_no_prior_still_refuses(self, query):
        """Here the refusal is true and useful: a first message asking to repeat
        a shortlist has no shortlist to repeat, and saying so is the entire
        point of _NO_REFERENT. This fix must not take that with it."""
        resolution = c.resolve(query, None)
        assert resolution.kind == c.RECALL
        assert resolution.reply is not None
        assert "earlier result" in resolution.reply

    def test_the_java_turns_reach_the_capability_refusal(self):
        """The honest answer to "for java app" is not a recall failure - it is
        that this CMDB records a hosting platform and not a language. Proving
        the turn reaches that path, rather than merely no longer being refused
        for the wrong reason."""
        from app.graph.nodes import quick_reply

        for query in ("for java app", "its a brand new java app"):
            assert c.resolve(query, None).reply is None
            answer = quick_reply(query)
            assert answer is not None, f"{query!r} fell through to a full investigation"
            assert "runtime language" in answer
