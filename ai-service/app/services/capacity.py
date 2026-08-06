"""Deterministic capacity engine.

Implements the formulas in docs/business-rules.md / IMPLEMENTATION_PLAN.md §5:

    effective_*  = Total*        (adjusted for reservation on CPU/memory)
    allocated_*  = sum(ApplicationHosting.Allocated*) over Active/Migrating rows
    measured_*   = Total* * avg(*UsedPercent over the utilization window) / 100
    consumed_*   = max(allocated_*, measured_*)          -- conservative by design
    available_*  = effective_* - consumed_*
    required_eff = required_* * (1 + growth%)**years * (1 + safety_margin%)
    projected_%  = (consumed_* + required_eff) / effective_* * 100
    headroom_%   = 100 - max(projected_cpu, projected_mem, projected_storage)

Every value here is a ``decimal.Decimal``. This module never imports anything
from ``app.agents`` or ``app.graph`` - the LLM layer only ever *reads* the
output of this module, it never calls into it to "help" compute a number.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.config import get_settings
from app.models.capacity import ClusterCapacitySnapshot, ProjectedUtilization
from app.models.entities import InfrastructureCluster
from app.repositories import hosting_repository, utilization_repository

TWOPLACES = Decimal("0.01")


def d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def compute_cluster_capacity(
    cluster: InfrastructureCluster, *, window_days: int | None = None
) -> ClusterCapacitySnapshot:
    settings = get_settings()
    window = window_days if window_days is not None else settings.policy.utilization_window_days

    effective_cpu = cluster.TotalCpuCores * (Decimal("1") - cluster.ReservedCpuPercent / Decimal("100"))
    effective_mem = cluster.TotalMemoryGb * (Decimal("1") - cluster.ReservedMemoryPercent / Decimal("100"))
    effective_storage = cluster.TotalStorageGb

    hosting_rows = hosting_repository.get_active_for_cluster(cluster.ClusterId)
    allocated_cpu = sum((h.AllocatedCpuCores for h in hosting_rows), Decimal("0"))
    allocated_mem = sum((h.AllocatedMemoryGb for h in hosting_rows), Decimal("0"))
    allocated_storage = sum((h.AllocatedStorageGb for h in hosting_rows), Decimal("0"))

    measured = utilization_repository.get_cluster_window_average(cluster.ClusterId, window)
    if measured:
        measured_cpu_pct = d(measured["cpu_used_percent"])
        measured_mem_pct = d(measured["memory_used_percent"])
        measured_storage_pct = d(measured["storage_used_percent"])
        sample_count = measured["sample_count"]
    else:
        measured_cpu_pct = measured_mem_pct = measured_storage_pct = None
        sample_count = 0

    measured_cpu = effective_cpu * (measured_cpu_pct / Decimal("100")) if measured_cpu_pct is not None else Decimal("0")
    measured_mem = effective_mem * (measured_mem_pct / Decimal("100")) if measured_mem_pct is not None else Decimal("0")
    measured_storage = (
        effective_storage * (measured_storage_pct / Decimal("100")) if measured_storage_pct is not None else Decimal("0")
    )

    consumed_cpu = max(allocated_cpu, measured_cpu)
    consumed_mem = max(allocated_mem, measured_mem)
    consumed_storage = max(allocated_storage, measured_storage)

    available_cpu = effective_cpu - consumed_cpu
    available_mem = effective_mem - consumed_mem
    available_storage = effective_storage - consumed_storage

    current_cpu_util = round2(_safe_div(consumed_cpu, effective_cpu) * Decimal("100"))
    current_mem_util = round2(_safe_div(consumed_mem, effective_mem) * Decimal("100"))
    current_storage_util = round2(_safe_div(consumed_storage, effective_storage) * Decimal("100"))

    return ClusterCapacitySnapshot(
        cluster_id=cluster.ClusterId,
        cluster_code=cluster.ClusterCode,
        effective_cpu_cores=round2(effective_cpu),
        effective_memory_gb=round2(effective_mem),
        effective_storage_gb=round2(effective_storage),
        allocated_cpu_cores=round2(allocated_cpu),
        allocated_memory_gb=round2(allocated_mem),
        allocated_storage_gb=round2(allocated_storage),
        measured_cpu_percent=round2(measured_cpu_pct) if measured_cpu_pct is not None else None,
        measured_memory_percent=round2(measured_mem_pct) if measured_mem_pct is not None else None,
        measured_storage_percent=round2(measured_storage_pct) if measured_storage_pct is not None else None,
        measurement_sample_count=sample_count,
        consumed_cpu_cores=round2(consumed_cpu),
        consumed_memory_gb=round2(consumed_mem),
        consumed_storage_gb=round2(consumed_storage),
        available_cpu_cores=round2(available_cpu),
        available_memory_gb=round2(available_mem),
        available_storage_gb=round2(available_storage),
        current_cpu_utilization_percent=current_cpu_util,
        current_memory_utilization_percent=current_mem_util,
        current_storage_utilization_percent=current_storage_util,
    )


def grow_requirement(required: Decimal, growth_percent: Decimal, horizon_years: int) -> Decimal:
    """required * (1 + growth%/100) ** horizon_years"""
    factor = (Decimal("1") + growth_percent / Decimal("100")) ** horizon_years
    return required * factor


def apply_safety_margin(value: Decimal, safety_margin_percent: Decimal) -> Decimal:
    return value * (Decimal("1") + safety_margin_percent / Decimal("100"))


def compute_effective_requirement(
    required_cpu: Decimal,
    required_memory_gb: Decimal,
    required_storage_gb: Decimal,
    growth_percent: Decimal,
    *,
    horizon_years: int | None = None,
    safety_margin_percent: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    settings = get_settings()
    years = horizon_years if horizon_years is not None else settings.policy.growth_horizon_years
    margin = (
        safety_margin_percent
        if safety_margin_percent is not None
        else d(settings.policy.safety_margin_percent)
    )
    cpu_eff = apply_safety_margin(grow_requirement(required_cpu, growth_percent, years), margin)
    mem_eff = apply_safety_margin(grow_requirement(required_memory_gb, growth_percent, years), margin)
    storage_eff = apply_safety_margin(grow_requirement(required_storage_gb, growth_percent, years), margin)
    return cpu_eff, mem_eff, storage_eff


def compute_projected_utilization(
    snapshot: ClusterCapacitySnapshot,
    cluster: InfrastructureCluster,
    *,
    required_cpu: Decimal,
    required_memory_gb: Decimal,
    required_storage_gb: Decimal,
    growth_percent: Decimal,
    horizon_years: int | None = None,
    safety_margin_percent: Decimal | None = None,
) -> ProjectedUtilization:
    settings = get_settings()

    cpu_eff, mem_eff, storage_eff = compute_effective_requirement(
        required_cpu, required_memory_gb, required_storage_gb, growth_percent,
        horizon_years=horizon_years, safety_margin_percent=safety_margin_percent,
    )

    projected_cpu_pct = round2(
        _safe_div(snapshot.consumed_cpu_cores + cpu_eff, snapshot.effective_cpu_cores) * Decimal("100")
    )
    projected_mem_pct = round2(
        _safe_div(snapshot.consumed_memory_gb + mem_eff, snapshot.effective_memory_gb) * Decimal("100")
    )
    projected_storage_pct = round2(
        _safe_div(snapshot.consumed_storage_gb + storage_eff, snapshot.effective_storage_gb) * Decimal("100")
    )

    headroom = round2(Decimal("100") - max(projected_cpu_pct, projected_mem_pct, projected_storage_pct))

    cpu_threshold = d(settings.policy.cpu_threshold_percent)
    mem_threshold = d(settings.policy.memory_threshold_percent)
    storage_threshold = d(settings.policy.storage_threshold_percent)

    fits_cpu = projected_cpu_pct < cpu_threshold
    fits_mem = projected_mem_pct < mem_threshold
    fits_storage = projected_storage_pct < storage_threshold

    return ProjectedUtilization(
        required_cpu_cores_effective=round2(cpu_eff),
        required_memory_gb_effective=round2(mem_eff),
        required_storage_gb_effective=round2(storage_eff),
        projected_cpu_utilization_percent=projected_cpu_pct,
        projected_memory_utilization_percent=projected_mem_pct,
        projected_storage_utilization_percent=projected_storage_pct,
        projected_headroom_percent=headroom,
        fits_cpu=fits_cpu,
        fits_memory=fits_mem,
        fits_storage=fits_storage,
        fits_all=fits_cpu and fits_mem and fits_storage,
    )
