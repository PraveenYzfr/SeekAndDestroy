"""The single most important safety property of the AI layer: the LLM may
narrate a number, but it may never change one.

:func:`assert_no_number_drift` compares every numeric field the LLM produced
against the frozen evidence dict it was given and raises
:class:`NumberDriftError` on any mismatch beyond a tiny float tolerance. Every
explanation-generating chain in app/agents calls this immediately after
parsing the model's structured output, before the result is returned to a
caller or persisted.
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class NumberDriftError(ValueError):
    """Raised when the LLM's output disagrees with the deterministic evidence it was given."""


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
        if abs(float(value) - expected_f) > tolerance:
            raise NumberDriftError(
                f"LLM output field '{field_name}' = {value} does not match evidence "
                f"'{evidence_key}' = {expected_f}. Rejecting explanation - numbers must come "
                f"from the deterministic engines only."
            )
