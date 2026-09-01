from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends

from app.agents.chains import summarize_tradeoffs
from app.agents.llm_factory import get_chat_model_for_role
from app.api import narration
from app.api.auth import get_current_employee, require_matching_employee_id
from app.api.errors import ProblemDetailsError
from app.api.schemas import CapacityRecommendationRequest, HostingRecommendationRequest, QuickRecommendationRequest
from app.config import get_settings
from app.models.requirements import HostingRequirement
from app.repositories import application_repository, capacity_request_repository
from app.security.jwt_service import AuthenticatedEmployee
from app.services import node_placement, placement
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["hosting"])


def _shortlist(requirement, payload) -> list:
    """Top N clusters for ``requirement``, each carrying its top M hosts.

    One helper for all three placement endpoints so they cannot drift: the
    same defaults, the same drill-down, the same "rejections are never
    truncated" contract.
    """
    policy = get_settings().policy
    top_n = payload.top_n if payload.top_n is not None else policy.top_clusters
    candidates = placement.find_and_score_candidates(
        requirement, data_center=payload.data_center, top_n=top_n
    )
    if payload.include_nodes:
        node_placement.attach_top_nodes(
            requirement, candidates,
            top_clusters=top_n,
            top_nodes_per_cluster=payload.top_nodes_per_cluster,
        )
    return candidates


def _tradeoffs(title: str, candidates: list, payload) -> dict | None:
    """Prose comparing the shortlist, when asked for.

    Eligible candidates only: a trade-off summary is about choosing between
    workable options, and a rejected cluster is not one of them. Bounded to
    the shortlist the caller already asked for, so this is exactly one model
    call regardless of how many clusters were scored.
    """
    if not payload.explain:
        return None
    eligible = [c for c in candidates if c.eligibility_status == "Eligible"][: narration.MAX_NARRATED]
    if not eligible:
        return None
    return narration.safely(
        "summarize_tradeoffs",
        lambda: summarize_tradeoffs(get_chat_model_for_role("summarization"), title, eligible),
    )


@router.post("/api/hosting/recommendations")
def hosting_recommendations(payload: HostingRecommendationRequest):
    app = application_repository.get_by_code(payload.application_code)
    if app is None:
        raise ProblemDetailsError(404, "Application not found", f"No application with code {payload.application_code!r}.")
    requirement = placement.requirement_for_application(app)
    candidates = _shortlist(requirement, payload)
    return to_jsonable({
        "application": app, "requirement": requirement, "candidates": candidates,
        "tradeoffs": _tradeoffs(f"Hosting options for {app.ApplicationCode}", candidates, payload),
    })


@router.post("/api/capacity/recommendations")
def capacity_recommendations(payload: CapacityRecommendationRequest, current: AuthenticatedEmployee = Depends(get_current_employee)):
    requested_by = require_matching_employee_id(current, payload.requested_by_employee_id)
    capacity_request_id = capacity_request_repository.create(
        application_id=payload.application_id, requested_by=requested_by,
        environment=payload.environment, required_cpu_cores=payload.cpu_cores,
        required_memory_gb=payload.memory_gb, required_storage_gb=payload.storage_gb,
        expected_growth_percent=payload.expected_growth_percent,
        required_availability_tier=payload.availability_tier, required_platform=payload.platform,
        preferred_location=payload.preferred_location, data_classification=payload.data_classification,
        required_by_date=payload.required_by_date,
    )
    req = capacity_request_repository.get_by_id(capacity_request_id)
    requirement = HostingRequirement.from_capacity_request(req)
    candidates = _shortlist(requirement, payload)
    return to_jsonable({
        "capacity_request": req, "requirement": requirement, "candidates": candidates,
        "tradeoffs": _tradeoffs(f"Capacity request {capacity_request_id}", candidates, payload),
    })


@router.post("/api/hosting/quick-recommendations")
def quick_recommendations(payload: QuickRecommendationRequest):
    """Lightweight "best N clusters for this shape of workload" lookup - no
    CapacityRequest is created, unlike /api/capacity/recommendations. This is
    the fast path for "2 cores, 2 GB, general purpose" style questions.
    """
    requirement = HostingRequirement(
        environment=payload.environment, platform=payload.platform, os_requirement="Any",
        cpu_cores=Decimal(str(payload.cpu_cores)), memory_gb=Decimal(str(payload.memory_gb)),
        storage_gb=Decimal(str(payload.storage_gb)), growth_percent=Decimal("0"),
        availability_tier=payload.availability_tier, data_classification=payload.data_classification,
        criticality="Medium",
    )
    candidates = _shortlist(requirement, payload)
    return to_jsonable({
        "requirement": requirement, "candidates": candidates,
        "tradeoffs": _tradeoffs(
            f"{payload.cpu_cores} cores, {payload.memory_gb} GB memory, {payload.storage_gb} GB storage",
            candidates, payload,
        ),
    })
