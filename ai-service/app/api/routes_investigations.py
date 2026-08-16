"""Investigation lifecycle endpoints. These delegate to the LangGraph
``InfrastructureRecommendationGraph`` (app.graph.graph) - imported lazily so
the rest of the API works even while that module is under active development.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from app.utils.json_utils import to_jsonable

from app.api.auth import get_current_employee, require_matching_employee_id
from app.api.errors import ProblemDetailsError
from app.api.schemas import CreateInvestigationRequest, ResumeInvestigationRequest
from app.repositories import conversation_repository, investigation_repository, recommendation_repository
from app.security.jwt_service import AuthenticatedEmployee

router = APIRouter(tags=["investigations"])


def _resolve_conversation(conversation_id: str | None, employee_id: int) -> str:
    """The conversation this message belongs to, created if the caller did not
    name one.

    Ownership is checked here rather than assumed. A conversation carries the
    engineer's previous questions and the results they were shown, so serving
    one to a different employee because they supplied its id would be a
    straightforward information leak - and the ids are server-generated uuid4
    precisely so this check has something to protect.
    """
    if not conversation_id:
        return conversation_repository.create(employee_id)
    conversation = conversation_repository.get(conversation_id)
    if conversation is None:
        raise ProblemDetailsError(
            404, "Conversation not found", f"No conversation with id {conversation_id}."
        )
    if conversation.CreatedBy != employee_id:
        raise ProblemDetailsError(
            403, "Conversation belongs to another employee",
            "You can only continue conversations you started.",
        )
    return conversation.ConversationId


@router.post("/api/investigations")
def create_investigation(payload: CreateInvestigationRequest, current: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.graph.graph import run_investigation

    created_by = require_matching_employee_id(current, payload.created_by_employee_id)
    conversation_id = _resolve_conversation(payload.conversation_id, created_by)
    result = run_investigation(query=payload.query, created_by=created_by, conversation_id=conversation_id)
    return to_jsonable(result)


@router.get("/api/investigations/{investigation_id}")
def get_investigation(investigation_id: int):
    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    return to_jsonable(inv)


@router.post("/api/investigations/{investigation_id}/resume")
def resume_investigation(
    investigation_id: int, payload: ResumeInvestigationRequest,
    current: AuthenticatedEmployee = Depends(get_current_employee),
):
    from app.graph.graph import resume_investigation as graph_resume

    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    reviewer_employee_id = require_matching_employee_id(current, payload.reviewer_employee_id)
    result = graph_resume(
        investigation_id=investigation_id, decision=payload.decision,
        reviewer_employee_id=reviewer_employee_id, comments=payload.comments,
        selected_cluster_code=payload.selected_cluster_code,
        selected_host_name=payload.selected_host_name,
    )
    return to_jsonable(result)


@router.get("/api/investigations/{investigation_id}/recommendations")
def list_investigation_recommendations(investigation_id: int):
    inv = investigation_repository.get_by_id(investigation_id)
    if inv is None:
        raise ProblemDetailsError(404, "Investigation not found", f"No investigation with id {investigation_id}.")
    recs = recommendation_repository.list_for_investigation(investigation_id)
    return to_jsonable({"investigation": inv, "recommendations": recs})
