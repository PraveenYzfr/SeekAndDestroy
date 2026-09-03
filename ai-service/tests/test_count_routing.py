"""Estate count questions: "how many servers do we have".

THE FAILURE THESE EXIST FOR
---------------------------
A real query, typed into the chat:

    good.. How many servers we have in our db

came back as a full investigation - roughly 150 seconds and two model calls -
concluding:

    I have no record of how many servers are in the database.

with next steps advising the reader to "query the database management system to
retrieve the current server count" and "update the knowledge base". At that
moment sad.ConfigurationItem held 10,943 rows of ClassName 'cmdb_ci_server'.

Three separate defects stacked, and only the third is about the model:

1. The CMDB Insighter - a whole SQL counting layer with a security whitelist -
   had exactly one caller, api/routes_insights.py. The chat graph never called
   it. A counting engine existed and the chat could not reach it.
2. Its whitelist had four entities: incident, change, problem, hosting. All
   four describe what HAPPENED to the estate. None describes what IS THERE, so
   "how many servers" was not expressible even if routing had worked.
3. So the question fell through to retrieval, which cannot count by
   construction - it returns the top-k chunks most similar to a question, and
   no chunk contains a total. The model was handed nothing and said so. It was
   the only honest participant in the failure.

The next steps were the worst part: generic ITSM advice, generated to fill a
report section that had nothing real in it, telling the reader to go and do by
hand the thing the platform exists to do. And it passed number fidelity
cleanly, because there were no numbers to drift. The guard is fine; it does not
cover confidently asserting the absence of data the platform owns.
"""

from __future__ import annotations

import pytest

from app.insights import router as insights_router
from app.insights.whitelist import (
    CI_CLASS_DISPLAY,
    CI_CLASS_SYNONYMS,
    ENTITIES,
    dimension_labels,
    normalize_ci_class,
)


class TestTheQuestionIsRecognised:
    @pytest.mark.parametrize(
        "query",
        [
            "How many servers we have in our db",
            "good.. How many servers we have in our db",  # the verbatim failing query
            "how many VMs are in production",
            "number of clusters",
            "how many Sev1 incidents last month",
            "count of applications by data centre",
        ],
    )
    def test_a_counting_question_is_recognised(self, query):
        assert insights_router.has_count_intent(query)

    @pytest.mark.parametrize(
        "query",
        [
            # A quantity of RESOURCE for one workload, not a quantity of things
            # in the estate. The distinction is what the negative lookahead
            # carries, and getting it wrong would route every capacity request
            # into a counting layer that cannot size anything.
            "how many cores does APP-CRM need",
            "how many GB of memory should I allocate",
            # No count intent at all.
            "find hosting for APP-CRM",
            "give me a different DC",
            # Count intent, nothing countable - "how many times" is a complaint.
            "how many times do I have to ask",
        ],
    )
    def test_a_question_that_is_not_a_count_is_left_alone(self, query):
        assert not insights_router.has_count_intent(query)

    def test_intent_needs_both_halves(self):
        """"How many" alone matches idiom; a countable noun alone matches every
        hosting request ever written. It is the conjunction that identifies a
        counting question, and either half on its own is a false positive."""
        assert not insights_router.has_count_intent("how many, roughly")
        assert not insights_router.has_count_intent("the servers in Denver")


class TestTheFastPathSpendsNoModelCall:
    """A bare "how many X" is a dictionary lookup and a COUNT.

    Routing it through the spec parser cost two provider calls and about twelve
    seconds to map one noun onto one class name and read a single integer
    aloud. Both calls are also failure surface: a provider outage turned a
    question the database answers in milliseconds into no answer at all.
    """

    @pytest.mark.parametrize(
        "query,expected_class",
        [
            ("How many servers we have in our db", "cmdb_ci_server"),
            ("good.. How many servers we have in our db", "cmdb_ci_server"),
            ("how many VMs do we have", "cmdb_ci_vm_instance"),
            ("number of clusters", "cmdb_ci_cluster"),
            ("how many databases are there", "cmdb_ci_db_instance"),
        ],
    )
    def test_a_bare_count_resolves_to_a_class_without_a_model(
        self, query, expected_class, monkeypatch
    ):
        seen: dict = {}

        def fake_run_query(spec):
            seen["spec"] = spec
            return {"total_count": 10943, "rows": [], "group_by": [], "filters": spec.filters,
                    "distinct_groups": 1}

        monkeypatch.setattr(insights_router, "run_query", fake_run_query)
        result = insights_router.simple_count(query)

        assert result is not None, "should not have needed the parser"
        assert seen["spec"].entity == "ci"
        assert seen["spec"].filters["ci_class"] == [expected_class]

    @pytest.mark.parametrize(
        "query",
        [
            # Every one of these carries a CONDITION, which the fast path
            # cannot express and must hand to the parser rather than quietly
            # dropping - answering "how many servers in production" with the
            # total for every environment is a wrong answer, not a rounding.
            "how many servers in production",
            "how many VMs by data centre",
            "how many Sev1 incidents last month",
            "how many servers are missing an owner",
        ],
    )
    def test_anything_with_a_condition_defers_to_the_parser(self, query):
        assert insights_router.simple_count(query) is None

    def test_an_unmapped_noun_defers_rather_than_counting_everything(self, monkeypatch):
        """A countable thing with no CI class - "how many incidents" - belongs
        to the parser and its own entity. Falling back to an unfiltered count
        would answer a different question with a confident number."""
        monkeypatch.setattr(
            insights_router, "run_query",
            lambda spec: pytest.fail("must not query for an unmapped noun"),
        )
        assert insights_router.simple_count("how many incidents do we have") is None


class TestServersAreNotVMs:
    """The single most likely way this feature could be confidently wrong.

    cmdb_ci_server and cmdb_ci_vm_instance are different rows - 10,943 and
    30,105 of them. Folding VMs into a server count would produce a number that
    is wrong by a factor of four, delivered in a sentence that sounds certain.
    """

    def test_server_and_vm_map_to_different_classes(self):
        assert CI_CLASS_SYNONYMS["servers"] != CI_CLASS_SYNONYMS["vms"]
        assert CI_CLASS_SYNONYMS["servers"] == "cmdb_ci_server"
        assert CI_CLASS_SYNONYMS["vms"] == "cmdb_ci_vm_instance"

    def test_a_server_count_says_that_vms_are_excluded(self, monkeypatch):
        """Stated before the reader acts on the figure, not after they notice
        it does not reconcile with a hypervisor inventory."""
        monkeypatch.setattr(
            insights_router, "run_query",
            lambda spec: {"total_count": 10943, "rows": [], "group_by": [],
                          "filters": spec.filters, "distinct_groups": 1},
        )
        result = insights_router.simple_count("how many servers do we have")
        assert any("irtual machine" in c for c in result["caveats"]), result["caveats"]

    def test_the_figure_is_never_bare(self, monkeypatch):
        """GUARDRAILS: never a bare number. The answer states WHICH class was
        counted, because "server" and "VM" are different questions."""
        monkeypatch.setattr(
            insights_router, "run_query",
            lambda spec: {"total_count": 10943, "rows": [], "group_by": [],
                          "filters": spec.filters, "distinct_groups": 1},
        )
        result = insights_router.simple_count("how many servers do we have")
        assert "10,943" in result["headline"]
        assert "cmdb_ci_server" in result["narrative"]

    def test_the_reader_is_not_shown_a_raw_plural(self):
        """Echoing the reader's own noun produced "30,105 vms." - correct and
        sloppy. A count often gets pasted into a change record."""
        assert CI_CLASS_DISPLAY["cmdb_ci_vm_instance"] == "VMs"


class TestTheEstateEntity:
    def test_ci_is_registered(self):
        assert "ci" in ENTITIES

    def test_it_counts_configuration_items(self):
        assert ENTITIES["ci"].table == "ConfigurationItem"

    def test_no_identity_or_free_text_dimension(self):
        """Name and SysId identify a specific box; nobody groups by them, and
        they are the CI equivalent of ShortDescription - RAG's evidence, never
        a SQL dimension."""
        columns = {d.column for d in ENTITIES["ci"].dimensions.values()}
        for forbidden in ("ci.Name", "ci.SysId", "ci.CiId"):
            assert forbidden not in columns

    def test_the_date_filter_is_when_a_ci_appeared_not_when_it_was_scanned(self):
        """LastDiscovered moves every time discovery runs, so a date filter on
        it would mean "scanned recently" - a staleness question wearing the
        same words as an inventory one."""
        assert ENTITIES["ci"].date_column == "ci.FirstDiscovered"

    def test_an_unrecognised_class_passes_through_rather_than_being_dropped(self):
        """Silently dropping it would turn a filter the reader asked for into a
        count of the whole estate. Passed through, validate_spec refuses it."""
        assert normalize_ci_class("widgets") == "widgets"


class TestARefusalNamesNoSchema:
    """The disclosure rule established on 2026-09-04, applied to this layer.

    A refusal may say what the platform CANNOT do; it must not teach the reader
    the schema or enumerate what the estate contains. Saying what can be
    grouped is the vocabulary of the question; listing every platform in the
    estate is inventory.
    """

    def test_labels_are_reader_facing_not_column_names(self):
        labels = dimension_labels("ci")
        assert "data classification" in labels
        for leaked in ("DataClassification", "ClassName", "OperationalStatus", "ci."):
            assert not any(leaked in label for label in labels), labels

    def test_labels_exist_for_every_dimension(self):
        """A dimension added without a label would fall back to something
        schema-shaped in a user-facing refusal."""
        assert len(dimension_labels("ci")) == len(ENTITIES["ci"].dimensions)


class TestFailureLeavesTheReaderWhereTheyWere:
    """This intercepts questions that already reached the graph and got a poor
    answer. Replacing a poor answer with a stack trace is a regression, so an
    unknown failure degrades to the previous behaviour.

    The cost of that is a silent failure indistinguishable from the original
    defect, which is why every outcome increments sad_count_routing_total -
    container logs on this platform are destroyed by every deploy, so a log
    line alone would not survive long enough to diagnose anything.
    """

    def test_an_unknown_failure_falls_through_to_the_graph(self, monkeypatch):
        from app.graph import graph as graph_module

        def explode(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(insights_router, "simple_count", explode)
        monkeypatch.setattr(insights_router, "answer_free_text", explode)
        assert graph_module._counted_answer("how many servers do we have", None) is None

    def test_a_refused_spec_is_explained_rather_than_swallowed(self, monkeypatch):
        """The one failure that can be described precisely: the parser named a
        dimension this layer does not have. The reader gets the vocabulary,
        not "rephrase and try again"."""
        from app.graph import graph as graph_module
        from app.insights.query_builder import InsightValidationError

        monkeypatch.setattr(insights_router, "simple_count", lambda q: None)
        monkeypatch.setattr(
            insights_router, "answer_free_text",
            lambda *a, **k: (_ for _ in ()).throw(InsightValidationError("Unknown dimension 'colour'")),
        )
        reply = graph_module._counted_answer("how many servers by colour", None)

        assert reply is not None, "a describable failure must not fall through silently"
        text = reply["final_report"]["executive_summary"]
        assert "data classification" in text
        assert "colour" not in text or "Unknown dimension" not in text
        # And it still says no schema - same rule as the refusal tests above.
        for leaked in ("DataClassification", "ConfigurationItem", "ClassName"):
            assert leaked not in text


class TestACountIsNotAnInvestigation:
    def test_no_investigation_row_is_created(self, monkeypatch):
        """Nothing was investigated - a table was counted. Keeping counts out
        of the Investigation table keeps the investigation list a list of
        investigations, and keeps the evaluation tables from being asked to
        grade narration against evidence that does not exist."""
        from app.graph import graph as graph_module

        monkeypatch.setattr(
            insights_router, "simple_count",
            lambda q: {"headline": "10,943 servers.", "narrative": "n", "insight": "",
                       "caveats": [], "table": None, "filters_applied": {},
                       "row_count": 1, "total_count": 10943, "deterministic": True},
        )
        reply = graph_module._counted_answer("how many servers do we have", None)
        assert reply["investigation_id"] is None
        assert reply["investigation_type"] == "Count"

    def test_a_deterministic_answer_says_so(self, monkeypatch):
        """A number produced without a model is a stronger claim than the same
        number with one, and the caller is entitled to know which it has."""
        from app.graph import graph as graph_module

        monkeypatch.setattr(
            insights_router, "simple_count",
            lambda q: {"headline": "256 clusters.", "narrative": "n", "insight": "",
                       "caveats": [], "table": None, "filters_applied": {},
                       "row_count": 1, "total_count": 256, "deterministic": True},
        )
        assert graph_module._counted_answer("number of clusters", None)["deterministic"] is True
