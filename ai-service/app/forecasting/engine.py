"""Deterministic capacity forecasting - ordinary least squares on daily means.

No ML, no LLM: a straight-line trend fit is transparent, reproducible, and
good enough for a first version per the specification. The LLM layer may
narrate a forecast's numbers; it never generates them (see app/agents/guards.py).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.config import get_settings
from app.models.entities import InfrastructureCluster
from app.models.forecast import ClusterForecast, ResourceForecast
from app.repositories import utilization_repository

TWOPLACES = Decimal("0.01")


def round2(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _ols(x: list[float], y: list[float]) -> tuple[float, float, float, float]:
    """Returns (slope, intercept, r_squared, residual_std_error)."""
    n = len(x)
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    sxx = sum((xi - x_mean) ** 2 for xi in x)
    sxy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    slope = sxy / sxx if sxx != 0 else 0.0
    intercept = y_mean - slope * x_mean

    sse = sum((yi - (intercept + slope * xi)) ** 2 for xi, yi in zip(x, y))
    sst = sum((yi - y_mean) ** 2 for yi in y)
    r_squared = 1.0 - sse / sst if sst != 0 else (1.0 if sse == 0 else 0.0)
    se = math.sqrt(sse / (n - 2)) if n > 2 else 0.0
    return slope, intercept, r_squared, se


def forecast_resource(
    daily_pairs: list[tuple[date, float]],
    *,
    resource: str,
    threshold_percent: float,
    horizon_days: int,
    confidence_z: float,
) -> ResourceForecast:
    if not daily_pairs:
        raise ValueError("forecast_resource requires at least one daily observation")

    ordered = sorted(daily_pairs, key=lambda p: p[0])
    base_ordinal = ordered[0][0].toordinal()
    x = [float(d.toordinal() - base_ordinal) for d, _ in ordered]
    y = [v for _, v in ordered]
    n = len(x)

    slope, intercept, r_squared, se = _ols(x, y)

    x_mean = sum(x) / n
    sxx = sum((xi - x_mean) ** 2 for xi in x) or 1.0

    last_x = x[-1]
    future_x = last_x + horizon_days
    predicted = intercept + slope * future_x
    predicted_clamped = max(0.0, min(150.0, predicted))

    margin = confidence_z * se * math.sqrt(1 + 1 / n + ((future_x - x_mean) ** 2) / sxx) if se > 0 else 0.0
    conf_low = max(0.0, predicted_clamped - margin)
    conf_high = min(200.0, predicted_clamped + margin)

    exhaustion_date = None
    breaches = False
    if slope > 0:
        x_cross = (threshold_percent - intercept) / slope
        if x_cross > last_x:
            days_from_last = x_cross - last_x
            exhaustion_date = ordered[-1][0] + timedelta(days=round(days_from_last))
            breaches = days_from_last <= horizon_days
        elif y[-1] >= threshold_percent:
            # Already at/over threshold as of the last observation.
            exhaustion_date = ordered[-1][0]
            breaches = True

    current = y[-1]
    if predicted_clamped >= threshold_percent:
        action = (
            f"Increase capacity or add nodes before "
            f"{exhaustion_date.isoformat() if exhaustion_date else 'the forecast horizon'} "
            f"- projected {resource} utilization reaches {round(predicted_clamped, 1)}%."
        )
    elif slope > 0 and (threshold_percent - predicted_clamped) < 10:
        action = f"Monitor closely - {resource} utilization is trending upward toward the threshold."
    elif slope < -0.01:
        action = f"{resource.capitalize()} utilization is trending down; no action needed."
    else:
        action = f"{resource.capitalize()} utilization is stable; no action needed."

    return ResourceForecast(
        resource=resource,
        horizon_days=horizon_days,
        current_percent=round2(current),
        predicted_percent=round2(predicted_clamped),
        confidence_low_percent=round2(conf_low),
        confidence_high_percent=round2(conf_high),
        slope_percent_per_day=round2(round(slope, 6)),
        r_squared=round2(round(max(0.0, min(1.0, r_squared)), 6)),
        exhaustion_date=exhaustion_date,
        breaches_threshold_within_horizon=breaches,
        recommended_action=action,
        sample_count=n,
    )


def forecast_cluster(
    cluster: InfrastructureCluster, *, horizon_days: int, history_days: int = 180
) -> ClusterForecast:
    settings = get_settings()
    if horizon_days not in settings.forecast.supported_horizons:
        raise ValueError(f"unsupported horizon {horizon_days}; supported: {settings.forecast.supported_horizons}")

    daily = utilization_repository.get_cluster_daily_means(cluster.ClusterId, history_days)
    if len(daily) < settings.forecast.min_history_days:
        raise ValueError(
            f"insufficient history for cluster {cluster.ClusterCode}: "
            f"{len(daily)} days available, {settings.forecast.min_history_days} required"
        )

    cpu_pairs = [(d, cpu) for d, cpu, _, _ in daily]
    mem_pairs = [(d, mem) for d, _, mem, _ in daily]
    storage_pairs = [(d, storage) for d, _, _, storage in daily]

    z = settings.forecast.confidence_z
    cpu_fc = forecast_resource(
        cpu_pairs, resource="cpu", threshold_percent=settings.policy.cpu_threshold_percent,
        horizon_days=horizon_days, confidence_z=z,
    )
    mem_fc = forecast_resource(
        mem_pairs, resource="memory", threshold_percent=settings.policy.memory_threshold_percent,
        horizon_days=horizon_days, confidence_z=z,
    )
    storage_fc = forecast_resource(
        storage_pairs, resource="storage", threshold_percent=settings.policy.storage_threshold_percent,
        horizon_days=horizon_days, confidence_z=z,
    )

    return ClusterForecast(
        cluster_id=cluster.ClusterId,
        cluster_code=cluster.ClusterCode,
        horizon_days=horizon_days,
        cpu=cpu_fc,
        memory=mem_fc,
        storage=storage_fc,
    )


def cluster_breaches_within_horizon(cluster: InfrastructureCluster, *, horizon_days: int = 90) -> bool:
    try:
        fc = forecast_cluster(cluster, horizon_days=horizon_days)
    except ValueError:
        return False
    return fc.cpu.breaches_threshold_within_horizon or fc.memory.breaches_threshold_within_horizon or fc.storage.breaches_threshold_within_horizon
