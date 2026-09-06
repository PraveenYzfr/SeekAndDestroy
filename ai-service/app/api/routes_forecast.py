from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agents.chains import explain_forecast
from app.agents.llm_factory import get_chat_model_for_role
from app.api import narration
from app.api.errors import ProblemDetailsError
from app.api.rate_limit import enforce_llm_rate_limit
from app.api.schemas import ForecastRequest
from app.forecasting.engine import forecast_cluster
from app.repositories import cluster_repository
from app.security.jwt_service import AuthenticatedEmployee
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["forecast"])


@router.post("/api/forecast")
def forecast(payload: ForecastRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
    cluster = cluster_repository.get_by_code(payload.cluster_code)
    if cluster is None:
        raise ProblemDetailsError(404, "Cluster not found", f"No cluster with code {payload.cluster_code!r}.")
    result = forecast_cluster(cluster, horizon_days=payload.horizon_days)
    if not payload.explain:
        return to_jsonable(result)

    # One explanation, for the resource that actually constrains the cluster.
    # Narrating all three costs three calls to say two things nobody asked.
    resource_name, resource = narration.binding_resource(result)
    explanation = narration.safely(
        "explain_forecast",
        lambda: explain_forecast(get_chat_model_for_role("narration"), cluster.ClusterCode, resource),
    )
    return to_jsonable({
        **result.model_dump(),
        "explained_resource": resource_name,
        "explanation": explanation,
    })
