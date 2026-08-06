"""Ranks clusters by current utilization - the "which clusters are least/most
used" view an infra engineer wants when browsing by data center rather than
starting from a specific application or requirement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.repositories import cluster_repository
from app.services import capacity


class ClusterUtilizationRank(BaseModel):
    cluster_id: int
    cluster_code: str
    cluster_name: str
    environment: str
    data_center: str
    region: str
    platform: str
    availability_tier: str
    node_count: int
    monthly_cost: Decimal
    current_cpu_utilization_percent: Decimal
    current_memory_utilization_percent: Decimal
    current_storage_utilization_percent: Decimal
    overall_utilization_percent: Decimal  # max of the three - the binding constraint


def rank_clusters_by_utilization(
    *, order: Literal["least", "most"] = "least", limit: int = 10,
    environment: str | None = None, data_center: str | None = None, platform: str | None = None,
) -> list[ClusterUtilizationRank]:
    clusters = cluster_repository.search(
        environment=environment, data_center=data_center, platform=platform,
        exclude_ineligible_lifecycle=True, limit=500,
    )

    ranked: list[ClusterUtilizationRank] = []
    for cluster in clusters:
        snapshot = capacity.compute_cluster_capacity(cluster)
        overall = max(
            snapshot.current_cpu_utilization_percent,
            snapshot.current_memory_utilization_percent,
            snapshot.current_storage_utilization_percent,
        )
        ranked.append(
            ClusterUtilizationRank(
                cluster_id=cluster.ClusterId, cluster_code=cluster.ClusterCode, cluster_name=cluster.ClusterName,
                environment=cluster.Environment, data_center=cluster.DataCenter, region=cluster.Region,
                platform=cluster.Platform, availability_tier=cluster.AvailabilityTier, node_count=cluster.NodeCount,
                monthly_cost=cluster.MonthlyCost,
                current_cpu_utilization_percent=snapshot.current_cpu_utilization_percent,
                current_memory_utilization_percent=snapshot.current_memory_utilization_percent,
                current_storage_utilization_percent=snapshot.current_storage_utilization_percent,
                overall_utilization_percent=overall,
            )
        )

    reverse = order == "most"
    ranked.sort(key=lambda r: (r.overall_utilization_percent, r.cluster_code), reverse=reverse)
    return ranked[:limit] if limit > 0 else ranked


def list_data_centers() -> list[str]:
    clusters = cluster_repository.list_all(limit=500)
    return sorted({c.DataCenter for c in clusters})
