from __future__ import annotations

from decimal import Decimal

from app.repositories import cluster_repository
from app.services import capacity


def test_effective_capacity_accounts_for_reservation():
    cluster = cluster_repository.get_by_code("nyc-03")
    snapshot = capacity.compute_cluster_capacity(cluster)
    expected_effective_cpu = (cluster.TotalCpuCores * (1 - cluster.ReservedCpuPercent / 100)).quantize(Decimal("0.01"))
    assert snapshot.effective_cpu_cores == expected_effective_cpu


def test_consumed_is_max_of_allocated_and_measured():
    cluster = cluster_repository.get_by_code("atl-03")
    snapshot = capacity.compute_cluster_capacity(cluster)
    measured_cpu = snapshot.effective_cpu_cores * (snapshot.measured_cpu_percent / 100)
    assert snapshot.consumed_cpu_cores == max(snapshot.allocated_cpu_cores, measured_cpu.quantize(Decimal("0.01")))


def test_growth_and_safety_margin_compound_correctly():
    cpu_eff, _mem_eff, _sto_eff = capacity.compute_effective_requirement(
        Decimal("100"), Decimal("100"), Decimal("100"), growth_percent=Decimal("20"),
        horizon_years=1, safety_margin_percent=Decimal("10"),
    )
    # 100 * 1.20 * 1.10 = 132
    assert cpu_eff == Decimal("132.0000")


def test_headroom_is_100_minus_max_projected():
    cluster = cluster_repository.get_by_code("den-03")
    snapshot = capacity.compute_cluster_capacity(cluster)
    projected = capacity.compute_projected_utilization(
        snapshot, cluster, required_cpu=Decimal("5"), required_memory_gb=Decimal("20"),
        required_storage_gb=Decimal("100"), growth_percent=Decimal("0"),
    )
    expected = Decimal("100") - max(
        projected.projected_cpu_utilization_percent,
        projected.projected_memory_utilization_percent,
        projected.projected_storage_utilization_percent,
    )
    assert projected.projected_headroom_percent == expected
