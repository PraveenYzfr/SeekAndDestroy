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


def created_after_id(last_id: int | None, limit: int = 500) -> list[ApplicationDependency]:
    """One page of dependencies inserted after ``last_id``.

    Followed by IDENTITY because sad.ApplicationDependency has no timestamp
    column. That makes the cursor trivially exact - identities are unique and
    monotonic - and it also means this finds inserts and can never find an edit:
    flipping IsCritical or IsActive on an existing row leaves its document stale
    until a full rebuild. Adding CreatedAt/UpdatedAt to the table is the fix.
    """
    if last_id is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ApplicationDependency')} ORDER BY DependencyId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ApplicationDependency')} "
            f"WHERE DependencyId > :last_id ORDER BY DependencyId",
            {"last_id": last_id, "limit": limit},
            max_rows=limit,
        )
    return [ApplicationDependency(**r) for r in rows]
