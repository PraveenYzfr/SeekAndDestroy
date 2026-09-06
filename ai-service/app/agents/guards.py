"""The single most important safety property of the AI layer: the LLM may
narrate a number, but it may never change one.

:func:`assert_no_number_drift` compares every numeric field the LLM produced
against the frozen evidence dict it was given and raises
:class:`NumberDriftError` on any mismatch beyond a tiny float tolerance. Every
explanation-generating chain in app/agents calls this immediately after
parsing the model's structured output, before the result is returned to a
caller or persisted.

AND IT NOW LEAVES A TRACE. This module imported no logger, and the handler that
turns the rejection into a response did not log either - so the single most
safety-critical event in the platform was legible ONLY to the person who
triggered it. api/errors.py has logged the handled case since 28c7f75; this logs
at the point of detection, which is the only place that sees a drift caught by a
caller that swallows it.

Both, deliberately. A caller catching NumberDriftError and continuing is exactly
the path where no handler runs, and that is the path where a silent drift would
matter most.
"""

from __future__ import annotations

import re

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class NumberDriftError(ValueError):
    """Raised when the LLM's output disagrees with the deterministic evidence it was given.

    THE MESSAGE IS DIAGNOSTIC AND MUST NOT REACH A CALLER. It names the field,
    the value the model produced, the evidence key and THE ENGINE-COMPUTED
    FIGURE - which is exactly what an attacker probing this endpoint wants.

    It subclasses ValueError because the platform treats it as a rejected input
    rather than a crash, and app.api.errors renders ValueError as a 400. That
    inheritance is what silently turned this diagnostic string into a response
    body: `handle_value_error` returned `str(exc)` verbatim, so tripping the
    guard handed the caller an internal score.

    app.api.errors now has a dedicated handler that keeps this message for the
    LOG and sends the caller a message that says what happened without saying
    what the number was. Do not add `public_detail = True` here.
    """

    #: Read by app.api.errors. False - and the absence of the attribute means
    #: the same thing - so a new exception is private unless it opts in.
    public_detail = False


def _to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


def _find_evidence_key(evidence: dict, field_name: str) -> str | None:
    target = _to_snake(field_name)
    for key in evidence:
        if _to_snake(key) == target:
            return key
    return None


def assert_no_number_drift(explanation: BaseModel, evidence: dict, *, tolerance: float = 0.01) -> None:
    # Did this call COMPARE anything, as opposed to merely run? The loop below
    # skips a field for four separate reasons, and a call that skips every field
    # used to be counted as a clean pass - see the outcome at the bottom.
    compared = False

    for field_name, value in explanation.model_dump().items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        evidence_key = _find_evidence_key(evidence, field_name)
        if evidence_key is None:
            continue
        expected = evidence[evidence_key]
        if expected is None:
            continue
        try:
            expected_f = float(expected)
        except (TypeError, ValueError):
            continue
        compared = True
        if abs(float(value) - expected_f) > tolerance:
            # Counted here, not at the five call sites. A metric that each caller
            # has to remember is one that will be missed at whichever call site is
            # added next - and the miss would look like an improvement in the
            # hallucination rate.
            _count_drift(explanation, "drift")
            # LOGGED WHERE IT IS DETECTED. The figures go to the operator; the
            # caller gets a message that says what happened without saying what
            # the number was - see app/api/errors.py::handle_number_drift.
            logger.warning(
                "guards.number_drift_rejected",
                schema=type(explanation).__name__,
                field=field_name,
                model_value=value,
                evidence_key=evidence_key,
                evidence_value=expected_f,
            )
            raise NumberDriftError(
                f"LLM output field '{field_name}' = {value} does not match evidence "
                f"'{evidence_key}' = {expected_f}. Rejecting explanation - numbers must come "
                f"from the deterministic engines only."
            )

    # THE DENOMINATOR, AND IT USED TO LIE BY A WHOLE CATEGORY.
    #
    # Without a denominator the drift counter says how many were rejected and
    # never what share that is - "12 rejections" means nothing without knowing
    # whether it was out of 20 narrations or 20,000. That part was right.
    #
    # But "ok" was recorded whether or not a single field had been COMPARED. The
    # loop skips a field when it is not numeric, when _find_evidence_key finds no
    # matching key, when the evidence value is None, and when it will not parse
    # as a float. A call that skipped every field on every one of those grounds
    # scored exactly like a call that checked twenty figures and found them all
    # correct.
    #
    # So the hallucination panel could read a confident 0% over ZERO actual
    # comparisons, and that is not a hypothetical: sad_narration_drift_total
    # {outcome="drift"} has never emitted a single series in production.
    #
    # Three outcomes, because "we looked and it was fine" and "we looked at
    # nothing" are different facts and only one of them is reassuring. A rising
    # `unchecked` share means the guard is passing answers it never inspected -
    # most likely because the evidence keys stopped matching the field names,
    # which is a silent failure of the safety property this module exists for.
    _count_drift(explanation, "ok" if compared else "unchecked")


def _count_drift(explanation, outcome: str) -> None:
    """Record one drift check. Never raises: observability must not be able to
    fail a request it is only watching."""
    try:
        from app.observability.metrics import narration_drift_total

        narration_drift_total.labels(
            schema=type(explanation).__name__, outcome=outcome
        ).inc()
    except Exception:  # noqa: BLE001
        pass
