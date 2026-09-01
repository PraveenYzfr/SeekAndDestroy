"""LLM-as-judge, scoped to what deterministic grading cannot see.

WHAT THIS DOES NOT DO
---------------------
It does not score numbers. graders.number_fidelity already proves, by
arithmetic, whether every figure in a sentence appears in the evidence that
sentence was written from. A judge asked the same question could only agree or
be wrong, and its disagreement would carry no information.

The prompt says so explicitly and the schema gives it nowhere to put a numeric
verdict - there is no dimension for arithmetic. That is the whole enforcement:
the judge is not asked, and cannot answer. A judge that comments on numbers
anyway does so in free text that nothing reads as a score.

WHAT IT DOES
------------
Three dimensions a rule cannot reach:

    relevance     does the answer address the question that was asked
    groundedness  does it stay inside its evidence, and say so when the
                  evidence runs out, rather than filling the gap
    actionability would an infrastructure engineer know what to do next

THE BIAS THIS CARRIES, STATED RATHER THAN HIDDEN
------------------------------------------------
A model judging its own output scores it higher. That is well documented and it
is not something a prompt fixes. So:

  * the judge is its own role, configurable independently of every other role,
    so it can be pointed at a different provider entirely;
  * judge_answer() records whether judge and author were the same model, and
    the golden-set runner reports that alongside the score rather than
    averaging it away.

A judge score is evidence about an answer. It is not a measurement, and this
module does not present it as one.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

JUDGE_SYSTEM = """You grade answers produced by an infrastructure platform.

You are grading THREE things and nothing else:

1. relevance - does the answer address the question actually asked?
2. groundedness - does it stay within the evidence it was given, and say plainly
   when the evidence does not cover something, rather than filling the gap?
3. actionability - would an infrastructure engineer know what to do next?

DO NOT grade numbers. Whether a figure in the answer appears in the evidence is
checked separately and exactly, by arithmetic, before you see this. Comments
about arithmetic will be discarded, so spend no effort on them.

Score each dimension 1-5:
  5 fully satisfies the dimension
  4 minor shortcoming a reader would not be misled by
  3 noticeably weak but not wrong
  2 a reader could be misled
  1 fails the dimension

An answer that correctly says it does not have enough information scores HIGH on
groundedness, not low. Refusing to guess is the behaviour being rewarded.

Quote the specific phrase you are reacting to in each justification. A
justification that could apply to any answer is not a justification."""


class JudgeDimension(BaseModel):
    score: int = Field(ge=1, le=5)
    #: The specific phrase being reacted to. Required, because a justification
    #: that names nothing is unfalsifiable and cannot be audited later.
    justification: str


class JudgeVerdict(BaseModel):
    relevance: JudgeDimension
    groundedness: JudgeDimension
    actionability: JudgeDimension
    #: The judge's own view of whether it had enough to grade on. A judge that
    #: cannot tell is more useful than one that guesses and is averaged in.
    confident: bool = True
    overall_comment: str = ""

    @property
    def mean_score(self) -> float:
        return round(
            (self.relevance.score + self.groundedness.score + self.actionability.score) / 3, 2
        )


class JudgeResult(BaseModel):
    verdict: JudgeVerdict | None
    judge_provider: str
    judge_model: str
    #: True when the judge graded output from its own model. Reported, never
    #: silently corrected - there is no correction, only disclosure.
    self_judged: bool
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Whether this verdict should count toward a headline score.

        A self-judged verdict is excluded by default, not because it is
        worthless but because averaging it with independent verdicts produces a
        number nobody can interpret.
        """
        return self.verdict is not None and not self.self_judged and self.verdict.confident


def judge_answer(
    question: str,
    answer: str,
    evidence: Any,
    *,
    author_provider: str | None = None,
    author_model: str | None = None,
) -> JudgeResult:
    """Grade one answer. Never raises - a judge failure is data, not an outage.

    ``author_provider``/``author_model`` name the model that wrote ``answer``,
    so self-judging can be detected. Passing neither is allowed and simply means
    self_judged cannot be determined; it is then reported as False, which is why
    the golden-set runner always passes them.
    """
    from app.agents.llm_factory import get_chat_model_for_role, resolve_role
    from app.agents.structured import run_structured

    resolved = resolve_role("judge")
    self_judged = bool(
        author_provider
        and author_model
        and author_provider == resolved["provider"]
        and author_model == resolved["model"]
    )

    try:
        from app.prompts.templates import with_evidence

        human = with_evidence(
            "Grade this answer on relevance, groundedness and actionability.",
            {
                "question": question,
                "answer_under_review": answer,
                "evidence_the_answer_was_given": evidence,
            },
        )
        verdict = run_structured(get_chat_model_for_role("judge"), JUDGE_SYSTEM, human, JudgeVerdict)
    except Exception as exc:  # noqa: BLE001
        # A judge that is down must not fail a golden-set run: the deterministic
        # graders still produce a complete result without it, which is the whole
        # reason they remain the backstop.
        logger.warning("judge.failed", error=str(exc))
        return JudgeResult(
            verdict=None, judge_provider=resolved["provider"], judge_model=resolved["model"],
            self_judged=self_judged, error=str(exc)[:300],
        )

    if self_judged:
        logger.warning(
            "judge.self_judged",
            model=resolved["model"],
            detail="judge and author are the same model; verdict excluded from headline scores",
        )

    return JudgeResult(
        verdict=verdict, judge_provider=resolved["provider"], judge_model=resolved["model"],
        self_judged=self_judged,
    )
