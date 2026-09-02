"""Out-of-scope questions must not reach the graph.

"Who is the best actor in India?" produced an Investigation row, a real model
call, a retrieval, and a report explaining at High confidence that
dal-p056-NODE-01 contains no information about actors. The pipeline behaved
correctly at every step; the question should have stopped before it.

The bias throughout is one-directional: **a wrongly-refused real question is far
worse than a wrongly-answered odd one.** A bad answer is visible and arguable. A
refusal tells an engineer the tool does not work, and they stop using it. So the
in-scope cases below far outnumber the out-of-scope ones, and the ones drawn
from live CMDB codes are the ones that matter most.
"""

from __future__ import annotations

import pytest

from app.graph import scope


class TestOutOfScope:
    @pytest.mark.parametrize(
        "query",
        [
            "Who is the best actor in India?",
            "What is the capital of France?",
            "Write me a poem about the sea",
            "who won the world cup",
            "What is 2 + 2?",
            "Tell me a joke",
            "translate good morning into spanish",
            "what do you think about the election",
        ],
    )
    def test_a_question_with_nothing_from_the_estate_in_it_is_refused(self, query):
        assert scope.out_of_scope_reply(query) is not None, query

    def test_the_reply_says_what_it_is_for_and_gives_copyable_examples(self):
        """Naming only what it refuses leaves the asker guessing, which is how
        someone decides the tool is useless.

        This used to require all five investigation types by name, each with its
        own description and example. Praveen read that version and called it
        clumsy, correctly: it was a manual delivered at the moment somebody
        wanted a redirect. The property that matters is not "every capability is
        listed" but "the asker can see what this is for and copy a working
        question", so that is what is asserted now.
        """
        reply = scope.OUT_OF_SCOPE_REPLY.lower()
        assert "infrastructure" in reply, "the reply never says what domain it covers"
        # NOT a specific application code. The examples originally named APP-CRM,
        # which was the wrong shape entirely: an application that already has a
        # code is already hosted, so "where should APP-CRM go" is a question
        # nobody has. A placement example has to describe a workload - tier,
        # platform, size - the way somebody with something new to place would.
        assert reply.count('"') >= 4, "fewer than two quoted examples to copy"
        assert any(w in reply for w in ("cores", "gb", "tier-1")), (
            "no example describes a workload by its requirements"
        )

    def test_the_reply_stays_short(self):
        """The reason the long version failed. A refusal is read in a second or
        not at all, and six bullets is a page. Kept as a test rather than a
        comment because the natural pressure on this string is to grow: every
        new investigation type will look like it deserves a line."""
        assert len(scope.OUT_OF_SCOPE_REPLY) < 400, (
            f"refusal is {len(scope.OUT_OF_SCOPE_REPLY)} chars - it is turning back into a manual"
        )


class TestInScope:
    @pytest.mark.parametrize(
        "query",
        [
            # identifiers
            "why was atl-03 rejected for APP-ANALYTICS",
            "status of cmh-p212",
            "what happened on atl-03-NODE-07",
            "tell me about INC1005432",
            # quantities, with no domain word at all
            "I need 64 cores and 512 GB",
            "somewhere with 4 TB free",
            # vocabulary
            "which clusters are underutilized",
            "where should this workload go",
            "show me the incident history",
            "what is the forecast for next quarter",
            "anything that can be consolidated",
            "how much headroom is left in production",
        ],
    )
    def test_anything_estate_shaped_reaches_the_graph(self, query):
        assert scope.out_of_scope_reply(query) is None, query

    def test_every_real_cluster_application_and_node_code_is_recognised(self):
        """Checked against the live CMDB, not against invented examples.

        This is the test that matters. A pattern that looks right but misses the
        naming convention would refuse real questions about real infrastructure,
        and no amount of hand-written examples would reveal it - the previous
        _CLUSTER_CODE_RE matched `CL-PROD-01` and zero of the 256 real clusters.
        """
        from app.repositories import application_repository, cluster_repository, node_repository

        clusters = cluster_repository.list_all(limit=5000)
        apps = application_repository.list_all(limit=5000)
        assert clusters and apps, "no CMDB data - this test proves nothing without it"

        unrecognised = [c.ClusterCode for c in clusters if not scope.has_estate_signal(f"why was {c.ClusterCode} rejected")]
        assert not unrecognised, f"cluster codes not recognised: {unrecognised[:10]}"

        unrecognised = [a.ApplicationCode for a in apps if not scope.has_estate_signal(f"where should {a.ApplicationCode} go")]
        assert not unrecognised, f"application codes not recognised: {unrecognised[:10]}"

        nodes = node_repository.get_by_cluster(clusters[0].ClusterId, limit=20)
        unrecognised = [n.HostName for n in nodes if not scope.has_estate_signal(f"status of {n.HostName}")]
        assert not unrecognised, f"node names not recognised: {unrecognised[:10]}"


class TestMatchingIsNotAccidental:
    def test_domain_words_match_whole_tokens_not_substrings(self):
        """A substring check finds "app" inside "happening" and "node" inside
        "anode", which would let almost anything through and quietly disable the
        gate in a way no in-scope test would catch."""
        assert scope.has_estate_signal("what is happening") is False
        assert scope.has_estate_signal("explain an anode") is False
        assert scope.has_estate_signal("is this a scam") is False

    def test_hyphenated_domain_words_survive_tokenisation(self):
        assert scope.has_estate_signal("can we right-size anything") is True
        assert scope.has_estate_signal("bare-metal options") is True

    def test_an_empty_query_is_not_treated_as_in_scope(self):
        assert scope.has_estate_signal("") is False
        assert scope.has_estate_signal("   ") is False

    def test_case_does_not_matter(self):
        assert scope.has_estate_signal("WHY WAS ATL-03 REJECTED") is True
        assert scope.has_estate_signal("app-analytics") is True


# =============================================================================
# Plurals - the gap that refused "what other DCs?"
# =============================================================================
#
# The vocabulary listed some plurals and not others: `cluster clusters`,
# `node nodes`, but `dc` with no `dcs` and `site` with no `sites`. That is worse
# than a short list, because the list looks complete - nobody reads it and
# thinks "the plurals are missing".
#
# Found in production: Praveen rejected a recommendation, asked for another data
# centre, and was told "I answer infrastructure questions only".


class TestPluralsAreNotOutOfScope:
    @pytest.mark.parametrize(
        "query",
        [
            "what other DCs?",
            "give me from a different DC",
            "other regions?",
            "which zones?",
            "what platforms are available",
            "other sites",
            "different racks",
            "which tiers?",
        ],
    )
    def test_a_plural_estate_word_is_in_scope(self, query):
        assert scope.has_estate_signal(query), f"{query!r} was refused as off-topic"

    def test_the_singular_still_works(self):
        assert scope.has_estate_signal("which DC?")

    @pytest.mark.parametrize(
        "query",
        [
            "who is the best actor in India?",
            "what is the weather",
            "tell me a joke",
            "what is 2+2",
            "how are the markets today",
        ],
    )
    def test_off_topic_is_still_refused(self, query):
        """Stemming must not become a way for anything to pass. It only ever
        tests membership in a curated set, so an over-eager stem cannot admit a
        word that is not in it."""
        assert not scope.has_estate_signal(query)

    def test_stemming_does_not_invent_matches(self):
        """"as" stems to "a", "is" stems to "i" - neither is in any vocabulary.
        The stem is only ever a lookup, never a guess."""
        assert not scope.has_estate_signal("as is was")
