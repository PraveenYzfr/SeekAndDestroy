"""What the platform will admit it cannot do.

Built from one real failure: "give me best dc for java apps" ran a full
investigation and returned a report that read as a retrieval miss for something
no retrieval could ever find - there is no runtime-language column in this CMDB.

The cases below are grouped by the way each one could go wrong again:

  * the specific query, end to end;
  * queries that must STILL reach the graph, because a guard that intercepts
    real investigations is worse than the bug it fixed;
  * the claims in the refusal text, which must stay true if the estate changes.
"""

from __future__ import annotations

import re

import pytest

from app.agents import query_capability as qc


class TestTheQueryThatFailed:
    def test_java_dc_query_is_intercepted(self):
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert reply is not None

    def test_it_says_the_data_does_not_exist_not_that_it_was_not_found(self):
        """The distinction the original report got wrong.

        "The evidence does not include" reads as a retrieval miss and sends the
        reader off to rephrase. The truth is structural - no column, ever - and
        those have opposite next steps.
        """
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        # Asserted as MEANING, not as the sentence. The wording changed when
        # the reply stopped explaining our machinery to the reader - it used to
        # say "a limit of what is recorded, not a search that came back empty",
        # which is the difference between a schema gap and an empty result set
        # and is nobody's business but ours.
        lowered = reply.lower()
        assert "don't record" in lowered or "does not track" in lowered
        assert "rewording won't help" in lowered or "rephrasing will not help" in lowered
        # THIS ASSERTION USED TO REQUIRE THE LEAK. It read
        #     assert "not a search that came back empty" in reply
        # and so pinned, as a requirement, the one sentence a reader should
        # never have seen - the difference between a schema gap and an empty
        # result set, which is our concern and not theirs. Praveen asked why the
        # platform was explaining its internals; this test was the reason it
        # kept doing so.
        #
        # The MEANING it was defending is real and is kept: do not send somebody
        # away to rephrase a question that can never work. That is now carried
        # by "rewording won't help" above.
        assert "came back empty" not in reply.lower()
        assert "result set" not in reply.lower()

    def test_it_names_the_attribute_that_does_exist(self):
        """A refusal that only says no leaves the reader guessing whether to
        rephrase or give up. Said in the reader's vocabulary, not the
        schema's - see TestARefusalDisclosesNothing."""
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert "hosting platform" in reply
        # The point is that it names something it DOES hold, so the reader has
        # somewhere to go. The phrasing is free to change.
        assert "hosting platform" in reply

    def test_it_asks_for_what_it_needs_to_rank(self):
        """The answerable half. "Best DC" is exactly what this platform does;
        only the java qualifier was impossible."""
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert "APP-CRM" in reply or "name the application" in reply

    def test_one_extra_word_no_longer_changes_the_outcome(self):
        """The root cause. quick_reply's guard fired at six words and this query
        is seven, so adding "java" turned a correct interception into a full
        investigation that could only report emptiness."""
        short = qc.capability_reply(
            "give me best dc for apps", has_app_code=False, has_quantity=False
        )
        long = qc.capability_reply(
            "give me the best data centre for our java applications please",
            has_app_code=False, has_quantity=False,
        )
        assert short is not None and long is not None


class TestItMustNotSwallowRealInvestigations:
    """A guard that eats real work is worse than the bug it fixed."""

    @pytest.mark.parametrize(
        "query, app_code, quantity",
        [
            ("Find the best clusters for hosting APP-ANALYTICS", True, False),
            ("best dc for a 32 core 128 GB RAM production app", False, True),
            ("best data centre for APP-CRM", True, False),
        ],
    )
    def test_a_placement_request_that_names_what_to_place_proceeds(
        self, query, app_code, quantity
    ):
        assert qc.capability_reply(
            query, has_app_code=app_code, has_quantity=quantity
        ) is None

    @pytest.mark.parametrize(
        "query",
        [
            "why was the incident on the payments cluster caused by a failed change?",
            "which clusters are underutilized?",
            "compare cmh-p212 and dal-03",
            "show clusters with at least 40% headroom",
        ],
    )
    def test_a_question_about_the_estate_is_not_a_placement_request(self, query):
        """These mention clusters, carry no app code and no quantity, and are
        perfectly answerable. The old length test protected them by accident;
        intent has to protect them on purpose."""
        assert not qc.has_placement_intent(query)
        assert qc.capability_reply(query, has_app_code=False, has_quantity=False) is None

    def test_asking_where_an_app_runs_today_is_a_lookup_not_a_placement(self):
        """"Which cluster is X on" is a fact to retrieve, not a choice to make."""
        assert not qc.has_placement_intent("which cluster is the payments app on today")


class TestTheClaimsStayTrue:
    def test_only_verified_absences_are_listed(self):
        """Claiming the platform cannot answer something it CAN is the same
        class of error as the reverse. Licence, patch level, compliance scope,
        power, latency and cost all have real columns and must not appear."""
        listed = {t for a in qc.UNMODELLED_ATTRIBUTES for t in a.terms}
        for present in ("licence", "license", "vendor", "patch", "compliance",
                        "pci", "power", "latency", "cost"):
            assert present not in listed

    def test_terms_match_on_word_boundaries(self):
        """"java" must not fire inside "javadoc", and ".net" must not fire
        inside "subnet"."""
        assert qc.unmodelled_attribute("check the javadoc build") is None
        assert qc.unmodelled_attribute("the subnet.network config") is None
        assert qc.unmodelled_attribute("our java services") is not None


class TestARefusalDisclosesNothing:
    """A refusal is the answer least likely to be reviewed, and this one used to
    name the backing store, name a column, list every platform recorded across
    the estate and state how many data centres exist - the last two read LIVE
    from production on the refusal path, so the leak stayed current.

    None of those facts is secret alone. The shape is the defect: one malformed
    question returned the platform inventory and the size of the estate. These
    tests pin the rule rather than the wording - name no table, no column, no
    enum value, no count.
    """

    QUERIES = (
        "give me best dc for java apps",
        "best data centre for our python services",
        "where should we host the dotnet estate",
    )

    @pytest.mark.parametrize("query", QUERIES)
    def test_no_schema_identifiers(self, query):
        reply = qc.capability_reply(query, has_app_code=False, has_quantity=False)
        assert reply is not None
        for leaked in ("TechnologyPlatform", "CmdbApplication", "InfrastructureCluster",
                       "CMDB", "column", "sad."):
            assert leaked not in reply, f"{leaked!r} disclosed in: {reply}"

    @pytest.mark.parametrize("query", QUERIES)
    def test_no_platform_inventory(self, query):
        """The sharpest part of the old leak: asking about Java returned the
        set of platforms the bank actually runs."""
        reply = qc.capability_reply(query, has_app_code=False, has_quantity=False)
        for platform in ("BareMetal", "Hyper-V", "Kubernetes", "OpenShift", "VMware"):
            assert platform not in reply, f"{platform!r} disclosed in: {reply}"

    @pytest.mark.parametrize("query", QUERIES)
    def test_no_estate_size(self, query):
        reply = qc.capability_reply(query, has_app_code=False, has_quantity=False)
        assert "across the estate" in reply
        assert not re.search(r"\d+\s+data\s+cent", reply), reply

    def test_the_refusal_path_issues_no_query(self, monkeypatch):
        """The disclosure was live because building it read production. Nothing
        on this path may touch the database at all - that also means a refusal
        still works when SQL Server is down."""
        def explode(*a, **k):
            raise AssertionError("the refusal path queried the database")

        monkeypatch.setattr("app.repositories.base.fetch_all", explode)
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert reply is not None

    def test_it_still_tells_the_reader_what_to_do(self):
        """Redaction must not turn a useful refusal into a blank no."""
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert "name the application" in reply
        assert "APP-CRM" in reply


class TestThroughQuickReply:
    def test_end_to_end(self):
        from app.graph.nodes import quick_reply

        reply = quick_reply("give me best dc for java apps")
        assert reply is not None
        assert "hosting platform" in reply
        assert "TechnologyPlatform" not in reply

    def test_datacentre_words_now_register_as_infrastructure(self):
        """"dc" appeared in none of _INFRA_INTENT_WORDS. The failing query only
        registered as infrastructure-shaped because "apps" contains "app"."""
        assert "dc" in qc.DATACENTRE_WORDS
        assert "datacentre" in qc.DATACENTRE_WORDS


class TestWordsNotSubstrings:
    """Four incident lookups in the hundred-case run were refused because
    "happened" contains "app".

    _INFRA_INTENT_WORDS was matched with `in`, so any query containing an
    ordinary English word with an infrastructure noun buried in it looked
    infrastructure-shaped. "What happened in INC1009985?" got "I need a bit more
    to work with" - a whole query class, silently unavailable.
    """

    @pytest.mark.parametrize("text, word", [
        ("what happened in INC1009985", "app"),
        ("apparently the change failed", "app"),
        ("apply the recommendation", "app"),
        ("a ghost in the machine", "host"),
        ("replace the disk", "place"),
    ])
    def test_a_buried_noun_is_not_a_mention(self, text, word):
        assert not qc.mentions_any(text, (word,))

    @pytest.mark.parametrize("text", ["java apps", "the application", "hosting for it",
                                      "cluster capacity", "which datacentre"])
    def test_real_mentions_still_match(self, text):
        """Uses the REAL word list, not a hand-picked one.

        An earlier version of this test passed ("app", "host", ...) and failed on
        "the application" - correctly, because "application" is not "app" plus an
        inflection. The fix was to list "application" in _INFRA_INTENT_WORDS
        rather than to widen the matcher back into matching "apply", and a test
        with its own private word list would not have shown that.
        """
        from app.graph.nodes import _INFRA_INTENT_WORDS

        assert qc.mentions_any(text, _INFRA_INTENT_WORDS + qc.DATACENTRE_WORDS)

    def test_an_incident_lookup_reaches_the_graph(self):
        """The regression, end to end."""
        from app.graph.nodes import quick_reply

        assert quick_reply("What happened in INC1009985?") is None
