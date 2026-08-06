from __future__ import annotations

from app.models.entities import ApplicationDependency
from app.repositories.base import T, fetch_all


def get_outbound(application_id: int) -> list[ApplicationDependency]:
    """Dependencies where this application is the source (what it depends on)."""
    rows = fetch_all(
        f"SELECT * FROM {T('ApplicationDependency')} "
        f"WHERE SourceApplicationId = :id AND IsActive = 1",
        {"id": application_id},
    )
    return [ApplicationDependency(**r) for r in rows]


def get_inbound(application_id: int) -> list[ApplicationDependency]:
    """Dependencies where this application is the target (what depends on it)."""
    rows = fetch_all(
        f"SELECT * FROM {T('ApplicationDependency')} "
        f"WHERE TargetApplicationId = :id AND IsActive = 1",
        {"id": application_id},
    )
    return [ApplicationDependency(**r) for r in rows]


def get_cluster_pinned(application_id: int) -> list[ApplicationDependency]:
    rows = fetch_all(
        f"SELECT * FROM {T('ApplicationDependency')} "
        f"WHERE SourceApplicationId = :id AND TargetClusterId IS NOT NULL AND IsActive = 1",
        {"id": application_id},
    )
    return [ApplicationDependency(**r) for r in rows]
