"""Golden-set evaluation for the CMDB Insighter's narrator.

WHY THIS EXISTS
----------------
80+ unit tests in ai-service/tests/test_insights.py and friends prove the
SQL is right. Nothing checks that the SENTENCE built from it says what the
numbers say - the exact gap named while building this feature: a narrator
that quietly rounds 209 to "over 200", or turns "weighted impact 16" into
"16 incidents", breaks the SQL-decides/model-narrates trust boundary without
touching a single database value, and the unit tests (which assert SQL
correctness, not prose correctness) stay green through all of it.

WHAT THIS DOES NOT DO
----------------------
It does not grade numbers with an LLM. app.evaluation.judge already refuses
that job for the same reason this module does: graders.number_fidelity
proves, by arithmetic, whether every figure in prose is traceable to the
evidence it was written from. A judge asked to also opine on arithmetic
could only agree or be wrong, and its disagreement would carry no
information (see judge.py's own docstring). The judge here is asked about
relevance, groundedness and actionability only - the three dimensions
arithmetic cannot see.

CASES ARE COMPUTED, NOT HAND-TYPED
-------------------------------------
Every case's evidence comes from calling the real query layer
(app.insights.query_builder.run_query) at evaluation time, never a number
written into this file by hand - same reasoning as
app.evaluation.retrieval_golden's derived-not-hand-labelled ground truth: a
hand-typed expectation describes the corpus it was written against and
starts lying, silently, the moment the seed changes (which, tonight, it did
four times).

THE CONTROL CASE
------------------
One case (empty_result_control) is a filter combination guaranteed to match
zero rows. Modelled on retrieval_golden's own unfalsifiable control case and
on tonight's repeated lesson (six separate times, by count, before this
module existed) that a check which can only ever pass proves nothing: this
one is built so a narrator that invents a finding to fill an empty result,
or claims something went wrong, fails it loudly and specifically, rather
than the golden set only ever exercising cases where the answer sounds like
something happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from langchain_core.language_models.chat_models import BaseChatModel

from app.agents.guards import NumberDriftError
from app.evaluation.graders import number_fidelity
from app.evaluation.judge import JudgeResult, judge_answer
from app.insights.narrator import InsightNarrationError, evidence_for, narrate
from app.insights.query_builder import run_query
from app.models.insights import InsightQuerySpec


@dataclass(frozen=True)
class InsightGoldenCase:
    id: str
    question: str
    #: Builds the query-layer result at evaluation time - never a hand-typed
    #: expectation. See module docstring.
    build: Callable[[], dict]
    #: True for the case whose result is a guaranteed-empty query - see
    #: module docstring's "THE CONTROL CASE".
    is_control: bool = False
    notes: str = ""


def _sev1_by_root_cause() -> dict:
    return run_query(InsightQuerySpec(entity="incident", group_by=["root_cause_category"], filters={"severity": ["Sev1"]}))


def _changes_by_close_code() -> dict:
    return run_query(InsightQuerySpec(entity="change", group_by=["close_code"]))


def _incidents_by_business_service_criticality() -> dict:
    return run_query(InsightQuerySpec(entity="incident", group_by=["business_service_criticality"]))


def _empty_result_control() -> dict:
    # 'Cancelled' is not a value CK_Incident_Status permits (Open | InProgress
    # | Resolved | Closed) - guaranteed zero rows, the same technique
    # test_insights.py's test_empty_result_is_returned_not_raised uses for
    # the same reason: deterministic regardless of what tonight's corpus
    # contains.
    return run_query(InsightQuerySpec(entity="incident", group_by=["root_cause_category"], filters={"status": ["Cancelled"]}))


CASES: tuple[InsightGoldenCase, ...] = (
    InsightGoldenCase(
        "sev1_by_root_cause", "How many Sev1 incidents and what are the root causes?", _sev1_by_root_cause,
        notes="The acceptance case. Also the one most likely to have an inversion worth calling out.",
    ),
    InsightGoldenCase(
        "changes_by_close_code", "How many changes failed, broken down by close code?", _changes_by_close_code,
    ),
    InsightGoldenCase(
        "incidents_by_business_service_criticality",
        "How many incidents by business service criticality tier?",
        _incidents_by_business_service_criticality,
        notes="Exercises a NULL group (incidents whose application has no business service) staying visible.",
    ),
    InsightGoldenCase(
        "empty_result_control",
        "How many Cancelled-status incidents are there, by root cause?",
        _empty_result_control,
        is_control=True,
        notes="Guaranteed zero rows. A narrator that invents a finding here fails number_fidelity, not a judge's opinion.",
    ),
)


@dataclass
class InsightCaseResult:
    case_id: str
    passed: bool
    headline: str = ""
    #: Figures in the prose that number_fidelity could not trace to the
    #: evidence. Non-empty implies passed=False.
    ungrounded_figures: list[str] = field(default_factory=list)
    judge: JudgeResult | None = None
    #: Set when assert_no_number_drift or the narrator's own fidelity check
    #: raised before scoring could happen at all. This is the trust boundary
    #: working, not the evaluation failing to run - see run_case.
    hard_failure: str | None = None


def run_case(
    case: InsightGoldenCase, narrator_llm: BaseChatModel, *, run_judge: bool = True,
) -> InsightCaseResult:
    """Runs one case through the real narrator and grades it.

    A NumberDriftError or InsightNarrationError here is not an evaluation
    failure to retry past - it is app.insights.narrator's own trust boundary
    refusing an unsafe narrative, which is the feature working exactly as
    designed. Recorded as a failed case with the reason, never swallowed and
    never retried into a passing one.
    """
    result = case.build()
    try:
        narrative = narrate(narrator_llm, case.question, result)
    except (NumberDriftError, InsightNarrationError) as exc:
        return InsightCaseResult(case_id=case.id, passed=False, hard_failure=str(exc))

    evidence = evidence_for(result)
    prose = " ".join([narrative.headline, narrative.narrative, narrative.insight, *narrative.caveats])
    fidelity = number_fidelity(prose, evidence)

    judge_result = None
    if run_judge:
        judge_result = judge_answer(case.question, prose, evidence)

    passed = not fidelity.ungrounded
    return InsightCaseResult(
        case_id=case.id, passed=passed, headline=narrative.headline,
        ungrounded_figures=fidelity.ungrounded, judge=judge_result,
    )


@dataclass
class InsightGoldenSummary:
    results: list[InsightCaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def control_passed(self) -> bool | None:
        """None if the control case was not run (should not happen - CASES
        always includes one) rather than defaulting to True, which would
        silently claim the control was exercised when it was not."""
        control_ids = {c.id for c in CASES if c.is_control}
        control_results = [r for r in self.results if r.case_id in control_ids]
        if not control_results:
            return None
        return all(r.passed for r in control_results)

    @property
    def usable_judge_scores(self) -> list[float]:
        """Mean judge scores, excluding self-judged and unconfident verdicts
        - see app.evaluation.judge.JudgeResult.usable for why those are
        excluded rather than averaged in."""
        return [
            r.judge.verdict.mean_score
            for r in self.results
            if r.judge is not None and r.judge.usable and r.judge.verdict is not None
        ]


def run_golden_set(narrator_llm: BaseChatModel, *, run_judge: bool = True) -> InsightGoldenSummary:
    return InsightGoldenSummary(results=[run_case(case, narrator_llm, run_judge=run_judge) for case in CASES])
