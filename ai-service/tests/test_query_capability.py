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
        assert "does not record" in reply
        assert "never captured" in reply or "no column" in reply

    def test_it_names_the_attribute_that_does_exist(self):
        """A refusal that only says no leaves the reader guessing whether to
        rephrase or give up."""
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        assert "TechnologyPlatform" in reply

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

    def test_estate_figures_are_read_not_hard_coded(self, monkeypatch):
        """A hard-coded "eight data centres" is correct until somebody adds one,
        and then it is a figure this platform states confidently and wrongly -
        the exact failure the rest of the codebase exists to prevent."""
        qc.reset_estate_cache()
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda sql, *a, **k: [{"n": 3}] if "COUNT" in sql else [{"p": "Kubernetes"}],
        )
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        qc.reset_estate_cache()
        assert "3 data centres" in reply
        # The real estate has 8. Matching on the bare digit is not the check:
        # "128 GB RAM" in the example text contains one.
        assert "8 data centres" not in reply

    def test_an_unreadable_database_says_less_rather_than_guessing(self, monkeypatch):
        """No figure beats an invented one."""
        qc.reset_estate_cache()
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        reply = qc.capability_reply(
            "give me best dc for java apps", has_app_code=False, has_quantity=False
        )
        qc.reset_estate_cache()
        assert reply is not None
        assert "across the estate" in reply
        assert "data centres" not in reply.split("across the estate")[1][:40]


class TestThroughQuickReply:
    def test_end_to_end(self):
        from app.graph.nodes import quick_reply

        reply = quick_reply("give me best dc for java apps")
        assert reply is not None
        assert "TechnologyPlatform" in reply

    def test_datacentre_words_now_register_as_infrastructure(self):
        """"dc" appeared in none of _INFRA_INTENT_WORDS. The failing query only
        registered as infrastructure-shaped because "apps" contains "app"."""
        assert "dc" in qc.DATACENTRE_WORDS
        assert "datacentre" in qc.DATACENTRE_WORDS
