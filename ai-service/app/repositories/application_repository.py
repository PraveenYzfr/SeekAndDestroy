from __future__ import annotations

from app.models.entities import CmdbApplication
from app.repositories.base import T, fetch_all, fetch_one


def get_by_id(application_id: int) -> CmdbApplication | None:
    row = fetch_one(
        f"SELECT * FROM {T('CmdbApplication')} WHERE ApplicationId = :id", {"id": application_id}
    )
    return CmdbApplication(**row) if row else None


def get_by_code(application_code: str) -> CmdbApplication | None:
    row = fetch_one(
        f"SELECT * FROM {T('CmdbApplication')} WHERE ApplicationCode = :code",
        {"code": application_code},
    )
    return CmdbApplication(**row) if row else None


def search(
    *,
    query: str | None = None,
    environment: str | None = None,
    criticality: str | None = None,
    platform: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = 100,
) -> list[CmdbApplication]:
    clauses = []
    params: dict = {"limit": limit}
    if query:
        clauses.append("(ApplicationCode LIKE :q OR ApplicationName LIKE :q)")
        params["q"] = f"%{query}%"
    if environment:
        clauses.append("Environment = :environment")
        params["environment"] = environment
    if criticality:
        clauses.append("BusinessCriticality = :criticality")
        params["criticality"] = criticality
    if platform:
        clauses.append("TechnologyPlatform = :platform")
        params["platform"] = platform
    if lifecycle_status:
        clauses.append("LifecycleStatus = :lifecycle_status")
        params["lifecycle_status"] = lifecycle_status
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('CmdbApplication')} {where} ORDER BY ApplicationCode",
        params,
    )
    return [CmdbApplication(**r) for r in rows]


def list_all(limit: int = 200) -> list[CmdbApplication]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('CmdbApplication')} ORDER BY ApplicationCode", {"limit": limit}
    )
    return [CmdbApplication(**r) for r in rows]


def changed_since(since, last_id: int = 0, limit: int = 500) -> list[CmdbApplication]:
    """One page of rows at or after the cursor ``(since, last_id)``.

    Keyset pagination on ``(UpdatedAt, ApplicationId)``, not OFFSET and not a bare
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
            f"SELECT TOP (:limit) * FROM {T('CmdbApplication')} ORDER BY UpdatedAt, ApplicationId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('CmdbApplication')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND ApplicationId > :last_id) "
            f"ORDER BY UpdatedAt, ApplicationId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [CmdbApplication(**r) for r in rows]
