"""Read-only CMDB / capacity tools. Every query is parameterized inside the
repository layer - there is no way to pass raw SQL through these tools."""

from __future__ import annotations

from tools._audit import audited, model_dict, model_list
from app.repositories import application_repository, cluster_repository, node_repository, utilization_repository
from app.services import capacity, utilization_ranking
from app.utils.json_utils import to_jsonable


def list_data_centers() -> dict:
    """List the distinct data centers clusters are located in - use this to let an engineer
    pick a location before searching for candidates. Returned as {"data_centers": [...]}. (A
    bare list return would come back from MCP as N separate content items instead of one JSON
    array, so every tool in this server returns a dict for a consistent, single-block shape.)"""
    params = dict(locals())
    return audited("list_data_centers", params, lambda: {"data_centers": utilization_ranking.list_data_centers()})


def search_applications(query: str = "", environment: str = "", criticality: str = "", limit: int = 20) -> list[dict]:
    """Search CMDB applications by code/name substring, optionally filtered by environment or criticality."""
    params = dict(locals())
    return audited(
        "search_applications", params,
        lambda: model_list(
            application_repository.search(
                query=query or None, environment=environment or None,
                criticality=criticality or None, limit=min(limit, 200),
            )
        ),
    )


def get_application(application_code: str) -> dict | None:
    """Fetch one application by its ApplicationCode (e.g. APP-PAYMENTS)."""
    params = dict(locals())
    return audited("get_application", params, lambda: model_dict(application_repository.get_by_code(application_code)))


def get_application_requirements(application_code: str) -> dict | None:
    """Return only the hosting-requirement fields of an application (CPU/memory/storage/tier/classification/platform)."""
    params = dict(locals())

    def run():
        app = application_repository.get_by_code(application_code)
        if app is None:
            return None
        return {
            "ApplicationCode": app.ApplicationCode, "Environment": app.Environment,
            "TechnologyPlatform": app.TechnologyPlatform, "OperatingSystemRequirement": app.OperatingSystemRequirement,
            "CpuRequirement": float(app.CpuRequirement), "MemoryRequirementGb": float(app.MemoryRequirementGb),
            "StorageRequirementGb": float(app.StorageRequirementGb),
            "ExpectedAnnualGrowthPercent": float(app.ExpectedAnnualGrowthPercent),
            "AvailabilityTier": app.AvailabilityTier, "DataClassification": app.DataClassification,
            "PreferredLocation": app.PreferredLocation, "BusinessCriticality": app.BusinessCriticality,
        }

    return audited("get_application_requirements", params, run)


def get_current_application_hosting(application_code: str) -> list[dict]:
    """List all hosting records (current and historical) for an application."""
    params = dict(locals())

    def run():
        app = application_repository.get_by_code(application_code)
        if app is None:
            return []
        from app.repositories import hosting_repository

        return model_list(hosting_repository.get_all_for_application(app.ApplicationId))

    return audited("get_current_application_hosting", params, run)


def search_clusters(
    query: str = "", environment: str = "", platform: str = "", availability_tier: str = "", limit: int = 20
) -> list[dict]:
    """Search infrastructure clusters, optionally filtered by environment/platform/availability tier."""
    params = dict(locals())
    return audited(
        "search_clusters", params,
        lambda: model_list(
            cluster_repository.search(
                query=query or None, environment=environment or None, platform=platform or None,
                availability_tier=availability_tier or None, limit=min(limit, 200),
            )
        ),
    )


def get_cluster(cluster_code: str) -> dict | None:
    """Fetch one infrastructure cluster by its ClusterCode (e.g. CL-PROD-03)."""
    params = dict(locals())
    return audited("get_cluster", params, lambda: model_dict(cluster_repository.get_by_code(cluster_code)))


def get_cluster_nodes(cluster_code: str) -> list[dict]:
    """List the nodes belonging to a cluster."""
    params = dict(locals())

    def run():
        cluster = cluster_repository.get_by_code(cluster_code)
        if cluster is None:
            return []
        return model_list(node_repository.get_by_cluster(cluster.ClusterId))

    return audited("get_cluster_nodes", params, run)


def get_cluster_utilization(cluster_code: str, days: int = 30) -> dict:
    """Return the utilization series and window average for a cluster over the last N days."""
    params = dict(locals())

    def run():
        cluster = cluster_repository.get_by_code(cluster_code)
        if cluster is None:
            return {"error": f"cluster {cluster_code} not found"}
        series = utilization_repository.get_cluster_series(cluster.ClusterId, days=min(days, 180))
        avg = utilization_repository.get_cluster_window_average(cluster.ClusterId, min(days, 180))
        return {"cluster_code": cluster_code, "window_average": avg, "series": model_list(series)}

    return audited("get_cluster_utilization", params, run)


def get_node_utilization(host_name: str, days: int = 30) -> dict:
    """Return the utilization series and window average for one node over the last N days."""
    params = dict(locals())

    def run():
        from app.repositories.base import T, fetch_one

        row = fetch_one(f"SELECT * FROM {T('ClusterNode')} WHERE HostName = :h", {"h": host_name})
        if row is None:
            return {"error": f"node {host_name} not found"}
        node_id = row["NodeId"]
        series = utilization_repository.get_node_series(node_id, days=min(days, 180))
        avg = utilization_repository.get_node_window_average(node_id, min(days, 180))
        return {"host_name": host_name, "window_average": avg, "series": model_list(series)}

    return audited("get_node_utilization", params, run)


def get_available_cluster_capacity(cluster_code: str) -> dict:
    """Return the full deterministic capacity snapshot for a cluster (effective/allocated/measured/consumed/available)."""
    params = dict(locals())

    def run():
        cluster = cluster_repository.get_by_code(cluster_code)
        if cluster is None:
            return {"error": f"cluster {cluster_code} not found"}
        snapshot = capacity.compute_cluster_capacity(cluster)
        return to_jsonable(snapshot)

    return audited("get_available_cluster_capacity", params, run)
