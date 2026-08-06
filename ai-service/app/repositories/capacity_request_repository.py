from __future__ import annotations

from datetime import date
from typing import Any

from app.models.entities import CapacityRequest
from app.repositories.base import T, execute, execute_insert, fetch_all, fetch_one


def get_by_id(capacity_request_id: int) -> CapacityRequest | None:
    row = fetch_one(
        f"SELECT * FROM {T('CapacityRequest')} WHERE CapacityRequestId = :id",
        {"id": capacity_request_id},
    )
    return CapacityRequest(**row) if row else None


def list_open(limit: int = 200) -> list[CapacityRequest]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('CapacityRequest')} "
        f"WHERE Status IN ('Open','InAnalysis') ORDER BY CreatedAt DESC",
        {"limit": limit},
    )
    return [CapacityRequest(**r) for r in rows]


def create(
    *,
    application_id: int | None,
    requested_by: int,
    environment: str,
    required_cpu_cores: float,
    required_memory_gb: float,
    required_storage_gb: float,
    expected_growth_percent: float,
    required_availability_tier: str,
    required_platform: str,
    preferred_location: str | None,
    data_classification: str,
    required_by_date: date | None,
) -> int:
    values: dict[str, Any] = {
        "ApplicationId": application_id,
        "RequestedBy": requested_by,
        "Environment": environment,
        "RequiredCpuCores": required_cpu_cores,
        "RequiredMemoryGb": required_memory_gb,
        "RequiredStorageGb": required_storage_gb,
        "ExpectedGrowthPercent": expected_growth_percent,
        "RequiredAvailabilityTier": required_availability_tier,
        "RequiredPlatform": required_platform,
        "PreferredLocation": preferred_location,
        "DataClassification": data_classification,
        "RequiredByDate": required_by_date,
        "Status": "Open",
    }
    return execute_insert(T("CapacityRequest"), "CapacityRequestId", values)


def update_status(capacity_request_id: int, status: str) -> None:
    execute(
        f"UPDATE {T('CapacityRequest')} SET Status = :status WHERE CapacityRequestId = :id",
        {"status": status, "id": capacity_request_id},
    )
