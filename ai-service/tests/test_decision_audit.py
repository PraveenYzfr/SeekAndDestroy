"""A decision made in the chat must record who made it.

sad.RecommendationDecision carries DecidedBy and DecisionReason and is written by
POST /api/recommendations/{id}/decision. The UI never calls that endpoint - the
chat review posts to resumeInvestigation - so production accumulated
recommendations with Approved and Rejected statuses and *zero* decision rows. The
status recorded what was decided; nothing recorded who decided it, or why, for
the only path anyone actually uses.

That is the kind of gap that stays invisible until somebody asks who approved a
Tier-1 production placement, which is exactly when it matters. These tests exist
because the failure is silent: nothing errors, a screen still says Approved, and
the table is simply empty.
"""

from __future__ import annotations

import pytest

from app.graph import nodes


@pytest.fixture
def recorded(monkeypatch):
    """Capture save_decision calls instead of writing to the database."""
    calls = []

    def fake_save_decision(*, recommendation_id, decision, decision_reason, decided_by):
        calls.append(
            {
                "recommendation_id": recommendation_id,
                "decision": decision,
                "decision_reason": decision_reason,
                "decided_by": decided_by,
            }
        )
        return len(calls)

    monkeypatch.setattr(nodes.recommendation_repository, "save_decision", fake_save_decision)
    return calls


class TestWhatGetsRecorded:
    def test_an_approval_records_the_reviewer_and_their_comment(self, recorded):
        nodes._record_decisions(
            {"reviewer_employee_id": 1001, "decision": "Approve", "comments": "Best headroom."},
            [55],
        )
        assert recorded == [
            {
                "recommendation_id": 55,
                "decision": "Approve",
                "decision_reason": "Best headroom.",
                "decided_by": 1001,
            }
        ]

    def test_a_rejection_is_recorded_too(self, recorded):
        """Rejecting the whole shortlist is a decision, not an absence of one.
        It is the case most worth having in an audit trail: someone looked at
        every option and said no to all of them."""
        nodes._record_decisions(
            {"reviewer_employee_id": 1001, "decision": "Reject", "comments": None}, [7, 8, 9]
        )
        assert [c["recommendation_id"] for c in recorded] == [7, 8, 9]
        assert {c["decision"] for c in recorded} == {"Reject"}

    def test_a_missing_comment_is_recorded_as_null_not_invented(self, recorded):
        nodes._record_decisions(
            {"reviewer_employee_id": 1001, "decision": "Approve", "comments": None}, [3]
        )
        assert recorded[0]["decision_reason"] is None


class TestWhatMustNotBeRecorded:
    def test_no_reviewer_means_no_row_rather_than_a_fabricated_approver(self, recorded):
        """DecidedBy is NOT NULL with a foreign key to sad.Employee, so there is
        no honest value to substitute. An audit trail that invents an approver is
        worse than one that is empty - the empty one is visibly missing, and the
        invented one is evidence of something that did not happen."""
        nodes._record_decisions(
            {"reviewer_employee_id": None, "decision": "Approve", "comments": "x"}, [1]
        )
        assert recorded == []

    def test_an_investigation_with_no_decision_records_nothing(self, recorded):
        """persist_recommendations also runs on the first pass, before any human
        has seen the shortlist. Every candidate is PendingReview then, and there
        is no decision to record."""
        nodes._record_decisions({"reviewer_employee_id": 1001, "decision": None}, [1, 2])
        assert recorded == []

    def test_nothing_decided_writes_nothing(self, recorded):
        nodes._record_decisions(
            {"reviewer_employee_id": 1001, "decision": "Approve", "comments": "x"}, []
        )
        assert recorded == []


def test_superseded_options_are_not_treated_as_decisions():
    """The distinction the whole design rests on.

    When a reviewer approves one cluster, the others become 'Superseded' - they
    were displaced by the one chosen, not judged on their merits. Recording a
    'Reject' against them would put a decision in the audit trail that the
    reviewer never made, and would later read as "they turned down atl-03",
    which is untrue and is the sort of thing a capacity review argues about.
    """
    assert "Superseded" not in nodes._DECIDED_STATUSES
    assert "PendingReview" not in nodes._DECIDED_STATUSES
    assert set(nodes._DECIDED_STATUSES) == {"Approved", "Rejected"}
