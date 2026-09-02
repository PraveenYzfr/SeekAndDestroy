"""Deterministic relative-date resolution - never left to the LLM.

WHY THIS EXISTS
----------------
Asked to map "how many changes failed last month" on the real date
2026-09-02, deepseek-v4-flash (this platform's configured provider) returned
opened_after=2026-04-01, opened_before=2026-05-01 - five months off, with no
sign anything was wrong. That is the same class of failure as an LLM
computing a count: the model has no reliable notion of "now", and a wrong
date range does not look wrong to a reader the way a wrong count sometimes
does, because there is nothing else in the answer to check it against.

So common relative-date phrases are recognised here, in Python, against the
real system clock, and the result OVERRIDES whatever the LLM produced for
the same question - never merged with or used to sanity-check the model's
own attempt, because the model's attempt cannot be trusted to be right even
approximately.

A question with no recognised phrase returns None, meaning "no confident
override" - the caller falls back to whatever explicit date the model (or a
direct caller of InsightQuerySpec) already supplied, which is a difference
in kind from "no date filter wanted at all".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: "last N day(s)" - the only pattern with a number in it, checked first so
#: it is not shadowed by the fixed-phrase checks below.
_LAST_N_DAYS = re.compile(r"\blast (\d+) days?\b")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_relative_dates(question: str, *, now: datetime | None = None) -> tuple[str, str] | None:
    """(opened_after, opened_before) as ISO dates if ``question`` contains a
    recognised relative-date phrase, else None.

    ``now`` is injectable for tests - never for production callers, who must
    use the real clock, which is the entire point of this function existing.
    """
    text = question.lower()
    current = now or _now()

    match = _LAST_N_DAYS.search(text)
    if match:
        days = int(match.group(1))
        after = current - timedelta(days=days)
        return after.date().isoformat(), current.date().isoformat()

    if "yesterday" in text:
        after = current - timedelta(days=1)
        return after.date().isoformat(), current.date().isoformat()

    if "last week" in text:
        after = current - timedelta(days=7)
        return after.date().isoformat(), current.date().isoformat()

    if "last month" in text:
        # The previous CALENDAR month, not "30 days ago" - "last month" on
        # September 2nd means all of August, not August 3rd through
        # September 2nd.
        first_of_this_month = current.replace(day=1)
        if first_of_this_month.month == 1:
            first_of_last_month = first_of_this_month.replace(year=first_of_this_month.year - 1, month=12)
        else:
            first_of_last_month = first_of_this_month.replace(month=first_of_this_month.month - 1)
        return first_of_last_month.date().isoformat(), first_of_this_month.date().isoformat()

    if "this month" in text:
        first_of_this_month = current.replace(day=1)
        return first_of_this_month.date().isoformat(), current.date().isoformat()

    if "last quarter" in text:
        # The previous CALENDAR quarter (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec),
        # not "the last 90 days" - same reasoning as "last month" above.
        first_of_this_quarter_month = ((current.month - 1) // 3) * 3 + 1
        first_of_this_quarter = current.replace(month=first_of_this_quarter_month, day=1)
        if first_of_this_quarter_month == 1:
            first_of_last_quarter = first_of_this_quarter.replace(year=first_of_this_quarter.year - 1, month=10)
        else:
            first_of_last_quarter = first_of_this_quarter.replace(month=first_of_this_quarter_month - 3)
        return first_of_last_quarter.date().isoformat(), first_of_this_quarter.date().isoformat()

    if "this quarter" in text:
        first_of_this_quarter_month = ((current.month - 1) // 3) * 3 + 1
        first_of_this_quarter = current.replace(month=first_of_this_quarter_month, day=1)
        return first_of_this_quarter.date().isoformat(), current.date().isoformat()

    if "today" in text:
        return current.date().isoformat(), (current + timedelta(days=1)).date().isoformat()

    return None
