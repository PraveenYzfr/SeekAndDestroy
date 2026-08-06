"""Cluster/node utilization queries.

Window averages are computed relative to the *latest available* metric
timestamp for the entity, not wall-clock "now" - seed/historical data is not
guaranteed to reach the current instant, and capacity decisions should always
be grounded in the freshest data actually on hand.
"""

from __future__ import annotations

from datetime import datetime

from app.models.entities import ClusterUtilization, NodeUtilization
from app.repositories.base import T, fetch_all, fetch_one


def get_cluster_series(cluster_id: int, days: int = 180, limit: int = 5000) -> list[ClusterUtilization]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) cu.* FROM {T('ClusterUtilization')} cu
        WHERE cu.ClusterId = :cluster_id
          AND cu.MetricDateTime >= (
              SELECT DATEADD(day, -:days, MAX(MetricDateTime))
              FROM {T('ClusterUtilization')} WHERE ClusterId = :cluster_id
          )
        ORDER BY cu.MetricDateTime
        """,
        {"cluster_id": cluster_id, "days": days, "limit": limit},
    )
    return [ClusterUtilization(**r) for r in rows]


def get_cluster_window_average(cluster_id: int, window_days: int) -> dict[str, float] | None:
    row = fetch_one(
        f"""
        SELECT
            AVG(CpuUsedPercent) AS AvgCpu,
            AVG(MemoryUsedPercent) AS AvgMemory,
            AVG(StorageUsedPercent) AS AvgStorage,
            AVG(NetworkUsedPercent) AS AvgNetwork,
            MAX(MetricDateTime) AS LatestAt,
            COUNT(*) AS SampleCount
        FROM {T('ClusterUtilization')}
        WHERE ClusterId = :cluster_id
          AND MetricDateTime >= (
              SELECT DATEADD(day, -:window_days, MAX(MetricDateTime))
              FROM {T('ClusterUtilization')} WHERE ClusterId = :cluster_id
          )
        """,
        {"cluster_id": cluster_id, "window_days": window_days},
    )
    if not row or row["SampleCount"] == 0 or row["AvgCpu"] is None:
        return None
    return {
        "cpu_used_percent": float(row["AvgCpu"]),
        "memory_used_percent": float(row["AvgMemory"]),
        "storage_used_percent": float(row["AvgStorage"]),
        "network_used_percent": float(row["AvgNetwork"]),
        "latest_at": row["LatestAt"],
        "sample_count": int(row["SampleCount"]),
    }


def get_node_series(node_id: int, days: int = 180, limit: int = 5000) -> list[NodeUtilization]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) nu.* FROM {T('NodeUtilization')} nu
        WHERE nu.NodeId = :node_id
          AND nu.MetricDateTime >= (
              SELECT DATEADD(day, -:days, MAX(MetricDateTime))
              FROM {T('NodeUtilization')} WHERE NodeId = :node_id
          )
        ORDER BY nu.MetricDateTime
        """,
        {"node_id": node_id, "days": days, "limit": limit},
    )
    return [NodeUtilization(**r) for r in rows]


def get_node_window_average(node_id: int, window_days: int) -> dict[str, float] | None:
    row = fetch_one(
        f"""
        SELECT
            AVG(CpuUsedPercent) AS AvgCpu,
            AVG(MemoryUsedPercent) AS AvgMemory,
            AVG(StorageUsedPercent) AS AvgStorage,
            COUNT(*) AS SampleCount
        FROM {T('NodeUtilization')}
        WHERE NodeId = :node_id
          AND MetricDateTime >= (
              SELECT DATEADD(day, -:window_days, MAX(MetricDateTime))
              FROM {T('NodeUtilization')} WHERE NodeId = :node_id
          )
        """,
        {"node_id": node_id, "window_days": window_days},
    )
    if not row or row["SampleCount"] == 0 or row["AvgCpu"] is None:
        return None
    return {
        "cpu_used_percent": float(row["AvgCpu"]),
        "memory_used_percent": float(row["AvgMemory"]),
        "storage_used_percent": float(row["AvgStorage"]),
        "sample_count": int(row["SampleCount"]),
    }


def get_cluster_daily_means(cluster_id: int, days: int) -> list[tuple[datetime, float, float, float]]:
    """One row per calendar day: (day, avg_cpu, avg_mem, avg_storage). Used by forecasting."""
    rows = fetch_all(
        f"""
        SELECT
            CAST(MetricDateTime AS DATE) AS Day,
            AVG(CpuUsedPercent) AS AvgCpu,
            AVG(MemoryUsedPercent) AS AvgMemory,
            AVG(StorageUsedPercent) AS AvgStorage
        FROM {T('ClusterUtilization')}
        WHERE ClusterId = :cluster_id
          AND MetricDateTime >= (
              SELECT DATEADD(day, -:days, MAX(MetricDateTime))
              FROM {T('ClusterUtilization')} WHERE ClusterId = :cluster_id
          )
        GROUP BY CAST(MetricDateTime AS DATE)
        ORDER BY Day
        """,
        {"cluster_id": cluster_id, "days": days},
        max_rows=400,
    )
    return [(r["Day"], float(r["AvgCpu"]), float(r["AvgMemory"]), float(r["AvgStorage"])) for r in rows]
