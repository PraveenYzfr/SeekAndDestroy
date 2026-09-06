from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import get_current_employee, require_matching_employee_id
from app.api.errors import ProblemDetailsError
from app.api.schemas import RecommendationDecisionRequest
from app.models.enums import DecisionType, RecommendationStatus
from app.repositories import recommendation_repository
from app.security.jwt_service import AuthenticatedEmployee
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["recommendations"])

_DECISION_TO_STATUS = {
    DecisionType.APPROVE: RecommendationStatus.APPROVED,
    DecisionType.REJECT: RecommendationStatus.REJECTED,
    DecisionType.REQUEST_MORE_ANALYSIS: RecommendationStatus.MORE_ANALYSIS,
}


@router.post("/api/recommendations/{recommendation_id}/decision")
def submit_decision(
    recommendation_id: int, payload: RecommendationDecisionRequest,
    current: AuthenticatedEmployee = Depends(get_current_employee),
):
    rec = recommendation_repository.get_by_id(recommendation_id)
    if rec is None:
        raise ProblemDetailsError(404, "Recommendation not found", f"No recommendation with id {recommendation_id}.")
    if payload.decision not in (DecisionType.APPROVE, DecisionType.REJECT, DecisionType.REQUEST_MORE_ANALYSIS):
        raise ProblemDetailsError(400, "Invalid decision", f"Decision must be one of Approve, Reject, RequestMoreAnalysis; got {payload.decision!r}.")
    reviewer_employee_id = require_matching_employee_id(current, payload.reviewer_employee_id)

    decision_id = recommendation_repository.save_decision(
        recommendation_id=recommendation_id, decision=payload.decision,
        decision_reason=payload.reason, decided_by=reviewer_employee_id,
    )
    new_status = _DECISION_TO_STATUS[payload.decision]
    recommendation_repository.update_status(recommendation_id, new_status)
    updated = recommendation_repository.get_by_id(recommendation_id)
    return to_jsonable({"decision_id": decision_id, "recommendation": updated})


@router.get("/api/recommendations")
def list_recommendations(status: str | None = None, limit: int = 200):
    return to_jsonable({"results": recommendation_repository.list_recent(status=status, limit=limit)})
