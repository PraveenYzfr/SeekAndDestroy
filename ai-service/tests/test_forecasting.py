from __future__ import annotations

from datetime import date

from app.forecasting.engine import forecast_resource, cluster_breaches_within_horizon
from app.repositories import cluster_repository


def test_flat_series_never_breaches():
    pairs = [(date(2026, 1, 1 + i), 20.0) for i in range(30)]
    result = forecast_resource(pairs, resource="cpu", threshold_percent=75.0, horizon_days=90, confidence_z=1.96)
    assert result.breaches_threshold_within_horizon is False
    assert result.exhaustion_date is None


def test_rising_series_breaches_within_horizon():
    pairs = [(date(2026, 1, 1 + i), 50.0 + i * 0.5) for i in range(30)]  # 50% -> 64.5% over 30 days
    result = forecast_resource(pairs, resource="cpu", threshold_percent=75.0, horizon_days=90, confidence_z=1.96)
    assert result.breaches_threshold_within_horizon is True
    assert result.exhaustion_date is not None


def test_designated_forecast_clusters_breach_within_90_days(scenarios):
    for code in scenarios["forecast_exhaustion_clusters"]:
        cluster = cluster_repository.get_by_code(code)
        assert cluster_breaches_within_horizon(cluster, horizon_days=90), f"{code} was designated to breach within 90 days"


def test_designated_overprovisioned_clusters_do_not_breach(scenarios):
    for code in scenarios["overprovisioned_clusters"]:
        cluster = cluster_repository.get_by_code(code)
        assert not cluster_breaches_within_horizon(cluster, horizon_days=90)
