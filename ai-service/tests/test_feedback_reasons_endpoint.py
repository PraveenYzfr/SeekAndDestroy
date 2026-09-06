"""The reason list has one owner, and the endpoint proves it.

WHY THIS EXISTS
---------------
The reasons a person may give for a rating were written down twice: as REASONS
in answer_feedback_repository.py and as FEEDBACK_REASONS in the UI's
types/index.ts. Both lists were correct. Nothing enforced that they stayed
correct together.

A reason added on one side only makes the rating 400 with "unknown reason", and
the row silently does not save - which is indistinguishable from the missing
GRANT UPDATE that made sad.AnswerFeedback unwritable for a day while step 6b
reported no drift. The Owner session hit the 400 by hand and flagged it.

It is the same shape as a deploy guard keyed on a process name written in
another file: a promise held by convention, with nothing failing when it
breaks. Both were fixed the same way - make the check depend on something the
subject owns, so there is no copy to drift.

WHAT THE UI KEEPS
-----------------
FEEDBACK_REASONS survives as a FALLBACK, rendered only if the fetch fails, so a
person can still say what went wrong when the endpoint is unreachable. Going
stale then costs a LABEL, not a rejected rating: the ids are validated
server-side either way, and a control offering no reasons at all is worse than
one offering slightly old ones.
"""

from __future__ import annotations

from app.api.routes_investigations import feedback_reasons
from app.repositories.answer_feedback_repository import REASONS


def _payload():
    """Called directly, not over HTTP. The dependency is an auth marker and the
    route body neither reads nor branches on it - going through the client would
    test FastAPI's wiring rather than this list."""
    return feedback_reasons(current=None)


class TestTheEndpointServesTheOneList:
    def test_every_reason_the_repository_accepts_is_offered(self):
        """The failure this closes: a reason the server would accept that the UI
        never shows is a person unable to say what was actually wrong."""
        assert [r["id"] for r in _payload()["reasons"]] == list(REASONS)

    def test_it_offers_nothing_the_repository_would_reject(self):
        """And the opposite failure, which is worse because it is visible only
        to the person whose rating vanished: an option that 400s when clicked."""
        for reason in _payload()["reasons"]:
            assert reason["id"] in REASONS

    def test_a_reason_added_to_the_tuple_appears_without_a_label(self, monkeypatch):
        """Driven from REASONS rather than from the label dict, so adding a
        reason cannot silently omit it. A missing label is cosmetic; a missing
        OPTION is the defect this file exists for."""
        import app.repositories.answer_feedback_repository as repo

        monkeypatch.setattr(repo, "REASONS", (*REASONS, "brand_new_reason"))
        offered = {r["id"]: r["label"] for r in _payload()["reasons"]}
        assert "brand_new_reason" in offered
        assert offered["brand_new_reason"] == "brand_new_reason"

    def test_every_shipped_reason_has_a_human_label(self):
        """Not the id echoed back. An id is a contract, not something to show
        somebody being asked what went wrong."""
        for reason in _payload()["reasons"]:
            assert reason["label"] != reason["id"], f"{reason['id']} has no label"
            assert reason["label"][0].isupper()


class TestTheUiFallbackDoesNotDrift:
    """The UI keeps a bundled copy for when the fetch fails. It is not the
    source of truth, but a fallback that offers an id the server rejects is a
    rating that 400s - so the two are checked against each other here, in the
    one place that can see both.

    Reads the .ts file as text deliberately. The alternative is trusting that
    somebody remembers, which is exactly what failed.
    """

    def test_the_bundled_fallback_matches_the_server(self):
        import re
        from pathlib import Path

        ui_types = Path(__file__).resolve().parents[2] / "ui" / "src" / "types" / "index.ts"
        if not ui_types.exists():
            # The image ships no ui/ directory, so this cannot run in the
            # container. Skipping is stated rather than silent: a check that
            # quietly does not run is the failure mode this whole file is about.
            import pytest

            pytest.skip(f"ui/src/types/index.ts not present at {ui_types} - source checkout only")

        block = ui_types.read_text(encoding="utf-8").split("export const FEEDBACK_REASONS = [", 1)[1]
        block = block.split("] as const", 1)[0]
        bundled = re.findall(r'id:\s*"([^"]+)"', block)
        assert bundled == list(REASONS), (
            "ui/src/types/index.ts FEEDBACK_REASONS has drifted from "
            "answer_feedback_repository.REASONS - a fallback offering an id the "
            "server rejects is a rating that 400s"
        )
