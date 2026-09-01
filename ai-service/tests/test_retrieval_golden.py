"""The identifier cases must be able to fail.

This file exists because they could not, and nothing said so.

Every chunk carries its ticket number in a contextual prefix, so a query for
that number matches all of its chunks on an exact token. If nothing else in the
corpus mentions that number, exact matching wins no matter how bad retrieval is.
The three original cases took the lowest IncidentIds and all three landed in the
2,117 incidents that nothing references - so the sparse score they produced was
a measurement of our own prefix format.

Difficulty here is competition, not construction. A query for INC1008825 is a
real test because nine other tickets cite it: those chunks carry the query term
too, and the retriever has to rank the ticket above the tickets discussing it.

These tests read the database like the other repository-backed tests. They never
touch an embedding provider or an LLM - build_cases is pure SQL and a regex.
"""

from __future__ import annotations

import pytest

from app.evaluation import retrieval_golden

MIN_COMPETITORS = 5


@pytest.fixture(scope="module")
def built():
    cases = retrieval_golden.build_cases(limit_per_kind=3)
    if not cases:
        pytest.skip("ITSM corpus not loaded")
    cited, leaks = retrieval_golden._citation_counts()
    return cases, cited, leaks


class TestCorpusInvariant:
    def test_no_note_repeats_its_own_ticket_number(self, built):
        """The condition that made every identifier case unfalsifiable.

        If this fails, the corpus regressed and no identifier number below is
        worth reading - the query term would be printed into the answer's own
        body as well as its prefix.
        """
        _, _, leaks = built
        assert leaks == {}, f"{len(leaks)} ticket(s) cite their own number in their own notes"


class TestIdentifierCasesCanFail:
    def test_every_identifier_case_has_real_competition(self, built):
        cases, cited, _ = built
        ids = [c for c in cases if c.kind == "exact_identifier"]
        assert ids, "no identifier cases were derived"
        for case in ids:
            competitors = cited.get(case.query, 0)
            assert competitors >= MIN_COMPETITORS, (
                f"{case.query} is cited by {competitors} other tickets. Below "
                f"{MIN_COMPETITORS} there is not enough competing text for the case "
                f"to fail, and it measures the prefix format instead of retrieval."
            )

    def test_the_original_selection_would_not_pass_this(self, built):
        """Regression pin on the actual bug.

        Selection used to be `ORDER BY IncidentId` with no citation filter. The
        first ticket in that order is cited by nothing, so the old query would
        still produce it today and the old bug would be back silently.
        """
        _, cited, _ = built
        assert cited.get("INC1000001", 0) == 0
        assert "INC1000001" not in {
            c.query for c in built[0] if c.kind == "exact_identifier"
        }, "selection regressed to id order - this ticket has no competitors"


class TestControl:
    def test_the_control_is_cited_by_nothing(self, built):
        """Its whole value is being unfalsifiable. A control with competitors
        would just be a fourth real case, and the gap it exists to expose would
        silently become meaningless rather than visibly wrong."""
        cases, cited, _ = built
        controls = [c for c in cases if c.kind == "control_prefix_match"]
        assert len(controls) == 1, "exactly one control, or the headline exclusion is wrong"
        assert cited.get(controls[0].query, 0) == 0

    def test_the_control_is_not_also_a_scored_case(self, built):
        """Same query in both kinds would double-count it and put an
        unfalsifiable result into the headline mean."""
        cases, _, _ = built
        control = {c.query for c in cases if c.kind == "control_prefix_match"}
        real = {c.query for c in cases if c.kind == "exact_identifier"}
        assert not (control & real)


class TestCitationCounting:
    def test_two_mentions_in_one_note_count_once(self):
        """A citing ticket is one competitor however often it says the number."""
        text = "duplicate of INC1008825, see INC1008825 for the timeline"
        assert len(set(retrieval_golden._INC_REF_RE.findall(text))) == 1

    def test_boundaries_hold(self):
        """`\b` was silently written to this file as a backspace byte once, which
        made the pattern match nothing while still looking correct in a grep."""
        assert retrieval_golden._INC_REF_RE.findall("see INC1008825.") == ["INC1008825"]
        assert retrieval_golden._INC_REF_RE.findall("xINC1008825") == []
