"""CMDB Insighter endpoint: one free-text question in, one composed answer
out. See app.insights.router for how a question is classified and answered -
this module is only the HTTP wrapper (auth, role gate, rate limiting, error
mapping).

Every exception app.insights can raise (InsightValidationError,
NumberDriftError, InsightNarrationError, UnknownCiError, NoCiNamedError) is a
ValueError subclass, so app.api.errors' generic ValueError handler already
turns each into a clean 400 - no per-exception handling needed here. That is
deliberate on this feature's part: a refused question or a rejected
narrative must reach the caller as an explicit error, never be caught here
and answered with something safer-looking instead.

RBAC, WHAT THIS COVERS AND WHAT IT DOES NOT
---------------------------------------------
require_role("Viewer") means an authenticated employee with no recognised
role (app.api.auth._rank maps that to -1, below every real role) cannot
reach this endpoint at all - before tonight, any valid token could, which is
the actual gap this closes. It is a real, if modest, access gate reusing
the platform's existing role machinery rather than inventing a new one.

What it does NOT do: filter which CIs, incidents or business services a
given caller can see by DataClassification or RegulatoryScope. Every CI
class and classification tier is visible to anyone who clears the Viewer
bar. Building that properly needs an actual clearance policy (which role, or
which support group, may see Restricted-classification aggregates) decided
by whoever owns that policy - inventing role-to-classification mappings
here, under time pressure, with no stated policy to build against, would be
worse than leaving the gap explicit. This comment is that gap, stated rather
than hidden.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.llm_factory import get_chat_model
from app.api.auth import require_role
from app.api.rate_limit import enforce_llm_rate_limit
from app.security.jwt_service import AuthenticatedEmployee
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["insights"])


class InsightAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


@router.post("/api/insights/ask")
def ask(
    payload: InsightAskRequest,
    current: AuthenticatedEmployee = Depends(require_role("Viewer")),
    _rate_limited: AuthenticatedEmployee = Depends(enforce_llm_rate_limit),
):
    from app.insights.router import answer_free_text

    # One configured model for both roles (spec-mapping and narration) -
    # no per-role override exists for this feature yet (see
    # app.agents.roles, which this deliberately does not touch tonight);
    # splitting them is a config-only change later; get_chat_model() is a
    # process-wide cached client, so this call is not a new API request.
    llm = get_chat_model()
    result = answer_free_text(llm, llm, payload.query)
    return to_jsonable(result)
