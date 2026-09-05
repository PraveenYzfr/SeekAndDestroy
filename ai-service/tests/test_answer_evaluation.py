"""Tests for grading a delivered answer.

The cases here are the ones that would let a broken evaluator look healthy:

  * a missing verdict must not be storable as a zero, because an average over
    this table would then report a judge outage as a quality collapse;
  * a self-judged verdict must not reach the dashboard, because a model grading
    its own work grades it high and mixing that with independent verdicts
    produces a line nobody can read;
  * nothing in here may be able to raise into the caller, because the caller has
    already handed the answer to a user.

The happy path is tested too, but it is the least interesting case: an evaluator
that only works when everything works is not an evaluator.
"""

from __future__ import annotations

import json

import pytest

from app.evaluation.judge import JudgeDimension, JudgeResult, JudgeVerdict
from app.services import answer_evaluation


def _verdict(relevance=5, groundedness=5, actionability=4, confident=True):
    return JudgeVerdict(
        relevance=JudgeDimension(score=relevance, justification="'the top candidate' - answers it"),
        groundedness=JudgeDimension(score=groundedness, justification="'no data' - says so"),
        actionability=JudgeDimension(score=actionability, justification="'raise a change'"),
        confident=confident,
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture what would have been written, without a database."""
    rows: list[dict] = []
    monkeypatch.setattr(
        "app.repositories.answer_evaluation_repository.record", lambda values: rows.append(values)
    )
    return rows


@pytest.fixture
def no_audit(monkeypatch):
    """No audit rows - isolates the judge half from the deterministic half."""
    monkeypatch.setattr("app.repositories.base.fetch_all", lambda *a, **k: [])


class TestWhatGetsGraded:
    def test_a_report_with_no_prose_is_not_graded(self, captured):
        """A greeting has no figures and no evidence. Scoring it 'perfect' would
        inflate every average this table feeds, so it is not scored at all."""
        result = answer_evaluation.evaluate(question="hi", result={"final_report": {}})
        assert result is None
        assert captured == []

    def test_prose_extraction_skips_identifiers(self):
        """A judge asked whether 'cmh-p225' is actionable is being asked
        nonsense. Only the narrative fields are prose."""
        prose = answer_evaluation._prose_from({
            "executive_summary": "Place it on the Coventry cluster.",
            "next_steps": ["Raise a change", "Book the window"],
            "investigation_id": 42,
            "status": "Completed",
            "candidate_scores": [{"cluster_code": "cmh-p225"}],
        })
        assert "Coventry" in prose and "Raise a change" in prose
        assert "cmh-p225" not in prose
        assert "42" not in prose

    def test_prose_is_capped(self):
        """An unbounded string becomes an unbounded prompt - the one part of
        this module that could cost real money if a caller went wrong."""
        prose = answer_evaluation._prose_from({"summary": "x" * 50_000})
        assert len(prose) == answer_evaluation._MAX_PROSE_CHARS


class TestMissingIsNotZero:
    def test_judge_failure_stores_a_reason_and_no_scores(self, captured, no_audit, monkeypatch):
        """The distinction the whole table depends on. A judge that was down
        must leave NULLs, not zeros: an average that counts an outage as a bad
        answer reports a quality collapse that never happened."""
        monkeypatch.setattr(
            answer_evaluation, "_evidence_and_author_for", lambda _id: ({"clusters": [{"code": "c1"}]}, {}, None)
        )
        monkeypatch.setattr(
            "app.evaluation.judge.judge_answer",
            lambda *a, **k: JudgeResult(
                verdict=None, judge_provider="openai", judge_model="gpt-4o",
                self_judged=False, error="503 from provider",
            ),
        )
        answer_evaluation.evaluate(
            question="where do I put it?",
            result={"investigation_id": 1, "final_report": {"summary": "Coventry."}},
        )
        row = captured[0]
        assert row["JudgeRelevance"] is None
        assert row["JudgeGroundedness"] is None
        assert row["JudgeActionability"] is None
        assert "503" in row["JudgeError"]

    def test_unrecoverable_evidence_is_not_judged_at_all(self, captured, no_audit, monkeypatch):
        """Groundedness is unanswerable without the evidence, and a judge asked
        anyway answers confidently from nothing. Recorded as a failure with a
        reason rather than as a low score."""
        called = []
        monkeypatch.setattr(answer_evaluation, "_evidence_and_author_for", lambda _id: (None, {}, "no_evidence_gathered"))
        monkeypatch.setattr(
            "app.evaluation.judge.judge_answer", lambda *a, **k: called.append(1)
        )
        answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )
        assert called == [], "the judge must not be asked without evidence"
        assert captured[0]["JudgeError"]
        assert captured[0]["JudgeGroundedness"] is None

    def test_fidelity_is_null_when_evidence_is_gone_but_completeness_is_not(
        self, captured, monkeypatch
    ):
        """The three deterministic graders do not fail together.

        With no recoverable evidence, number and entity fidelity are NOT
        MEASURABLE and must be null. Completeness needs no evidence - it asks
        whether the model filled in the fields the schema requires - so it is
        still a real score, and dropping it would leave this answer with no
        objective measurement at all when one was available.
        """
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: [
                {"AuditId": 1, "ToolName": "CandidateExplanation",
                 "InputJson": None, "OutputJson": json.dumps({"summary": ""})},
            ],
        )
        monkeypatch.setattr(answer_evaluation, "_should_judge", lambda: False)
        row = answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )
        assert row["NumberFidelity"] is None
        assert row["EntityFidelity"] is None
        assert row["Completeness"] is not None
        assert row["GradedCalls"] == 1

    def test_nothing_measurable_writes_no_row(self, captured, monkeypatch):
        """A row of nulls makes the table look busier than the evaluation was."""
        monkeypatch.setattr("app.repositories.base.fetch_all", lambda *a, **k: [])
        monkeypatch.setattr(answer_evaluation, "_should_judge", lambda: False)
        result = answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )
        assert result is None
        assert captured == []

    def test_every_row_has_every_column(self, captured, no_audit, monkeypatch):
        """A dict whose keys depend on which branch ran is one a reader will
        eventually probe wrong, and the KeyError lands far from the cause."""
        from app.repositories import answer_evaluation_repository as repo

        monkeypatch.setattr(answer_evaluation, "_evidence_and_author_for", lambda _id: ({"e": 1}, {}, None))
        monkeypatch.setattr(
            "app.evaluation.judge.judge_answer",
            lambda *a, **k: JudgeResult(
                verdict=None, judge_provider="p", judge_model="m",
                self_judged=False, error="down",
            ),
        )
        answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )
        assert set(captured[0]) == set(repo.COLUMNS)


class TestSelfJudgingIsDisclosedNotExported:
    def test_self_judged_verdict_is_stored(self, captured, no_audit, monkeypatch):
        monkeypatch.setattr(answer_evaluation, "_evidence_and_author_for", lambda _id: ({"e": 1}, {}, None))
        monkeypatch.setattr(
            "app.evaluation.judge.judge_answer",
            lambda *a, **k: JudgeResult(
                verdict=_verdict(), judge_provider="openai", judge_model="gpt-4o",
                self_judged=True,
            ),
        )
        answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )
        assert captured[0]["JudgeSelfJudged"] is True
        assert captured[0]["JudgeRelevance"] == 5

    def test_self_judged_verdict_is_not_observed(self, monkeypatch):
        """Stored, because the disclosure is the point. Not exported, because a
        model scoring its own work scores it higher and averaging that with
        independent verdicts produces a series that cannot be read."""
        observed: list[tuple] = []

        class _Fake:
            def labels(self, **kw):
                observed.append(kw)
                return self

            def observe(self, value):
                pass

        monkeypatch.setattr("app.observability.metrics.judge_score", _Fake())
        monkeypatch.setattr("app.observability.metrics.fidelity_score", _Fake())
        answer_evaluation._observe({
            "JudgeRelevance": 5, "JudgeGroundedness": 5, "JudgeActionability": 5,
            "JudgeSelfJudged": True, "NumberFidelity": 1.0,
        })
        assert {"dimension": "relevance"} not in observed
        assert {"grader": "number_fidelity"} in observed, "fidelity is arithmetic, always exported"


class TestNothingCanBreakTheCaller:
    def test_a_database_failure_does_not_raise(self, no_audit, monkeypatch):
        """The answer has already been returned to the user. Losing a verdict is
        strictly better than turning a completed investigation into an error."""
        monkeypatch.setattr(answer_evaluation, "_evidence_and_author_for", lambda _id: ({"e": 1}, {}, None))
        monkeypatch.setattr(
            "app.evaluation.judge.judge_answer",
            lambda *a, **k: JudgeResult(
                verdict=_verdict(), judge_provider="p", judge_model="m", self_judged=False
            ),
        )
        monkeypatch.setattr(
            "app.repositories.base.execute",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no such table")),
        )
        answer_evaluation.evaluate(
            question="q", result={"investigation_id": 1, "final_report": {"summary": "Coventry."}}
        )  # must not raise

    def test_evaluate_async_swallows_everything(self, monkeypatch):
        monkeypatch.setattr(
            answer_evaluation, "evaluate",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        answer_evaluation._safe_evaluate(question="q", result={}, conversation_id=None)

    def test_disabled_does_nothing(self, monkeypatch):
        monkeypatch.setattr(answer_evaluation, "_enabled", lambda: False)
        started: list[int] = []
        monkeypatch.setattr(
            "threading.Thread", lambda **kw: started.append(1)
        )
        answer_evaluation.evaluate_async(question="q", result={"final_report": {"summary": "x"}})
        assert started == []


class TestSampling:
    def test_full_rate_never_touches_the_rng(self, monkeypatch):
        """The default path must have no randomness in it at all, so a seed set
        somewhere else cannot make 'judge everything' stop judging everything."""
        monkeypatch.setattr(
            "random.random", lambda: (_ for _ in ()).throw(AssertionError("rng was used"))
        )
        assert answer_evaluation._should_judge() is True

    def test_zero_rate_disables_the_judge_only(self, monkeypatch, captured):
        monkeypatch.setattr(
            "app.config.settings.get_settings",
            lambda: type("S", (), {"llm": type("L", (), {"judge_sample_rate": 0.0})()})(),
        )
        assert answer_evaluation._should_judge() is False


class TestRepositoryDecoding:
    def test_malformed_ungrounded_json_returns_empty(self):
        """A malformed audit detail must not break the screen displaying it."""
        from app.models.entities import AnswerEvaluation
        from app.repositories import answer_evaluation_repository as repo

        row = AnswerEvaluation(
            AnswerEvaluationId=1, InvestigationId=None, ConversationId=None, Question=None,
            NumberFidelity=None, EntityFidelity=None, Completeness=None,
            UngroundedJson="{not json",
            GradedCalls=0, UngradeableCalls=0, JudgeProvider=None, JudgeModel=None,
            JudgeRelevance=None, JudgeGroundedness=None, JudgeActionability=None,
            JudgeConfident=None, JudgeSelfJudged=None, JudgeJustification=None,
            JudgeError=None, DurationMs=None, CreatedAt="2026-09-02T00:00:00",
        )
        assert repo.ungrounded_tokens(row) == []
        row.UngroundedJson = json.dumps(["999", "12.5"])
        assert repo.ungrounded_tokens(row) == ["999", "12.5"]


class TestTheJudgeIsToldWhoWroteTheAnswer:
    """The gap every other test in this file passed straight over.

    judge_answer was called with the evidence alone. Its contract says that
    means self-judging "cannot be determined; it is then reported as False", so
    in production every verdict came back independent no matter which model
    wrote the answer - and the exclusion built on top of it could never fire.

    Sixteen tests passed throughout, because every one of them supplied
    self_judged directly on a canned JudgeResult instead of letting the real
    call decide it. A fake that answers the question under test cannot fail.
    """

    def test_the_author_from_the_audit_row_reaches_the_judge(self, captured, monkeypatch):
        seen: dict = {}

        def _spy(question, answer, evidence, *, author_provider=None, author_model=None):
            seen["provider"] = author_provider
            seen["model"] = author_model
            return JudgeResult(
                verdict=_verdict(), judge_provider="deepseek",
                judge_model="deepseek-v4-flash", self_judged=False,
            )

        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: [{
                "AuditId": 9, "ToolName": "FinalRecommendationReport",
                "InputJson": '<<<with_evidence>>>{"clusters": []}',
                "OutputJson": "{}",
                "Provider": "deepseek", "ModelIdentity": "deepseek-v4-flash",
            }],
        )
        monkeypatch.setattr(
            answer_evaluation, "_evidence_and_author_for",
            lambda _id: ({"clusters": []}, {"provider": "deepseek", "model": "deepseek-v4-flash"}, None),
        )
        monkeypatch.setattr("app.evaluation.judge.judge_answer", _spy)

        answer_evaluation.evaluate(
            question="where?",
            result={"investigation_id": 1, "final_report": {"summary": "Coventry."}},
        )
        assert seen == {"provider": "deepseek", "model": "deepseek-v4-flash"}, (
            "the judge must be told who wrote the answer, or self-judging is "
            "undetectable and same-model verdicts export as independent"
        )

    def test_the_author_is_read_not_re_resolved(self, monkeypatch):
        """A role can be repointed between an answer being written and this
        grading it. Re-resolving would compare the judge against a model that
        did not write the report."""
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: [{
                "InputJson": '<<<with_evidence>>>{"a": 1}',
                "Provider": "groq", "ModelIdentity": "gpt-oss-20b",
            }],
        )
        monkeypatch.setattr(
            "app.evaluation.graders.evidence_from_prompt", lambda _s: {"a": 1}
        )
        _, author, _why = answer_evaluation._evidence_and_author_for(1)
        assert author == {"provider": "groq", "model": "gpt-oss-20b"}


class TestWhichKindOfNothing:
    """One label was hiding three unrelated defects.

    "no_evidence" fired for an unreachable database, a pipeline that gathered
    nothing, and evidence in a shape the parser did not recognise. An
    infrastructure fault, a graph defect and a contract break, all raising one
    alarm that could not say which one to go and fix.

    Production settled it. Investigation 124 - "which 3 clusters are the best
    right-sizing candidates" - had ONE audit row, the final report, with no
    grounded answer and no narration behind it, and no investigation in that
    window had zero audit rows. So the answer was not that evidence went
    missing. The pipeline DELIVERED AN ANSWER IT NEVER GATHERED EVIDENCE FOR,
    and the judge was the only thing that noticed.
    """

    def test_an_unreachable_database_is_not_a_missing_report(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("database is down")

        monkeypatch.setattr("app.repositories.base.fetch_all", _boom)
        evidence, author, why = answer_evaluation._evidence_and_author_for(1)
        assert evidence is None and author == {}
        assert why == "evidence_read_failed", (
            "an unreachable database must not be reported as a pipeline defect - "
            "one is fixed by restarting infrastructure, the other by changing the graph"
        )

    def test_no_audit_rows_means_the_pipeline_gathered_nothing(self, monkeypatch):
        """NOT investigation 124 - I attributed this case to it and was wrong.

        Re-running inv 124's question after the split shipped returned
        evidence_unparseable, not this. Its audit row exists; the evidence
        inside it could not be read. Corrected here because a test docstring
        naming the wrong real-world case is how a wrong diagnosis gets
        inherited by whoever reads it next.
        """
        monkeypatch.setattr("app.repositories.base.fetch_all", lambda *a, **k: [])
        evidence, _author, why = answer_evaluation._evidence_and_author_for(1)
        assert evidence is None
        assert why == "no_evidence_gathered"

    def test_rows_that_carry_no_typed_evidence_are_a_contract_break(self, monkeypatch):
        """THIS is investigation 124's shape, confirmed by re-running its exact
        question against the deployed split.

        The call happened and was recorded. What it carried was not evidence
        this platform could read - a different fix again, and the one the real
        case needed."""
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: [{"InputJson": "{}", "Provider": "groq", "ModelIdentity": "m"}],
        )
        monkeypatch.setattr("app.evaluation.graders.evidence_from_prompt", lambda _s: None)
        evidence, _author, why = answer_evaluation._evidence_and_author_for(1)
        assert evidence is None
        assert why == "evidence_unparseable"

    def test_a_prompt_the_audit_writer_cut_says_so(self, monkeypatch):
        """Truncation is a KNOWN condition with a known fix, and the platform
        records it at the moment it happens - _audit_payload writes
        "truncated": true into the row this reads. Reporting it as a generic
        parse failure throws away the one thing that says what to do about it.

        This matters most for the final report, which carries more evidence
        than any other call and is therefore the likeliest to be cut - so the
        answer a user actually reads is the one the judge can least often
        ground-check."""
        monkeypatch.setattr(
            "app.repositories.base.fetch_all",
            lambda *a, **k: [{
                "InputJson": '{"truncated": true, "human": "Evidence: {\\"a\\": 1"}',
                "Provider": "groq", "ModelIdentity": "m",
            }],
        )
        monkeypatch.setattr("app.evaluation.graders.evidence_from_prompt", lambda _s: None)
        _evidence, _author, why = answer_evaluation._evidence_and_author_for(1)
        assert why == "evidence_truncated", (
            "a prompt we cut ourselves is not an unexplained contract break"
        )

    def test_the_four_reasons_are_distinct(self):
        """The point of the split. If any two collapse, the alarm goes back to
        being unactionable and nobody will notice until it fires."""
        assert len({"evidence_read_failed", "no_evidence_gathered",
                    "evidence_unparseable", "evidence_truncated"}) == 4
