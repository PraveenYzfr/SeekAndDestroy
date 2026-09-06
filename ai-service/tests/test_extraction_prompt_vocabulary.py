"""The extraction prompt must name the vocabulary it will be judged against.

_coerce_enum forces every categorical answer onto a real enum member and
replaces anything that misses with a fallback. The model was never told what
the members are, so it was marked wrong against a vocabulary it had not been
given. Measured on production, twice per sentence, identical both runs:

    "an INTERNAL Java app, 8 cores, 32 GB"      -> data_classification None
    "a RESTRICTED payments app, 16 cores"       -> data_classification Restricted
    "a Tier-1 CONFIDENTIAL workload, 32 cores"  -> data_classification Confidential

Restricted and Confidential are unambiguous security words. "Internal" reads as
an ordinary adjective, so the classification an engineer in a bank is most
likely to type was the one that got dropped - and since 691b5ed a dropped
classification fails closed to Restricted, which would have narrowed real
searches for people who HAD said what they meant.

Storage had the same shape for a different reason: storage_gb is gigabytes and
the field name was the only thing that said so, so "2 TB storage" returned null
and _CAPACITY_DEFAULTS supplied 500. A quarter of the request, silently
replaced - and the source of the long-standing "2 TB sometimes becomes 500 GB"
puzzle, which was never non-determinism.

These assert the PROMPT rather than model output: a model call is slow, costs
money and can pass for reasons unrelated to the prompt. What must not drift is
that every accepted value appears in the instructions.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    AvailabilityTier,
    DataClassification,
    Environment,
    TechnologyPlatform,
)
from app.prompts.templates import REQUIREMENT_EXTRACTION_SYSTEM


@pytest.mark.parametrize(
    "enum_cls",
    [Environment, TechnologyPlatform, AvailabilityTier, DataClassification],
)
def test_every_accepted_value_appears_in_the_prompt(enum_cls):
    """Built from the enums, so a new member cannot fall out of the prompt
    while _coerce_enum carries on accepting it."""
    missing = [str(m) for m in enum_cls if str(m) not in REQUIREMENT_EXTRACTION_SYSTEM]
    assert not missing, (
        f"{enum_cls.__name__} members {missing} are accepted by _coerce_enum but "
        "never named in the extraction prompt - the model is being marked wrong "
        "against a vocabulary it was not given"
    )


def test_the_units_are_stated():
    """storage_gb and memory_gb are gigabytes and the field name was the only
    thing that ever said so."""
    assert "GIGABYTES" in REQUIREMENT_EXTRACTION_SYSTEM
    assert "2 TB is 2048" in REQUIREMENT_EXTRACTION_SYSTEM


def test_it_still_forbids_inventing_unstated_values():
    """The vocabulary must not become licence to guess. Both halves have to
    survive together: name the permitted values, and keep null as the answer
    for anything the user did not say."""
    assert "never guess a number that was not stated" in REQUIREMENT_EXTRACTION_SYSTEM
    assert "null if the user did not say" in REQUIREMENT_EXTRACTION_SYSTEM


def test_a_language_is_not_a_platform():
    """The one disambiguation the probe showed the model getting right without
    help, pinned so the vocabulary list does not push it the other way -
    "a Java app" must not become platform=Java."""
    assert "programming language is not a platform" in REQUIREMENT_EXTRACTION_SYSTEM
