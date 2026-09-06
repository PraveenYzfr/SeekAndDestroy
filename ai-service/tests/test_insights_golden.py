"""Tests for app.evaluation.insights_golden.

run_judge=False everywhere except the one test that explicitly monkeypatches
judge_answer: the real judge role resolves to whatever SAD_LLM__PROVIDER is
configured (a real, billed provider in this environment), and this suite
must stay fast and free like the rest of ai-service/tests. The golden set
itself is meant to be run manually against a real judge, not on every
commit - same reasoning as this codebase's other evaluation modules, which
read already-recorded calls rather than making new ones during tests.
"""

from __future__ import annotations

from app.agents.mock_llm import MockChatModel
from app.evaluation import insights_golden
from app.evaluation.insights_golden import (
    CASES,
    InsightGoldenSummary,
    run_case,
    run_golden_set,
)
from app.evaluation.judge import JudgeDimension, JudgeResult, JudgeVerdict
from app.models.insights import InsightNarrative


def test_cases_include_exactly_one_control():
    controls = [c for c in CASES if c.is_control]
    assert len(controls) == 1


def test_control_case_result_has_zero_rows():
    """The control's whole point depends on this being true - if the corpus
    ever grows a status value matching the filter, the control stops
    controlling anything and this test is what would catch it."""
    control = next(c for c in CASES if c.is_control)
    result = control.build()
    assert result["rows"] == []
    assert result["total_count"] == 0


def test_run_case_against_mock_provider_passes_for_every_case():
    """No API key needed - the mock echoes evidence values verbatim (see
    app.agents.mock_llm), so a well-behaved narrator over real query-layer
    results should pass number_fidelity for every case, including the
    control."""
    llm = MockChatModel()
    for case in CASES:
        result = run_case(case, llm, run_judge=False)
        assert result.passed, f"{case.id} failed: {result.ungrounded_figures or result.hard_failure}"
        assert result.hard_failure is None


def test_run_golden_set_reports_control_passed():
    summary = run_golden_set(MockChatModel(), run_judge=False)
    assert summary.total == len(CASES)
    assert summary.control_passed is True


def test_run_case_records_hard_failure_without_crashing(monkeypatch):
    """A tampered narrative (wrong total_count) must surface as a failed
    case with the reason, not raise out of run_case and abort the whole
    golden-set run over one bad case.

    Patches app.insights.narrator.run_structured, NOT insights_golden.narrate
    directly - narrate() is what runs assert_no_number_drift; replacing
    narrate() wholesale would bypass the very guard this test means to
    exercise and let a tampered total_count sail through unchecked."""
    from app.insights import narrator as narrator_module

    case = next(c for c in CASES if not c.is_control)
    result_dict = case.build()

    tampered = InsightNarrative(
        headline="tampered", narrative="...", insight="", caveats=[],
        total_count=result_dict["total_count"] + 1,
    )
    monkeypatch.setattr(narrator_module, "run_structured", lambda *a, **k: tampered)

    outcome = run_case(case, MockChatModel(), run_judge=False)
    assert outcome.passed is False
    assert outcome.hard_failure is not None


def test_run_case_includes_judge_verdict_when_requested(monkeypatch):
    """The judge integration point, proven with a canned verdict rather than
    a real model call - see module docstring on why this suite never calls
    the real judge role."""
    canned = JudgeResult(
        verdict=JudgeVerdict(
            relevance=JudgeDimension(score=5, justification="Directly answers the question asked."),
            groundedness=JudgeDimension(score=5, justification="States figures matching the evidence."),
            actionability=JudgeDimension(score=4, justification="Names the categories a reader would act on."),
        ),
        judge_provider="mock", judge_model="mock-judge", self_judged=False,
    )
    monkeypatch.setattr(insights_golden, "judge_answer", lambda *a, **k: canned)

    case = next(c for c in CASES if not c.is_control)
    result = run_case(case, MockChatModel(), run_judge=True)
    assert result.judge is not None
    # mean_score rounds to 2 places (see JudgeVerdict.mean_score) - (5+5+4)/3
    # rounds to 4.67, not the unrounded fraction.
    assert result.judge.verdict.mean_score == 4.67


def test_usable_judge_scores_excludes_self_judged_and_unconfident():
    verdict = JudgeVerdict(
        relevance=JudgeDimension(score=5, justification="x"),
        groundedness=JudgeDimension(score=5, justification="x"),
        actionability=JudgeDimension(score=5, justification="x"),
    )
    unconfident_verdict = JudgeVerdict(
        relevance=JudgeDimension(score=3, justification="x"),
        groundedness=JudgeDimension(score=3, justification="x"),
        actionability=JudgeDimension(score=3, justification="x"),
        confident=False,
    )
    summary = InsightGoldenSummary(results=[
        insights_golden.InsightCaseResult(
            case_id="a", passed=True,
            judge=JudgeResult(verdict=verdict, judge_provider="p", judge_model="m", self_judged=False),
        ),
        insights_golden.InsightCaseResult(
            case_id="b", passed=True,
            judge=JudgeResult(verdict=verdict, judge_provider="p", judge_model="m", self_judged=True),
        ),
        insights_golden.InsightCaseResult(
            case_id="c", passed=True,
            judge=JudgeResult(
                verdict=unconfident_verdict, judge_provider="p", judge_model="m", self_judged=False,
            ),
        ),
    ])
    # Only the first result is usable: the second is self-judged, the third
    # is not confident - both excluded per JudgeResult.usable, not averaged in.
    assert summary.usable_judge_scores == [5.0]
