from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends

from app.api.auth import get_current_employee, require_matching_employee_id
from app.api.errors import ProblemDetailsError
from app.api.schemas import CapacityRecommendationRequest, HostingRecommendationRequest, QuickRecommendationRequest
from app.models.requirements import HostingRequirement
from app.repositories import application_repository, capacity_request_repository
from app.security.jwt_service import AuthenticatedEmployee
from app.services import placement
from app.utils.json_utils import to_jsonable

router = APIRouter(tags=["hosting"])


@router.post("/api/hosting/recommendations")
def hosting_recommendations(payload: HostingRecommendationRequest):
    app = application_repository.get_by_code(payload.application_code)
    if app is None:
        raise ProblemDetailsError(404, "Application not found", f"No application with code {payload.application_code!r}.")
    requirement = placement.requirement_for_application(app)
    candidates = placement.find_and_score_candidates(requirement, data_center=payload.data_center, top_n=payload.top_n)
    return to_jsonable(
        {"application": app, "requirement": requirement, "candidates": candidates}
    )


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
    candidates = placement.find_and_score_candidates(requirement, data_center=payload.data_center, top_n=payload.top_n)
    return to_jsonable(
        {"capacity_request": req, "requirement": requirement, "candidates": candidates}
    )


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
    candidates = placement.find_and_score_candidates(requirement, data_center=payload.data_center, top_n=payload.top_n)
    return to_jsonable({"requirement": requirement, "candidates": candidates})
