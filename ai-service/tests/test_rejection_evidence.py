"""'Why was X rejected for Y' must be answered by the rules, not by the index.

A rejection is the output of app.rules.eligibility - a rule id, a pass/fail and
a written reason. It was being answered from the vector store, so the narration
could disagree with the engine that actually rejected the cluster and nothing
would flag the contradiction. These tests pin the evidence to the rules.

They use the real database, like the other repository-backed tests, but never
an embedding provider or an LLM: _rejection_rule_evidence is deterministic.
"""

from __future__ import annotations

import pytest

from app.graph.nodes import (
    _CLUSTER_CODE_RE,
    _REJECTION_QUESTION_RE,
    _rejection_rule_evidence,
)
from app.repositories import application_repository, cluster_repository
from app.services import placement


@pytest.fixture(scope="module")
def rejected_pair():
    """A real (application, cluster) pair the engine actually rejects.

    Discovered rather than hardcoded: which clusters fail for which application
    depends on seeded utilization, and a hardcoded pair would start passing for
    the wrong reason the day the seed changes.
    """
    app = application_repository.list_all(limit=5)[0]
    requirement = placement.requirement_for_application(app)
    for cluster in cluster_repository.list_all(limit=80):
        candidate, _ = placement.evaluate_candidate(requirement, cluster)
        if candidate.eligibility_status == "Rejected":
            return app, cluster
    pytest.skip("no rejected cluster in the seeded data")


def _query(state_query: str) -> dict:
    return {"user_query": state_query, "resolved_query": None}


class TestClusterCodeRegex:
    """The previous pattern was `\\bCL-[A-Z0-9-]+\\b` and matched 0 of 256
    clusters. It had never fired, which is why a cluster named in a query was
    invisible to forecasting and to this feature."""

    @pytest.mark.parametrize("code", ["atl-03", "msp-09", "cmh-p212", "nyc-p005", "dal-p043"])
    def test_it_matches_the_codes_the_cmdb_actually_uses(self, code):
        assert _CLUSTER_CODE_RE.search(f"why was {code} rejected") is not None

    def test_it_no_longer_matches_the_shape_that_never_existed(self):
        assert _CLUSTER_CODE_RE.search("CL-WEST-01") is None

    def test_a_node_name_yields_its_cluster(self):
        """"why was cmh-p212-NODE-04 rejected" is a question about the cluster
        that host belongs to; returning the cluster is the useful reading."""
        assert _CLUSTER_CODE_RE.search("cmh-p212-NODE-04").group(0) == "cmh-p212"

    def test_matching_is_case_insensitive(self):
        """Callers search both the raw query and query.upper()."""
        assert _CLUSTER_CODE_RE.search("CMH-P212") is not None


class TestRejectionQuestionDetection:
    @pytest.mark.parametrize(
        "q",
        [
            "Why was atl-03 rejected for APP-ANALYTICS?",
            "why is cmh-p212 not eligible for APP-CRM",
            "Why was nyc-p005 excluded for APP-ANALYTICS?",
            "why was dal-p043 ruled out for APP-CRM",
        ],
    )
    def test_rejection_phrasings_are_recognised(self, q):
        assert _REJECTION_QUESTION_RE.search(q) is not None

    @pytest.mark.parametrize(
        "q",
        [
            "Find the best clusters for hosting APP-ANALYTICS.",
            "Why is cmh-p212 the top recommendation?",
            "Which clusters are underutilized?",
        ],
    )
    def test_other_questions_are_not(self, q):
        """A false positive here would run a placement evaluation for a
        question that did not ask for one."""
        assert _REJECTION_QUESTION_RE.search(q) is None


class TestRejectionEvidence:
    def test_the_verdict_comes_from_the_rules(self, rejected_pair):
        app, cluster = rejected_pair
        docs = _rejection_rule_evidence(_query(f"Why was {cluster.ClusterCode} rejected for {app.ApplicationCode}?"))
        assert docs, "no rule evidence produced for a genuinely rejected pair"
        assert docs[0]["entity_type"] == "eligibility_verdict"
        # Case-insensitive: this asserted "Rejected" and broke when the wording
        # became "was rejected for". Third time today a test has pinned copy
        # rather than behaviour - the property is that the verdict is stated,
        # not that it is capitalised.
        assert "rejected" in docs[0]["text"].lower()
        assert cluster.ClusterCode in docs[0]["text"]

    def test_only_failures_are_sent_as_evidence(self, rejected_pair):
        """The passes are not the answer to "why was this rejected".

        This used to send all ten rules with failures sorted first, and the model
        dutifully narrated the nine that passed - reproducing exactly the
        detailed summary the feature was built to replace. Sorting was not
        enough; the passes had to stop being sent at all.
        """
        app, cluster = rejected_pair
        docs = _rejection_rule_evidence(_query(f"Why was {cluster.ClusterCode} rejected for {app.ApplicationCode}?"))
        rules = [d for d in docs if d["entity_type"] == "eligibility_rule"]
        assert "failed" in rules[0]["text"].lower()

    def test_every_rule_is_traceable_to_a_rule_id(self, rejected_pair):
        """The point of this path: each claim the model can make is anchored to
        a rule the engine actually evaluated."""
        app, cluster = rejected_pair
        docs = _rejection_rule_evidence(_query(f"Why was {cluster.ClusterCode} rejected for {app.ApplicationCode}?"))
        rules = [d for d in docs if d["entity_type"] == "eligibility_rule"]
        assert rules and all("RULE-" in d["text"] for d in rules)

    def test_it_declines_when_the_question_is_not_about_a_rejection(self, rejected_pair):
        app, cluster = rejected_pair
        assert _rejection_rule_evidence(_query(f"Tell me about {cluster.ClusterCode} and {app.ApplicationCode}")) == []

    def test_it_declines_without_both_names(self, rejected_pair):
        """One name is not enough to evaluate a pair, and guessing the other
        would produce a confident answer about the wrong thing."""
        app, cluster = rejected_pair
        assert _rejection_rule_evidence(_query(f"Why was {cluster.ClusterCode} rejected?")) == []
        assert _rejection_rule_evidence(_query(f"Why was it rejected for {app.ApplicationCode}?")) == []

    def test_it_declines_when_a_name_does_not_resolve(self):
        assert _rejection_rule_evidence(_query("Why was zzz-p999 rejected for APP-NOTREAL?")) == []

    def test_a_database_failure_degrades_to_no_evidence(self, rejected_pair, monkeypatch):
        """Evidence is best-effort. A failure here must fall back to ordinary
        retrieval rather than failing the whole investigation - the same posture
        retrieve_related_context takes for the vector store."""
        app, cluster = rejected_pair

        def boom(*_a, **_k):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(placement, "evaluate_candidate", boom)
        assert _rejection_rule_evidence(_query(f"Why was {cluster.ClusterCode} rejected for {app.ApplicationCode}?")) == []
