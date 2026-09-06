"""Investigation lifecycle endpoints. These delegate to the LangGraph
``InfrastructureRecommendationGraph`` (app.graph.graph) - imported lazily so
the rest of the API works even while that module is under active development.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from app.utils.json_utils import to_jsonable

from app.api.auth import get_current_employee, require_matching_employee_id
from app.api.errors import ProblemDetailsError
from app.api.rate_limit import enforce_llm_rate_limit
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
def create_investigation(payload: CreateInvestigationRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
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
    current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit),
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


class AnswerFeedbackRequest(BaseModel):
    """One person's verdict on one answer.

    Rating is the only required field. Reason and comment are optional because
    demanding a reason is how a feedback control stops being used - and a
    thumbs-up with no explanation is still the data point that matters.
    """

    rating: int = Field(ge=-1, le=1, description="-1 unhelpful, 0 unsure, +1 helpful")
    reason: str | None = None
    comment: str | None = None
    conversation_id: str | None = None


@router.post("/api/investigations/{investigation_id}/feedback")
def submit_feedback(
    investigation_id: int,
    payload: AnswerFeedbackRequest,
    current: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Record what the person who read this answer thought of it.

    THE ONLY GROUND TRUTH THIS PLATFORM HAS. Fidelity is arithmetic,
    completeness is field presence, and the judge is one model's opinion of
    another's work - none of them has ever been checked against a human. This is
    what makes "is the judge worth its cost" answerable with data instead of
    argument.

    Not admin-gated. The person who read the report is the one qualified to say
    whether it helped, and that is rarely an administrator. The employee id
    comes from the token, never the payload, so nobody can rate as somebody else.
    """
    from app.repositories import answer_feedback_repository

    try:
        answer_feedback_repository.record(
            employee_id=current.employee_id,
            rating=payload.rating,
            investigation_id=investigation_id,
            conversation_id=payload.conversation_id,
            reason=payload.reason,
            comment=payload.comment,
        )
    except ValueError as exc:
        # A bad reason is the caller's mistake, not a server fault, and the
        # message names the accepted set rather than saying "invalid".
        raise ProblemDetailsError(400, "Invalid feedback", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT swallowed, unlike the evaluation writes next door.
        # A rating is a person's deliberate act: dropping it silently means they
        # clicked, saw nothing, and stopped bothering - and the data that would
        # have told us the judge is wrong never arrives.
        raise ProblemDetailsError(
            500, "Feedback could not be recorded", str(exc)[:500]
        ) from exc

    return {"investigation_id": investigation_id, "rating": payload.rating, "recorded": True}


@router.get("/api/feedback/reasons")
def feedback_reasons(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """The reasons a person may give for a rating, from the ONE place they are
    defined.

    The UI carried its own copy of this list. Both were correct, and nothing
    enforced that they stayed correct together - a reason added on one side and
    not the other makes the rating 400 and the row silently not save, which
    looks exactly like the missing UPDATE grant that made this table unwritable
    for a day.

    That is the same shape as a deploy guard keyed on a process name written in
    another file: a promise held by convention, with nothing failing when it
    breaks. The fix in both cases is to make the check depend on something the
    subject owns, so the copy cannot drift because there is no copy.

    Ids are the contract and are never translated. Labels are display text and
    live here rather than in the UI so that adding a reason is one change in one
    file - the point of the endpoint.

    Authenticated, like every other route on this router: the list is not
    sensitive, and an unauthenticated exception would be one more path to
    reason about for no gain.
    """
    from app.repositories.answer_feedback_repository import REASONS

    labels = {
        "wrong_numbers": "A figure looked wrong",
        "wrong_entity": "Wrong cluster or application",
        "missing_evidence": "Missing evidence",
        "did_not_answer": "Did not answer the question",
        "not_actionable": "Nothing I can act on",
        "too_slow": "Too slow",
        "other": "Something else",
    }
    # Driven from REASONS, not from the dict, so a reason added to the tuple
    # appears immediately - with its id as the label if nobody wrote one. A
    # missing label is a cosmetic gap; a missing OPTION is a person unable to
    # say what was wrong.
    return {"reasons": [{"id": r, "label": labels.get(r, r)} for r in REASONS]}


@router.get("/api/investigations/{investigation_id}/feedback")
def get_my_feedback(
    investigation_id: int,
    current: AuthenticatedEmployee = Depends(get_current_employee),
):
    """This person's own rating, so the control renders in the state they left
    rather than resetting and inviting a second, contradictory vote."""
    from app.repositories import answer_feedback_repository

    row = answer_feedback_repository.mine(investigation_id, current.employee_id)
    if not row:
        return {"investigation_id": investigation_id, "rating": None}
    return {
        "investigation_id": investigation_id,
        "rating": row["Rating"],
        "reason": row["Reason"],
        "comment": row["Comment"],
    }
