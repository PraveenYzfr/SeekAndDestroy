"""A flat utilisation series crashed a hosting investigation.

Found by the hundred-case golden run: hosting-app-aml-recon0268 failed with
OverflowError, no answer at all. The cause is not date handling - it is
unbounded extrapolation. A positive but vanishing slope makes the threshold
crossing millions of years out, and timedelta refuses to represent it.

An almost-flat series is the most ordinary shape a healthy cluster has, which is
what makes this worth a test rather than a note.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.forecasting.engine import _MAX_PROJECTION_DAYS, forecast_resource

BASE = date(2026, 6, 1)


def _series(step: float, start: float = 75.0, points: int = 13):
    return [(BASE + timedelta(days=i * 7), start + i * step) for i in range(points)]


def _forecast(pairs, threshold: float = 85.0):
    return forecast_resource(
        pairs, resource="cpu", threshold_percent=threshold,
        horizon_days=90, confidence_z=1.96,
    )


class TestAFlatSeriesDoesNotCrash:
    @pytest.mark.parametrize("step", [1e-7, 1e-9, 1e-12])
    def test_a_vanishing_slope_yields_no_date_rather_than_an_error(self, step):
        """slope 1e-9 with the threshold 10 points away is 9,999,999,910 days -
        27 million years - and timedelta raises OverflowError. The whole
        investigation failed, not just the forecast."""
        assert _forecast(_series(step)).exhaustion_date is None

    def test_a_perfectly_flat_series_is_not_a_breach(self):
        assert _forecast(_series(0.0)).exhaustion_date is None


class TestRealTrendsStillProduceDates:
    def test_a_rising_series_still_names_a_day(self):
        """The cap must not silence forecasts that matter."""
        assert _forecast(_series(1.5, start=70.0)).exhaustion_date is not None

    def test_a_slow_but_real_trend_inside_the_cap_is_kept(self):
        """0.05 per week crosses in about four years - well inside ten, and a
        real capacity-planning answer."""
        result = _forecast(_series(0.05))
        assert result.exhaustion_date is not None
        assert result.exhaustion_date.year < BASE.year + 10

    def test_already_over_threshold_reports_the_last_observation(self):
        assert _forecast(_series(0.1, start=86.0)).exhaustion_date is not None


class TestTheCapIsAboutCredibilityNotOverflow:
    def test_the_ceiling_is_ten_years(self):
        """Not chosen to avoid OverflowError - that would be satisfied by any
        number below 999,999,999. An OLS fit over weeks of utilisation says
        nothing credible a decade out, and naming a specific day beyond that
        invents a number."""
        assert _MAX_PROJECTION_DAYS == 3650

    def test_a_crossing_just_past_the_cap_is_dropped(self):
        # ~11 years out: representable, and not a forecast.
        step = (85.0 - 75.0) / (11 * 52)
        assert _forecast(_series(step)).exhaustion_date is None
