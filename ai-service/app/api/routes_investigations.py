"""Investigation lifecycle endpoints. These delegate to the LangGraph
``InfrastructureRecommendationGraph`` (app.graph.graph) - imported lazily so
the rest of the API works even while that module is under active development.
"""

from __future__ import annotations

from fastapi import APIRouter
from app.utils.json_utils import to_jsonable

from app.api.errors import ProblemDetailsError
from app.api.schemas import CreateInvestigationRequest, ResumeInvestigationRequest
from app.repositories import investigation_repository, recommendation_repository

router = APIRouter(tags=["investigations"])


@router.post("/api/investigations")
def create_investigation(payload: CreateInvestigationRequest):
    from app.graph.graph import run_investigation

    result = run_investigation(query=payload.query, created_by=payload.created_by_employee_id)
    return to_jsonable(result)


@router.get("/api/investigations/{investigation_id}")
def get_investigation(investigation_id: int):
    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    return to_jsonable(inv)


@router.post("/api/investigations/{investigation_id}/resume")
def resume_investigation(investigation_id: int, payload: ResumeInvestigationRequest):
    from app.graph.graph import resume_investigation as graph_resume

    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    result = graph_resume(
        investigation_id=investigation_id, decision=payload.decision,
        reviewer_employee_id=payload.reviewer_employee_id, comments=payload.comments,
    )
    return to_jsonable(result)


@router.get("/api/investigations/{investigation_id}/recommendations")
def list_investigation_recommendations(investigation_id: int):
    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    recs = recommendation_repository.list_for_investigation(investigation_id)
    return to_jsonable({"investigation": inv, "recommendations": recs})
