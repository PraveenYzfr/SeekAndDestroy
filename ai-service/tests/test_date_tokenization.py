"""A date copied verbatim is not three ungrounded numbers.

number_fidelity extracts figures from prose and checks each against the evidence.
It was splitting "2026-07-01" into 2026, -07 and -01 - two of which are negative
numbers nothing can ever ground - so a narrator that quoted a date ACCURATELY,
straight from the evidence, failed the fidelity check for doing the right thing.

Found by the insights narrator, which worked around it by describing windows in
words and reported the root cause here rather than patching around it twice.

Dates are excluded the same way entity codes already were: they identify a
window, they do not measure one.
"""

from __future__ import annotations

from app.evaluation.graders import _numbers_in, number_fidelity


class TestDatesAreNotMeasurements:
    def test_an_iso_date_yields_no_numbers(self):
        assert _numbers_in("the window 2026-07-01 to 2026-09-30") == []

    def test_a_slash_date_yields_no_numbers(self):
        assert _numbers_in("on 01/07/2026") == []
        assert _numbers_in("on 2026/07/01") == []

    def test_a_quarter_label_yields_no_numbers(self):
        """Q3's 3 is a label, not a count."""
        assert _numbers_in("in Q3") == []

    def test_a_copied_date_no_longer_fails_fidelity(self):
        evidence = {"rows": [{"window_start": "2026-07-01", "window_end": "2026-09-30", "incidents": 412}]}
        r = number_fidelity("Between 2026-07-01 and 2026-09-30 there were 412 incidents.", evidence)
        assert r.ungrounded == []
        assert r.total == 1, "only the incident count is a measurement"


class TestRealNumbersStillCounted:
    def test_a_score_beside_a_quarter_survives(self):
        assert _numbers_in("score 98.33 in Q3") == ["98.33"]

    def test_a_thousands_separator_survives(self):
        assert _numbers_in("1,234 incidents") == ["1,234"]

    def test_a_plain_count_survives(self):
        assert _numbers_in("48 cores") == ["48"]

    def test_a_two_part_range_is_not_mistaken_for_a_date(self):
        """The date patterns require three components, so a range keeps both
        numbers rather than vanishing."""
        assert _numbers_in("between 10-20 cores") == ["10", "-20"]

    def test_an_invented_figure_beside_a_date_is_still_caught(self):
        """Excluding dates must not become a way to smuggle a number past the
        guard by parking it next to one."""
        evidence = {"rows": [{"window_start": "2026-07-01", "incidents": 412}]}
        r = number_fidelity("Since 2026-07-01 there were 9,999 incidents.", evidence)
        assert "9,999" in r.ungrounded
