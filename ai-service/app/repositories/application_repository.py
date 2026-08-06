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
