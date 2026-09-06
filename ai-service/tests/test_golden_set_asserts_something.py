"""A golden case that asserts nothing positive cannot fail for the right reason.

WHAT THIS IS
------------
Seventeen of the hundred cases carry no ``must_contain`` and no ``must_refuse``.
Their only checks are three forbidden hedge phrases:

    must_contain:     []
    must_not_contain: ["I don't have enough", "no information", "cannot answer"]

That is a test which passes as long as the platform does not apologise. It
cannot tell a correct capacity shortlist from a wrong one, an empty one, or a
paragraph about the weather - only from one that hedges.

MEASURED, NOT ARGUED. Four of the seventeen were passing on a ZERO-LENGTH
answer in both pinned baselines:

    run 44:  4 of 17 passed on an empty answer
    run 30:  4 of 17 passed on an empty answer
             capacity-32c-128g, -48c-192g, -64c-256g, -96c-384g

So 77/100 and 74/100 are really 73 and 70. Anyone comparing tomorrow's number
against 77 reads a four-point drop that is entirely the ruler.

The empty-answer half is closed - run_case now reports an ungradeable answer
rather than grading one - but that fixed the SYMPTOM. The remaining thirteen
produce real answers, so nothing is currently wrong with their verdicts; they
simply cannot detect a wrong answer, only a rude one.

WHY A RATCHET AND NOT A FLAT RULE
---------------------------------
Asserting outright that every case has a positive check would fail seventeen
times today and be deleted by the first person who ran it. This pins the
seventeen that exist and fails when an EIGHTEENTH appears, so the shape cannot
spread while the real fix is built.

The real fix is review-aware expectations: capacity, consolidation and
rightsizing cases pause for human review, and the thing to grade is the review
payload the platform actually produced - the ranked candidates, the scores, the
rejection reasons - rather than prose that only exists because the suite
approved its own investigation. When a case gains one, delete it from the list
below. THE LIST IS ALLOWED TO SHRINK AND NOT TO GROW.
"""

from __future__ import annotations

import pathlib

import yaml

#: The seventeen that exist today, verified against the file rather than copied
#: from a report. Shrink this as cases gain real expectations; never extend it.
KNOWN_WITHOUT_POSITIVE_ASSERTION = {
    "capacity-4c-16g", "capacity-8c-32g", "capacity-16c-64g", "capacity-24c-96g",
    "capacity-32c-128g", "capacity-48c-192g", "capacity-64c-256g", "capacity-96c-384g",
    "consolidation-basic", "consolidation-by-dc", "consolidation-prod", "consolidation-retire",
    "rightsizing-over", "rightsizing-prod", "rightsizing-under", "rightsizing-waste",
    "future-speculation",
}


def _cases() -> list[dict]:
    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "evaluation" / "golden_set.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def _asserts_nothing_positive(case: dict) -> bool:
    """No required substring and no required refusal.

    must_not_contain does not count. It is satisfied by silence, and a check
    that silence satisfies is not evidence the platform did anything.
    """
    return not case.get("must_contain") and not case.get("must_refuse")


class TestTheShapeCannotSpread:
    def test_no_new_case_asserts_only_the_absence_of_hedging(self):
        """The ratchet. A new case written in this shape is caught here rather
        than by somebody noticing months later that a green suite proves less
        than it appears to."""
        offenders = {c["id"] for c in _cases() if _asserts_nothing_positive(c)}
        added = offenders - KNOWN_WITHOUT_POSITIVE_ASSERTION
        assert not added, (
            f"new golden case(s) with no must_contain and no must_refuse: {sorted(added)}. "
            f"Their only checks would be forbidden hedge phrases, which silence satisfies. "
            f"Give the case something it must positively contain, or a review-payload "
            f"expectation - see this file's docstring."
        )

    def test_the_list_is_kept_current_as_cases_are_fixed(self):
        """The other direction, and the reason a ratchet is safe to leave in
        place: a stale allowlist quietly re-permits the shape it was written to
        stop. Fixing a case must also remove it from the list."""
        offenders = {c["id"] for c in _cases() if _asserts_nothing_positive(c)}
        fixed = KNOWN_WITHOUT_POSITIVE_ASSERTION - offenders
        assert not fixed, (
            f"case(s) now assert something positive and should be removed from "
            f"KNOWN_WITHOUT_POSITIVE_ASSERTION: {sorted(fixed)}"
        )

    def test_the_count_is_what_was_reported(self):
        """Pins the number that the baseline correction is based on. If this
        moves, the claim that 77/100 is really 73 needs restating rather than
        being carried forward as folklore."""
        offenders = [c["id"] for c in _cases() if _asserts_nothing_positive(c)]
        assert len(offenders) == 17
        assert len(_cases()) == 100


class TestEveryOtherCaseCanActuallyFail:
    def test_the_remaining_cases_all_assert_something(self):
        """Eighty-three of the hundred. Stated as a test so the ratchet is not
        mistaken for a tolerance - the normal case is a case that can fail for
        a reason somebody chose."""
        strong = [c for c in _cases() if not _asserts_nothing_positive(c)]
        assert len(strong) == 83
        for case in strong:
            assert case.get("must_contain") or case.get("must_refuse"), case["id"]
