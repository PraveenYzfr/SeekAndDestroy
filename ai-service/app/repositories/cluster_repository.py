from __future__ import annotations

from app.models.entities import InfrastructureCluster
from app.models.enums import INELIGIBLE_LIFECYCLE_STATES
from app.repositories.base import T, fetch_all, fetch_one


def get_by_id(cluster_id: int) -> InfrastructureCluster | None:
    row = fetch_one(
        f"SELECT * FROM {T('InfrastructureCluster')} WHERE ClusterId = :id", {"id": cluster_id}
    )
    return InfrastructureCluster(**row) if row else None


def get_by_code(cluster_code: str) -> InfrastructureCluster | None:
    row = fetch_one(
        f"SELECT * FROM {T('InfrastructureCluster')} WHERE ClusterCode = :code",
        {"code": cluster_code},
    )
    return InfrastructureCluster(**row) if row else None


def search(
    *,
    query: str | None = None,
    environment: str | None = None,
    platform: str | None = None,
    availability_tier: str | None = None,
    region: str | None = None,
    data_center: str | None = None,
    exclude_ineligible_lifecycle: bool = True,
    limit: int = 100,
) -> list[InfrastructureCluster]:
    clauses = []
    params: dict = {"limit": limit}
    if query:
        clauses.append("(ClusterCode LIKE :q OR ClusterName LIKE :q)")
        params["q"] = f"%{query}%"
    if environment:
        clauses.append("Environment = :environment")
        params["environment"] = environment
    if platform:
        clauses.append("Platform = :platform")
        params["platform"] = platform
    if availability_tier:
        clauses.append("AvailabilityTier = :availability_tier")
        params["availability_tier"] = availability_tier
    if region:
        clauses.append("Region = :region")
        params["region"] = region
    if data_center:
        clauses.append("DataCenter = :data_center")
        params["data_center"] = data_center
    if exclude_ineligible_lifecycle:
        placeholders = []
        for i, status in enumerate(sorted(INELIGIBLE_LIFECYCLE_STATES)):
            key = f"excl_{i}"
            placeholders.append(f":{key}")
            params[key] = status
        clauses.append(f"LifecycleStatus NOT IN ({', '.join(placeholders)})")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('InfrastructureCluster')} {where} ORDER BY ClusterCode",
        params,
    )
    return [InfrastructureCluster(**r) for r in rows]


def list_all(limit: int = 500) -> list:
    # max_rows must track limit. fetch_all defaults its cap to
    # service.max_rows (500), so a caller asking for 5,000 got a
    # RowLimitExceeded instead of 5,000 rows - invisible until the
    # estate grew past 500 applications.
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('InfrastructureCluster')} ORDER BY ClusterCode",
        {"limit": limit}, max_rows=limit,
    )
    return [InfrastructureCluster(**r) for r in rows]


def changed_since(since, last_id: int = 0, limit: int = 500) -> list[InfrastructureCluster]:
    """One page of rows at or after the cursor ``(since, last_id)``.

    Keyset pagination on ``(UpdatedAt, ClusterId)``, not OFFSET and not a bare
    timestamp comparison. Both alternatives are wrong here:

    * ``WHERE UpdatedAt > :since`` alone loses rows whenever a page boundary
      falls inside a group sharing one timestamp - the next query excludes the
      whole group, so those rows are skipped permanently rather than late.
    * OFFSET re-scans everything it has already skipped, so the last page of a
      large corpus costs the most exactly when the run is most likely to be
      interrupted.

    The cursor is exact, which is what lets the caller persist it after every
    batch and resume from it rather than restarting.

    ``since=None`` means "never indexed": the first page starts at the beginning.
    """
    if since is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('InfrastructureCluster')} ORDER BY UpdatedAt, ClusterId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('InfrastructureCluster')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND ClusterId > :last_id) "
            f"ORDER BY UpdatedAt, ClusterId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [InfrastructureCluster(**r) for r in rows]
