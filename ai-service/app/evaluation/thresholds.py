"""What counts as pass, warn and fail. One definition, used everywhere.

Set by Praveen, and written down here rather than inline at each gate so the
eval suite and the live path cannot drift into disagreeing about what "good"
means. Two components each holding their own copy of a threshold is how a
dashboard ends up reporting green while a gate blocks the deploy.

    number_fidelity     100%              fail below
    entity_fidelity     100%              fail below
    completeness        >95% pass, 85-95% warn, <85% fail
    judge (min of 3)    4-5 pass, 3 warn, <=2 fail

WHY THE FIRST TWO HAVE NO WARN BAND
------------------------------------
An invented cluster code or an invented figure in an infrastructure
recommendation is not a degradation, it is a wrong answer. There is no rate of
it that is acceptable: one fabricated cluster in twenty still sends somebody to
a data centre that was never a candidate.

Rounding is already forgiven INSIDE the grader - a figure within 1% or 0.05 of
an evidence value counts as grounded, because reporting 27.3 for 27.28 is honest
writing. So anything that reaches "ungrounded" has already failed a tolerance
check, and a warn band on top would be excusing it twice.

WHY THE JUDGE CANNOT FAIL A DEPLOY
-----------------------------------
The deterministic graders decide pass/fail. The judge decides what goes in the
retry queue. That split is the whole design:

    python   judge    meaning                          action
    pass     pass     right and useful                 pass
    pass     FAIL     figures correct, answer useless  retry
    FAIL     pass     fluent and wrong                 fail - the worst case
    FAIL     FAIL     broken                           fail

Row three is why a judge score can never override a grader. A model that writes
confidently and cites an invented cluster reads well to a judge and is the most
dangerous output this platform can produce.

Row two is why the judge exists at all. "Give me best dc for java apps" scored
completeness 7/7 and entity fidelity 16/16 - every deterministic check green -
and the answer was useless. Arithmetic cannot see that.

MINIMUM, NEVER MEAN
-------------------
An answer's judge outcome is the WORST of its three dimensions. A mean of 4.0 is
5, 5, 2 as easily as 4, 4, 4, and that 2 is a groundedness failure hiding behind
two good scores. Same reason fidelity is reported as a distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


#: Deterministic graders. A rate at or above `fail_below` passes.
NUMBER_FIDELITY_MIN = 1.0
ENTITY_FIDELITY_MIN = 1.0
COMPLETENESS_PASS = 0.95
COMPLETENESS_WARN = 0.85

#: Judge, per dimension on the 1-5 rubric the judge itself is given:
#:   5 fully satisfies · 4 minor shortcoming a reader would not be misled by
#:   3 noticeably weak but not wrong · 2 A READER COULD BE MISLED · 1 fails
#:
#: "A reader could be misled" is where fail has to start, so 2 is the ceiling
#: for a failure. Taken from the rubric rather than invented, so the scale the
#: judge is scored against is the scale it was asked to use.
JUDGE_PASS = 4
JUDGE_WARN = 3

#: Actionability is looser by one notch. A correct, grounded, slightly terse
#: answer is a style complaint, not a defect, and gating on it fills the retry
#: queue with noise. Groundedness and relevance are real failures: filling a gap
#: the evidence did not cover, or answering a question nobody asked.
JUDGE_ACTIONABILITY_PASS = 3
JUDGE_ACTIONABILITY_WARN = 2


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str

    @property
    def blocks(self) -> bool:
        """Whether this stops a deploy. Only a deterministic FAIL does."""
        return self.outcome is Outcome.FAIL


def grade_outcome(name: str, rate: float | None) -> Verdict:
    """One deterministic grader's verdict.

    ``rate`` of None means NOT MEASURED - a truncated prompt, unrecoverable
    evidence, a property that did not apply. It is not a zero and must not be
    graded as one: an unevaluable check reported as a failure is how a broken
    instrument reads as broken output.
    """
    if rate is None:
        return Verdict(Outcome.PASS, f"{name}: not measured")

    if name == "completeness":
        if rate > COMPLETENESS_PASS:
            return Verdict(Outcome.PASS, f"completeness {rate:.0%}")
        if rate >= COMPLETENESS_WARN:
            return Verdict(Outcome.WARN, f"completeness {rate:.0%} - below {COMPLETENESS_PASS:.0%}")
        return Verdict(Outcome.FAIL, f"completeness {rate:.0%} - below {COMPLETENESS_WARN:.0%}")

    minimum = NUMBER_FIDELITY_MIN if name == "number_fidelity" else ENTITY_FIDELITY_MIN
    if rate >= minimum:
        return Verdict(Outcome.PASS, f"{name} {rate:.0%}")
    return Verdict(Outcome.FAIL, f"{name} {rate:.0%} - anything below 100% is a fabricated value")


def judge_outcome(relevance: int | None, groundedness: int | None,
                  actionability: int | None) -> Verdict:
    """The judge's verdict: the WORST dimension, never the mean.

    A dimension of None is not scored rather than scored zero - the judge was
    unavailable, or excluded as self-judged, and neither is evidence about the
    answer.
    """
    scored = [
        ("relevance", relevance, JUDGE_PASS, JUDGE_WARN),
        ("groundedness", groundedness, JUDGE_PASS, JUDGE_WARN),
        ("actionability", actionability, JUDGE_ACTIONABILITY_PASS, JUDGE_ACTIONABILITY_WARN),
    ]
    present = [(n, v, p, w) for n, v, p, w in scored if v is not None]
    if not present:
        return Verdict(Outcome.PASS, "judge: no verdict")

    worst = Verdict(Outcome.PASS, "judge: all dimensions pass")
    for name, value, passing, warning in present:
        if value < warning:
            return Verdict(Outcome.FAIL, f"judge {name} {value} - a reader could be misled")
        if value < passing and worst.outcome is Outcome.PASS:
            worst = Verdict(Outcome.WARN, f"judge {name} {value} - noticeably weak")
    return worst


def combine(deterministic: list[Verdict], judge: Verdict) -> tuple[Outcome, str, bool]:
    """The 2x2, resolved.

    Returns (outcome, reason, should_retry).

    A deterministic FAIL blocks whatever the judge said - fluent and wrong is
    the most dangerous output here, and it is precisely the case a judge waves
    through. A judge FAIL never blocks; it queues the answer for remediation,
    because it is one model's opinion of another's work and that is not grounds
    to stop a deploy.
    """
    failed = [v for v in deterministic if v.outcome is Outcome.FAIL]
    if failed:
        return Outcome.FAIL, "; ".join(v.reason for v in failed), True

    if judge.outcome is Outcome.FAIL:
        # Figures correct, answer useless. Delivered, and queued for a retry.
        return Outcome.WARN, judge.reason, True

    warned = [v for v in deterministic if v.outcome is Outcome.WARN]
    if warned or judge.outcome is Outcome.WARN:
        reasons = [v.reason for v in warned] + ([judge.reason] if judge.outcome is Outcome.WARN else [])
        return Outcome.WARN, "; ".join(reasons), False

    return Outcome.PASS, "all checks pass", False
