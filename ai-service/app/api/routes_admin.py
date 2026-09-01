"""Administrator endpoints: which model runs each role.

Every route here is behind require_admin. These change what model the platform
uses and therefore what it spends, so they are not something an ordinary
authenticated employee should reach.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents import provider_models, roles
from app.agents.llm_factory import reset_role_model_cache, resolve_all_roles
from app.api.auth import require_admin
from app.api.errors import ProblemDetailsError
from app.repositories import llm_role_repository
from app.security.jwt_service import AuthenticatedEmployee

router = APIRouter(tags=["admin"])


class RoleAssignment(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)


@router.get("/api/admin/model-roles")
def get_model_roles(current: AuthenticatedEmployee = Depends(require_admin)):
    """Every role, its effective model, and where that model came from.

    ``source`` is "config" or "override" - the distinction the Reset control
    acts on, and the one that tells an operator whether changing
    config/settings.py would affect this role at all.

    ``chains`` lists the functions each role routes, so the choice is traceable
    to real behaviour instead of to a label somebody has to guess the meaning of.
    """
    resolved = {r["role"]: r for r in resolve_all_roles()}
    return {
        "roles": [
            {
                "name": role.name,
                "title": role.title,
                "description": role.description,
                "chains": list(role.chains),
                **resolved.get(role.name, {}),
            }
            for role in roles.ROLES
        ],
        # Stated here rather than left for someone to infer from the absence of
        # an "evaluation" row. app.evaluation.graders uses no model on purpose;
        # an LLM-as-judge would introduce the failure it is measuring.
        "evaluation_note": (
            "Evaluation has no model to configure. app/evaluation/graders.py grades "
            "deterministically, and scripts/evaluate.py scores whichever models these "
            "roles used, from recorded calls in sad.AgentAuditLog."
        ),
    }


@router.get("/api/admin/model-providers")
def get_model_providers(refresh: bool = False, current: AuthenticatedEmployee = Depends(require_admin)):
    """What each provider will actually serve, asked at runtime.

    Never a hardcoded list - see app/agents/provider_models.py for the five
    times a stale name broke this estate. A provider that cannot be reached is
    returned with available=false and its reason, so the screen can say why
    instead of showing an empty dropdown.
    """
    return {"providers": provider_models.list_all(refresh=refresh)}


@router.put("/api/admin/model-roles/{role_name}")
def set_model_role(
    role_name: str,
    assignment: RoleAssignment,
    current: AuthenticatedEmployee = Depends(require_admin),
):
    """Point a role at a specific provider and model.

    Validated against the provider's live listing rather than accepted as typed:
    storing a name the provider does not serve turns a bad save into a failed
    investigation later, somewhere with no visible connection to this screen.
    """
    if role_name not in roles.ROLE_NAMES:
        raise ProblemDetailsError(
            404, "Unknown role", f"'{role_name}' is not a model role. Known roles: {sorted(roles.ROLE_NAMES)}."
        )
    if assignment.provider not in provider_models.LISTABLE:
        raise ProblemDetailsError(
            400, "Unknown provider",
            f"'{assignment.provider}' is not a supported provider. Supported: {list(provider_models.LISTABLE)}.",
        )

    listing = provider_models.list_models(assignment.provider)
    if listing["available"] and assignment.model not in listing["models"]:
        raise ProblemDetailsError(
            400, "Unknown model",
            f"{assignment.provider} does not currently serve '{assignment.model}'. "
            f"It may have been retired - reload the model list and choose again.",
        )

    llm_role_repository.set_override(
        role_name, assignment.provider, assignment.model, current.employee_number
    )
    # The cache is keyed on (provider, model), so a stale entry would keep
    # serving the previous model until the process restarted.
    reset_role_model_cache()
    return {
        "role": role_name,
        "provider": assignment.provider,
        "model": assignment.model,
        "source": "override",
        # True when the provider could not be reached to confirm the name. Said
        # plainly rather than implied, because an unverified save can still fail
        # at run time.
        "unverified": not listing["available"],
    }


@router.delete("/api/admin/model-roles/{role_name}")
def clear_model_role(role_name: str, current: AuthenticatedEmployee = Depends(require_admin)):
    """Drop the override so the role follows config/settings.py again."""
    if role_name not in roles.ROLE_NAMES:
        raise ProblemDetailsError(404, "Unknown role", f"'{role_name}' is not a model role.")
    removed = llm_role_repository.clear(role_name)
    reset_role_model_cache()
    # "was already the default" is a different answer from "reset", and the
    # screen should not claim to have changed something it did not.
    return {"role": role_name, "removed": removed, "source": "config"}
