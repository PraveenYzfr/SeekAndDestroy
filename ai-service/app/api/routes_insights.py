"""CMDB Insighter endpoint: one free-text question in, one composed answer
out. See app.insights.router for how a question is classified and answered -
this module is only the HTTP wrapper (auth, rate limiting, error mapping).

Every exception app.insights can raise (InsightValidationError,
NumberDriftError, InsightNarrationError, UnknownCiError, NoCiNamedError) is a
ValueError subclass, so app.api.errors' generic ValueError handler already
turns each into a clean 400 - no per-exception handling needed here. That is
deliberate on this feature's part: a refused question or a rejected
narrative must reach the caller as an explicit error, never be caught here
and answered with something safer-looking instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.llm_factory import get_chat_model
from app.api.rate_limit import enforce_llm_rate_limit
from app.security.jwt_service import AuthenticatedEmployee
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["insights"])


class InsightAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


@router.post("/api/insights/ask")
def ask(payload: InsightAskRequest, current: AuthenticatedEmployee = Depends(enforce_llm_rate_limit)):
    from app.insights.router import answer_free_text

    # One configured model for both roles (spec-mapping and narration) -
    # no per-role override exists for this feature yet (see
    # app.agents.roles, which this deliberately does not touch tonight);
    # splitting them is a config-only change later; get_chat_model() is a
    # process-wide cached client, so this call is not a new API request.
    llm = get_chat_model()
    result = answer_free_text(llm, llm, payload.query)
    return to_jsonable(result)
