"""Tests for app.insights.date_resolver.

The motivating bug: deepseek-v4-flash, asked to map "how many changes failed
last month" on the real date 2026-09-02, returned opened_after=2026-04-01 -
five months wrong, with nothing about the output that would look wrong to a
reader. These tests fix ``now`` explicitly rather than relying on the actual
clock, so they are not date-dependent themselves.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.insights.date_resolver import resolve_relative_dates

_FIXED_NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_no_relative_phrase_returns_none():
    assert resolve_relative_dates("how many Sev1 incidents are there", now=_FIXED_NOW) is None


def test_last_n_days():
    after, before = resolve_relative_dates("how many incidents in the last 30 days", now=_FIXED_NOW)
    assert after == "2026-08-03"
    assert before == "2026-09-02"


def test_yesterday():
    after, before = resolve_relative_dates("how many incidents opened yesterday", now=_FIXED_NOW)
    assert after == "2026-09-01"
    assert before == "2026-09-02"


def test_last_week():
    after, before = resolve_relative_dates("how many changes failed last week", now=_FIXED_NOW)
    assert after == "2026-08-26"
    assert before == "2026-09-02"


def test_last_month_is_the_previous_calendar_month_not_thirty_days_ago():
    """The exact case that motivated this module: 'last month' on
    2026-09-02 means all of August, not August 3 through September 2."""
    after, before = resolve_relative_dates("how many changes failed last month", now=_FIXED_NOW)
    assert after == "2026-08-01"
    assert before == "2026-09-01"


def test_last_month_crosses_a_year_boundary():
    january_now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    after, before = resolve_relative_dates("incidents from last month", now=january_now)
    assert after == "2025-12-01"
    assert before == "2026-01-01"


def test_this_month():
    after, before = resolve_relative_dates("incidents this month", now=_FIXED_NOW)
    assert after == "2026-09-01"
    assert before == "2026-09-02"


def test_this_quarter():
    # 2026-09-02 is in Q3 (Jul-Sep).
    after, before = resolve_relative_dates("Sev1 incidents this quarter", now=_FIXED_NOW)
    assert after == "2026-07-01"
    assert before == "2026-09-02"


def test_last_quarter():
    after, before = resolve_relative_dates("incidents last quarter", now=_FIXED_NOW)
    assert after == "2026-04-01"
    assert before == "2026-07-01"


def test_last_quarter_crosses_a_year_boundary():
    # 2026-02-10 is in Q1 (Jan-Mar); last quarter is Q4 of the PRIOR year.
    january_now = datetime(2026, 2, 10, tzinfo=timezone.utc)
    after, before = resolve_relative_dates("incidents last quarter", now=january_now)
    assert after == "2025-10-01"
    assert before == "2026-01-01"


def test_this_quarter_does_not_match_this_month():
    """'this quarter' must not be shadowed by an earlier, less specific
    check - a real risk given both phrases contain 'this' and a time unit."""
    after, before = resolve_relative_dates("incidents this quarter", now=_FIXED_NOW)
    assert after == "2026-07-01"  # quarter start, not month start (2026-09-01)


def test_today():
    after, before = resolve_relative_dates("incidents opened today", now=_FIXED_NOW)
    assert after == "2026-09-02"
    assert before == "2026-09-03"


def test_case_insensitive():
    assert resolve_relative_dates("Incidents From LAST WEEK", now=_FIXED_NOW) is not None
