from __future__ import annotations

from fastapi import APIRouter

from app.api.errors import ProblemDetailsError
from app.repositories import application_repository, cluster_repository, hosting_repository, utilization_repository
from app.services import capacity, utilization_ranking
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["cmdb"])


@router.get("/api/clusters/data-centers")
def get_data_centers():
    """Distinct data-center "neighborhoods" available to filter by - powers
    the location picker on the Hosting Recommendation / Dashboard screens.
    """
    return to_jsonable({"data_centers": utilization_ranking.list_data_centers()})


@router.get("/api/clusters/utilization-ranking")
def get_utilization_ranking(
    order: str = "least", limit: int = 10, environment: str | None = None,
    data_center: str | None = None, platform: str | None = None,
):
    if order not in ("least", "most"):
        raise ProblemDetailsError(400, "Invalid order", "order must be 'least' or 'most'.")
    results = utilization_ranking.rank_clusters_by_utilization(
        order=order, limit=limit, environment=environment, data_center=data_center, platform=platform,
    )
    return to_jsonable({"order": order, "limit": limit, "results": results})


@router.get("/api/applications/{application_id}/hosting")
def get_application_hosting(application_id: int):
    app = application_repository.get_by_id(application_id)
    if app is None:
        raise ProblemDetailsError(404, "Application not found", f"No application with id {application_id}.")
    hostings = hosting_repository.get_all_for_application(application_id)
    return to_jsonable({"application": app, "hosting": hostings})


@router.get("/api/clusters/{cluster_id}/capacity")
def get_cluster_capacity(cluster_id: int):
    cluster = cluster_repository.get_by_id(cluster_id)
    if cluster is None:
        raise ProblemDetailsError(404, "Cluster not found", f"No cluster with id {cluster_id}.")
    snapshot = capacity.compute_cluster_capacity(cluster)
    return to_jsonable({"cluster": cluster, "capacity": snapshot})


@router.get("/api/clusters/{cluster_id}/utilization")
def get_cluster_utilization(cluster_id: int, days: int = 30):
    cluster = cluster_repository.get_by_id(cluster_id)
    if cluster is None:
        raise ProblemDetailsError(404, "Cluster not found", f"No cluster with id {cluster_id}.")
    series = utilization_repository.get_cluster_series(cluster_id, days=days)
    return to_jsonable({"cluster_code": cluster.ClusterCode, "days": days, "series": series})
