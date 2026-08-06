from __future__ import annotations

from fastapi import APIRouter
from app.utils.json_utils import to_jsonable

from app.api.errors import ProblemDetailsError
from app.api.schemas import ApplicationRightSizingRequest, ClusterRightSizingRequest, ConsolidationAnalysisRequest
from app.repositories import application_repository, cluster_repository
from app.services import consolidation, rightsizing

router = APIRouter(tags=["right-sizing"])


@router.post("/api/right-sizing/clusters")
def right_size_clusters(payload: ClusterRightSizingRequest):
    if payload.cluster_code:
        cluster = cluster_repository.get_by_code(payload.cluster_code)
        if cluster is None:
            raise ProblemDetailsError(404, "Cluster not found", f"No cluster with code {payload.cluster_code!r}.")
        clusters = [cluster]
    else:
        clusters = cluster_repository.list_all(limit=500)
    results = [rightsizing.analyze_cluster_right_sizing(c) for c in clusters]
    results.sort(key=lambda r: (r.classification != "Overprovisioned", -float(r.estimated_monthly_savings)))
    return to_jsonable({"results": results})


@router.post("/api/right-sizing/applications")
def right_size_applications(payload: ApplicationRightSizingRequest):
    if payload.application_code:
        app = application_repository.get_by_code(payload.application_code)
        if app is None:
            raise ProblemDetailsError(404, "Application not found", f"No application with code {payload.application_code!r}.")
        apps = [app]
    else:
        apps = application_repository.list_all(limit=200)
    results = [rightsizing.analyze_application_right_sizing(a) for a in apps]
    results = [r for r in results if r is not None]
    return to_jsonable({"results": results})


@router.post("/api/consolidation/analyze")
def analyze_consolidation(payload: ConsolidationAnalysisRequest):
    apps = application_repository.search(environment=payload.environment, limit=200) if payload.environment else application_repository.list_all(limit=200)
    results = consolidation.find_consolidation_candidates(apps)
    feasible = [r for r in results if r.feasible]
    return to_jsonable({"feasible_count": len(feasible), "results": results})
