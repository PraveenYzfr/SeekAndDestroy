"""Failures the graph used to drop are now recorded, and losing one is counted.

Seven except branches logged a warning and continued. What they DO is right - a
narration failure must not fail an investigation whose numbers are already
computed - but nothing counted them, nothing stored them, and "how often does
narration fail" needed a log grep on a production box.
"""

from __future__ import annotations

from app.repositories import remediation_repository as repo


class _Recorder:
    def __init__(self, fail=False):
        self.rows: list[dict] = []
        self.fail = fail

    def __call__(self, sql, params):
        if self.fail:
            raise RuntimeError("INSERT permission was denied")
        self.rows.append(params)
        return 1


def test_a_dropped_failure_is_stored_with_where_it_happened(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    assert repo.record(
        site="graph.explain_candidate_failed",
        investigation_id=42, conversation_id="c" * 32,
        detail="RuntimeError: provider said no",
    ) is True
    row = rec.rows[0]
    # The logger event name, unchanged, so a row traces back to the exact except
    # branch without a translation table that would drift from the code.
    assert row["Site"] == "graph.explain_candidate_failed"
    assert row["Source"] == "python"
    assert row["InvestigationId"] == 42
    assert row["Status"] == "Queued"


def test_a_failure_with_no_investigation_is_still_recorded(monkeypatch):
    """The extraction drop site has only the query in scope, and the chat
    replies that never run the pipeline still produce failures. A failure with no
    investigation id is not less real than one with."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    assert repo.record(site="graph.load_application_requirements.llm_extraction_failed") is True
    assert rec.rows[0]["InvestigationId"] is None


def test_losing_a_row_is_counted_not_just_logged(monkeypatch):
    """A queue that quietly fails to record failures reproduces the bug it exists
    to fix. sad.AnswerEvaluation sat empty for hours behind a missing INSERT
    grant while every write appeared to succeed."""
    seen = []

    class _Counter:
        def labels(self, **kw):
            seen.append(kw)
            return self

        def inc(self):
            pass

    import app.observability.metrics as metrics
    monkeypatch.setattr(metrics, "remediation_enqueued_total", _Counter())
    monkeypatch.setattr(repo, "execute", _Recorder(fail=True))

    assert repo.record(site="graph.grounded_qa_failed") is False
    assert seen and seen[-1]["outcome"] == "lost"


def test_recording_never_raises(monkeypatch):
    """Enqueuing a failure must not turn a degraded answer into a broken one."""
    monkeypatch.setattr(repo, "execute", _Recorder(fail=True))
    assert repo.record(site="graph.generate_final_report_failed") is False


def test_unserialisable_evidence_keeps_its_shape(monkeypatch):
    """repr beats dropping the column and losing the only record of what the
    model was given."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)

    class _Odd:
        def __repr__(self):
            return "<odd evidence>"

    repo.record(site="graph.explain_candidate_failed", evidence={"x": _Odd()})
    assert "odd evidence" in rec.rows[0]["EvidenceJson"]


def test_judge_justifications_travel_with_the_scores(monkeypatch):
    """A bare 2/5 says something is wrong and nothing about what."""
    rec = _Recorder()
    monkeypatch.setattr(repo, "execute", rec)
    repo.record(
        site="judge.low_verdict", source="judge",
        judge={"relevance": 2, "groundedness": 5, "actionability": 3,
               "justifications": {"relevance": "answers a different question"}},
    )
    row = rec.rows[0]
    assert row["JudgeRelevance"] == 2
    assert "different question" in row["JudgeJustifications"]


# ---------------------------------------------------------------------------
# The user-facing text
# ---------------------------------------------------------------------------


def test_the_notice_leads_with_the_word_failed():
    """Praveen was explicit, and the reason is that the alternatives read as an
    answer. "I found limited information" and "based on available evidence" are
    what a reader acts on rather than questions - a degraded answer that does not
    announce itself is worse than an error, because an error stops somebody."""
    from app.graph.nodes import _narration_failed_notice

    notice = _narration_failed_notice("cmh-p225")
    assert "FAILED" in notice
    # Before the reassurance, not after it.
    assert notice.index("FAILED") < notice.index("correct")
    for weasel in ("limited information", "based on available evidence", "some"):
        assert weasel not in notice.lower()


def test_the_notice_says_what_survives_the_failure():
    """The scores come from the deterministic engines and are unaffected by a
    narration failure. Withholding them because the prose broke would discard
    the correct part of the answer."""
    from app.graph.nodes import _narration_failed_notice

    notice = _narration_failed_notice("atl-03")
    assert "engine" in notice
    assert "atl-03" in notice


def test_a_candidate_that_lost_its_prose_still_appears(monkeypatch):
    """_narrate_all returns only successes, so a failed narration used to leave a
    shortlisted cluster silently unexplained - the reader saw two explanations
    for three options with no way to tell whether the third was unremarkable or
    broken."""
    from app.graph import nodes
    from app.models.enums import InvestigationType

    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: object())
    monkeypatch.setattr(nodes, "_narrate_all", lambda items, narrate, on_error: [])
    monkeypatch.setattr(nodes, "_dropped", lambda *a, **k: None)

    out = nodes.generate_recommendation_explanations({
        "investigation_type": InvestigationType.HOSTING,
        "candidate_scores": [
            {"cluster_code": "atl-03", "eligibility_status": "Eligible", "overall_score": 91.4},
        ],
        "application_requirements": {"application_code": "APP-CRM"},
    })
    got = out["recommendation_explanations"]
    assert len(got) == 1
    assert got[0]["cluster_code"] == "atl-03"
    assert got[0]["narration_failed"] is True
    assert "FAILED" in got[0]["summary"]
    # The engine's own figures survive and are carried through.
    assert got[0]["overall_score"] == 91.4
