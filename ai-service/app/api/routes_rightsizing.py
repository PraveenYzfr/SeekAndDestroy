from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agents.chains import explain_application_right_sizing
from app.agents.llm_factory import get_chat_model_for_role
from app.api import narration
from app.api.errors import ProblemDetailsError
from app.api.rate_limit import enforce_llm_rate_limit
from app.api.schemas import (
    ApplicationRightSizingRequest,
    ClusterRightSizingRequest,
    ConsolidationAnalysisRequest,
)
from app.repositories import application_repository, cluster_repository
from app.security.jwt_service import AuthenticatedEmployee
from app.services import consolidation, rightsizing
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["right-sizing"])


@router.post("/api/right-sizing/clusters")
def right_size_clusters(payload: ClusterRightSizingRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
    if payload.cluster_code:
        cluster = cluster_repository.get_by_code(payload.cluster_code)
        if cluster is None:
            raise ProblemDetailsError(404, "Cluster not found", f"No cluster with code {payload.cluster_code!r}.")
        clusters = [cluster]
    else:
        clusters = cluster_repository.list_all(limit=500)
    results = [rightsizing.analyze_cluster_right_sizing(c) for c in clusters]
    #  One ranking, shared with the graph, so the API and the chat answer
    #  cannot disagree about which cluster is the best candidate. The old key
    #  here sorted Overprovisioned first and then by estimated_monthly_savings,
    #  which no longer exists - and which ranked a cluster with nothing to do
    #  (node_delta 0, floored by N-1 tolerance) above a real reduction.
    return to_jsonable(
        {"results": rightsizing.rank_right_sizing(to_jsonable(results))}
    )


@router.post("/api/right-sizing/applications")
def right_size_applications(payload: ApplicationRightSizingRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
    if payload.application_code:
        app = application_repository.get_by_code(payload.application_code)
        if app is None:
            raise ProblemDetailsError(404, "Application not found", f"No application with code {payload.application_code!r}.")
        apps = [app]
    else:
        apps = application_repository.list_all(limit=200)
    results = [rightsizing.analyze_application_right_sizing(a) for a in apps]
    results = [r for r in results if r is not None]
    if not payload.explain:
        return to_jsonable({"results": results})

    # Only the ones with a finding, and only the first few: "Rightsized" needs
    # no explanation, and 200 applications is 200 model calls.
    flagged = [r for r in results if r.classification != "Rightsized"][: narration.MAX_NARRATED]
    explanations = [
        e for e in (
            narration.safely(
                "explain_application_right_sizing",
                lambda r=r: explain_application_right_sizing(get_chat_model_for_role("narration"), r),
            )
            for r in flagged
        ) if e is not None
    ]
    return to_jsonable({
        "results": results,
        "explanations": explanations,
        "explained_count": len(explanations),
        "unexplained_count": max(0, len(results) - len(flagged)),
    })


@router.post("/api/consolidation/analyze")
def analyze_consolidation(payload: ConsolidationAnalysisRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
    apps = application_repository.search(environment=payload.environment, limit=200) if payload.environment else application_repository.list_all(limit=200)
    results = consolidation.find_consolidation_candidates(apps)
    feasible = [r for r in results if r.feasible]
    return to_jsonable({"feasible_count": len(feasible), "results": results})
