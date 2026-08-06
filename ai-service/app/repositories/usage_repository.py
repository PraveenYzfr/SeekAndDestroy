from __future__ import annotations

from app.models.entities import ApplicationUsage
from app.repositories.base import T, fetch_all, fetch_one


def get_series(application_id: int, days: int = 180, limit: int = 5000) -> list[ApplicationUsage]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) au.* FROM {T('ApplicationUsage')} au
        WHERE au.ApplicationId = :application_id
          AND au.UsageDateTime >= (
              SELECT DATEADD(day, -:days, MAX(UsageDateTime))
              FROM {T('ApplicationUsage')} WHERE ApplicationId = :application_id
          )
        ORDER BY au.UsageDateTime
        """,
        {"application_id": application_id, "days": days, "limit": limit},
    )
    return [ApplicationUsage(**r) for r in rows]


def get_window_average(application_id: int, window_days: int) -> dict[str, float] | None:
    row = fetch_one(
        f"""
        SELECT
            AVG(CpuConsumed) AS AvgCpu,
            AVG(MemoryConsumedGb) AS AvgMemory,
            AVG(StorageConsumedGb) AS AvgStorage,
            AVG(CAST(ResponseTimeMs AS FLOAT)) AS AvgResponseMs,
            COUNT(*) AS SampleCount
        FROM {T('ApplicationUsage')}
        WHERE ApplicationId = :application_id
          AND UsageDateTime >= (
              SELECT DATEADD(day, -:window_days, MAX(UsageDateTime))
              FROM {T('ApplicationUsage')} WHERE ApplicationId = :application_id
          )
        """,
        {"application_id": application_id, "window_days": window_days},
    )
    if not row or row["SampleCount"] == 0 or row["AvgCpu"] is None:
        return None
    return {
        "cpu_consumed": float(row["AvgCpu"]),
        "memory_consumed_gb": float(row["AvgMemory"]),
        "storage_consumed_gb": float(row["AvgStorage"]),
        "avg_response_time_ms": float(row["AvgResponseMs"]),
        "sample_count": int(row["SampleCount"]),
    }
