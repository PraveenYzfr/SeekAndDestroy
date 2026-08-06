from __future__ import annotations

from fastapi import APIRouter
from app.utils.json_utils import to_jsonable

from app.api.errors import ProblemDetailsError
from app.api.schemas import ForecastRequest
from app.forecasting.engine import forecast_cluster
from app.repositories import cluster_repository

router = APIRouter(tags=["forecast"])


@router.post("/api/forecast")
def forecast(payload: ForecastRequest):
    cluster = cluster_repository.get_by_code(payload.cluster_code)
    if cluster is None:
        raise ProblemDetailsError(404, "Cluster not found", f"No cluster with code {payload.cluster_code!r}.")
    result = forecast_cluster(cluster, horizon_days=payload.horizon_days)
    return to_jsonable(result)
