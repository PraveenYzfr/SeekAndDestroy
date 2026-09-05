"""A sized request for infrastructure is a placement, whatever the workload is called.

Found by verifying a production deploy rather than by reading the code:
"where can I host a Tier-2 reporting service needing 10 cores, 40 GB RAM and
600 GB storage" came back Completed with no shortlist, three runs out of three,
while "batch analytics workload" and "internal web app" each returned twelve
options.

The cause was one bare substring. _QUESTION_KEYWORDS held "report", which
matches "reporting", and the question test ran before the capacity test - so an
entire class of ordinary workload name had been unplaceable since the first
commit. Probing for siblings found "at least" doing the same thing to "I need
at least 32 cores", which is not an edge case but the plainest way there is to
ask for capacity.

These tests pin BOTH directions, because the fix is an ordering change and the
risk it carries is the opposite mistake: a genuine question that happens to
mention a number must not become a placement run.
"""

from __future__ import annotations

import pytest

from app.graph.nodes import classify_investigation_type
from app.models.enums import InvestigationType


class TestASizedRequestIsAPlacement:
    """The regression. Each of these names a workload, a quantity and a verb."""

    @pytest.mark.parametrize(
        "query",
        [
            "Where can I host a Tier-2 reporting service needing 10 cores, 40 GB RAM and 600 GB storage?",
            "Where can I host a reporting workload needing 10 cores and 40 GB RAM?",
            "I need at least 32 cores and 128 GB RAM for a production workload",
            "Find hosting for a regulatory reporting app needing 8 cores and 32 GB",
        ],
    )
    def test_it_classifies_as_capacity(self, query):
        assert classify_investigation_type(query) == InvestigationType.CAPACITY


class TestQuestionsStayQuestions:
    """The other direction, which the ordering change could have broken.

    "why was" and the rest are tested BEFORE the capacity check precisely so a
    retrospective question about a sized workload is still a question.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "Why was CL-NYC-03 rejected for a 32 core application?",
            "Why is atl-03 running so hot?",
            "Compare the top three clusters for a 16 core workload",
            "Show clusters with at least 20% headroom",
            "Generate a report of underutilised capacity",
        ],
    )
    def test_it_stays_a_question(self, query):
        assert classify_investigation_type(query) in (
            InvestigationType.QUESTION,
            InvestigationType.RIGHT_SIZING,
        )

    def test_at_least_alone_is_still_a_question(self):
        """Without a provisioning verb it is a filter, not a request."""
        assert classify_investigation_type(
            "Which clusters have at least 20% CPU headroom?"
        ) == InvestigationType.QUESTION


class TestTheOtherTypesAreUndisturbed:
    """The reorder sits between the question and capacity tests, so everything
    routed before or after it must be unaffected."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("Forecast capacity for CL-NYC-03 over 90 days", InvestigationType.FORECAST),
            ("Which clusters are underutilized and could be right-sized?", InvestigationType.RIGHT_SIZING),
            ("Can we consolidate these workloads?", InvestigationType.CONSOLIDATION),
            ("8 CPU, 32 GB RAM, 500 GB storage", InvestigationType.CAPACITY),
        ],
    )
    def test_unchanged(self, query, expected):
        assert classify_investigation_type(query) == expected
