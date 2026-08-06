from __future__ import annotations

from tools._audit import audited, model_list
from app.repositories import application_repository, cluster_repository, dependency_repository, incident_repository


def get_application_dependencies(application_code: str) -> dict:
    """List outbound and inbound dependencies for an application."""
    params = dict(locals())

    def run():
        app = application_repository.get_by_code(application_code)
        if app is None:
            return {"error": f"application {application_code} not found"}
        return {
            "outbound": model_list(dependency_repository.get_outbound(app.ApplicationId)),
            "inbound": model_list(dependency_repository.get_inbound(app.ApplicationId)),
        }

    return audited("get_application_dependencies", params, run)


def get_recent_incidents(
    application_code: str = "", cluster_code: str = "", days: int = 90
) -> list[dict]:
    """List recent incidents for an application or a cluster (provide exactly one code)."""
    params = dict(locals())

    def run():
        if application_code:
            app = application_repository.get_by_code(application_code)
            if app is None:
                return []
            return model_list(incident_repository.get_recent_for_application(app.ApplicationId, days=min(days, 365)))
        if cluster_code:
            cluster = cluster_repository.get_by_code(cluster_code)
            if cluster is None:
                return []
            return model_list(incident_repository.get_recent_for_cluster(cluster.ClusterId, days=min(days, 365)))
        return []

    return audited("get_recent_incidents", params, run)
