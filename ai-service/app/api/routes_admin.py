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


def _fallback_for(role_name: str) -> dict:
    """What answers for this role when its primary provider fails."""
    from app.agents.llm_factory import resolve_role

    key = roles.fallback_role_name(role_name)
    resolved = resolve_role(key)
    configured = resolved.get("source") == "override"
    return {
        "role": key,
        "provider": resolved.get("provider") if configured else None,
        "model": resolved.get("model") if configured else None,
        "configured": configured,
    }


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
                # This role's own backup, sent beside it so the screen can show
                # the pair together rather than as two unrelated rows.
                #
                # Per role, not one global spare: extraction wants strict schema
                # adherence and reporting wants readable prose, so a single
                # estate-wide substitute is right for at most one of them. Being
                # wrong for the others surfaces as a quiet change in output
                # quality rather than an error.
                #
                # `configured` False means no backup was chosen. Deliberately not
                # filled in with a default - a fallback nobody selected is a model
                # nobody evaluated, and choosing one automatically turns an outage
                # into a silent change in behaviour.
                "fallback": _fallback_for(role.name),
            }
            for role in roles.ROLES
        ],
        # THIS USED TO CONTRADICT THE SCREEN IT APPEARS ON. It read "Evaluation
        # has no model to configure" while an "Evaluation judge" role was listed
        # directly above it, with its own model selector. Both halves were
        # half-true and together they were misleading - Praveen read it and asked
        # what it meant, which is the only reason it was found.
        #
        # The missing distinction is WHICH evaluation. The checks that decide
        # whether a figure was invented use no model at all. The judge is a
        # separate, narrower thing that does.
        "evaluation_note": (
            "Two different things are called evaluation here.\n\n"
            "The checks that matter use NO model. app/evaluation/graders.py proves by "
            "arithmetic that every figure in an answer traces to a value the engine "
            "computed, and that every cluster or application code was a real candidate. "
            "A model asked the same question could only agree or be wrong.\n\n"
            "The Evaluation judge role above DOES use a model, deliberately limited to "
            "the three things arithmetic cannot see - relevance, groundedness and "
            "actionability. It is never asked about numbers and its schema gives it "
            "nowhere to put a numeric verdict. Point it at a different provider from the "
            "one being judged: a model scoring its own output rates it higher.\n\n"
            "To compare two models: assign one above, run some investigations, then run "
            "scripts/evaluate.py. It grades calls that already happened from "
            "sad.AgentAuditLog, so a full run costs a table scan rather than a provider "
            "bill, and every historical call is already tagged with the model that made it."
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
    if role_name not in roles.ASSIGNABLE_ROLE_NAMES:
        raise ProblemDetailsError(
            404, "Unknown role", f"'{role_name}' is not a model role. Known roles: {sorted(roles.ASSIGNABLE_ROLE_NAMES)}."
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
    if role_name not in roles.ASSIGNABLE_ROLE_NAMES:
        raise ProblemDetailsError(404, "Unknown role", f"'{role_name}' is not a model role.")
    removed = llm_role_repository.clear(role_name)
    reset_role_model_cache()
    # "was already the default" is a different answer from "reset", and the
    # screen should not claim to have changed something it did not.
    return {"role": role_name, "removed": removed, "source": "config"}


@router.get("/api/admin/evaluation")
def get_evaluation(limit: int = 5000, current: AuthenticatedEmployee = Depends(require_admin)):
    """The scorecard, from calls that already happened.

    WHY THIS ENDPOINT EXISTS. The Model Settings screen told an administrator to
    "run scripts/evaluate.py" - and scripts/ is not in the service image, so on
    the deployed system that instruction could not be followed by anyone. The
    capability was real and reachable only by someone with a shell on the box and
    a willingness to import app.evaluation.harness by hand. Which meant that in
    practice nobody could see whether a model change had made answers worse.

    That matters more now than it did a week ago: the point of per-role model
    selection is to move a role onto a faster or cheaper model, and the only
    thing standing between that and "fast and confidently wrong" is this
    scorecard. An acceptance gate nobody can run is not a gate.

    NO MODEL IS CALLED HERE and nothing is spent. The harness reads
    sad.AgentAuditLog and grades text that was already generated and already paid
    for, so a run costs a table scan. That is also why it is safe to expose: the
    expensive, irreversible thing already happened.

    ``limit`` bounds the scan rather than the result. An estate running for a year
    has more audit rows than belong in one response, and the default is chosen to
    cover recent behaviour rather than all history - a lifetime average hides
    exactly the regression this is meant to catch.
    """
    from app.evaluation import harness

    try:
        return harness.evaluate(limit=limit)
    except Exception as exc:  # noqa: BLE001
        # Surfaced rather than swallowed. An empty scorecard and a broken one look
        # identical to a reader, and the difference is whether the platform is
        # behaving or the measurement is.
        raise ProblemDetailsError(
            status=500,
            title="Evaluation could not be computed",
            detail=str(exc)[:500],
        ) from exc
