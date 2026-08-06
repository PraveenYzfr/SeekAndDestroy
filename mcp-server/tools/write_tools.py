"""Controlled write tools - the only tools in this server that mutate data,
and every one of them writes to governance/tracking tables only
(CapacityRequest, Investigation, InfrastructureRecommendation,
RecommendationDecision). None of them can touch CmdbApplication,
InfrastructureCluster, ClusterNode or ApplicationHosting - there is no
provisioning, decommissioning or migration tool in this server, by design.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from tools._audit import audited, model_dict
from tools._auth import authenticate

from app.repositories import (
    application_repository,
    capacity_request_repository,
    investigation_repository,
    recommendation_repository,
)
from app.utils.json_utils import to_jsonable


def create_capacity_request(
    requested_by_employee_id: int, access_token: str, environment: str, required_cpu_cores: float,
    required_memory_gb: float, required_storage_gb: float, required_availability_tier: str, required_platform: str,
    data_classification: str, application_code: str = "", expected_growth_percent: float = 0.0,
    preferred_location: str = "", required_by_date: Optional[str] = None,
) -> dict:
    """Create a Scenario-B raw capacity request (new infrastructure space need).
    access_token must be a valid JWT (see app.security.jwt_service) identifying
    the requester - it is authoritative over requested_by_employee_id."""
    params = dict(locals())
    params.pop("access_token", None)  # never persist a raw token to the audit log

    def run():
        requested_by, error = authenticate(access_token, requested_by_employee_id)
        if error:
            return error
        app_id = None
        if application_code:
            app = application_repository.get_by_code(application_code)
            if app is None:
                return {"error": f"application {application_code} not found"}
            app_id = app.ApplicationId
        parsed_date = date.fromisoformat(required_by_date) if required_by_date else None
        new_id = capacity_request_repository.create(
            application_id=app_id, requested_by=requested_by, environment=environment,
            required_cpu_cores=required_cpu_cores, required_memory_gb=required_memory_gb,
            required_storage_gb=required_storage_gb, expected_growth_percent=expected_growth_percent,
            required_availability_tier=required_availability_tier, required_platform=required_platform,
            preferred_location=preferred_location or None, data_classification=data_classification,
            required_by_date=parsed_date,
        )
        return model_dict(capacity_request_repository.get_by_id(new_id))

    return audited("create_capacity_request", params, run)


def create_investigation(query: str, investigation_type: str, created_by_employee_id: int, access_token: str) -> dict:
    """Create a new Investigation row. investigation_type must be one of Hosting, Capacity,
    RightSizing, Consolidation, Forecast, Question, Refused. access_token must be a valid
    JWT (see app.security.jwt_service) - it is authoritative over created_by_employee_id."""
    params = dict(locals())
    params.pop("access_token", None)  # never persist a raw token to the audit log

    def run():
        created_by, error = authenticate(access_token, created_by_employee_id)
        if error:
            return error
        new_id = investigation_repository.create(query, investigation_type, created_by)
        return model_dict(investigation_repository.get_by_id(new_id))

    return audited("create_investigation", params, run)


def get_investigation(investigation_id: int) -> dict:
    """Fetch an investigation and its recommendations."""
    params = dict(locals())

    def run():
        inv = investigation_repository.get_by_id(investigation_id)
        if inv is None:
            return {"error": f"investigation {investigation_id} not found"}
        recs = recommendation_repository.list_for_investigation(investigation_id)
        return {"investigation": to_jsonable(inv), "recommendations": [to_jsonable(r) for r in recs]}

    return audited("get_investigation", params, run)


def save_recommendation(
    investigation_id: int, recommendation_type: str, candidate_entity_type: str, candidate_entity_id: int,
    rank: int, eligibility_status: str, explanation: str, evidence_json: str,
    capacity_request_id: Optional[int] = None, application_id: Optional[int] = None,
    overall_score: Optional[float] = None, capacity_score: Optional[float] = None,
    compatibility_score: Optional[float] = None, cost_score: Optional[float] = None,
    resiliency_score: Optional[float] = None, dependency_score: Optional[float] = None,
    risk_score: Optional[float] = None, projected_cpu_utilization: Optional[float] = None,
    projected_memory_utilization: Optional[float] = None, projected_storage_utilization: Optional[float] = None,
    projected_headroom_percent: Optional[float] = None, estimated_monthly_cost: Optional[float] = None,
) -> dict:
    """Persist one already-computed recommendation row. All scores/costs must come from the
    scoring/capacity engines - this tool only records them, it never computes them."""
    params = dict(locals())

    def run():
        rec = {
            "InvestigationId": investigation_id, "CapacityRequestId": capacity_request_id,
            "ApplicationId": application_id, "RecommendationType": recommendation_type,
            "CandidateEntityType": candidate_entity_type, "CandidateEntityId": candidate_entity_id,
            "Rank": rank, "EligibilityStatus": eligibility_status, "OverallScore": overall_score,
            "CapacityScore": capacity_score, "CompatibilityScore": compatibility_score,
            "CostScore": cost_score, "ResiliencyScore": resiliency_score, "DependencyScore": dependency_score,
            "RiskScore": risk_score, "ProjectedCpuUtilization": projected_cpu_utilization,
            "ProjectedMemoryUtilization": projected_memory_utilization,
            "ProjectedStorageUtilization": projected_storage_utilization,
            "ProjectedHeadroomPercent": projected_headroom_percent, "EstimatedMonthlyCost": estimated_monthly_cost,
            "Explanation": explanation, "EvidenceJson": evidence_json, "Status": "Proposed",
        }
        new_id = recommendation_repository.save(rec)
        return model_dict(recommendation_repository.get_by_id(new_id))

    return audited("save_recommendation", params, run)


def list_recommendations(status: str = "", limit: int = 100) -> list[dict]:
    """List recent recommendations, optionally filtered by status."""
    params = dict(locals())
    return audited(
        "list_recommendations", params,
        lambda: [to_jsonable(r) for r in recommendation_repository.list_recent(status=status or None, limit=min(limit, 500))],
    )


def submit_recommendation_decision(
    recommendation_id: int, decision: str, reviewer_employee_id: int, access_token: str, reason: str = ""
) -> dict:
    """Record a human decision (Approve | Reject | RequestMoreAnalysis) on a recommendation.
    access_token must be a valid JWT (see app.security.jwt_service) identifying the reviewer -
    it is authoritative over reviewer_employee_id, and this tool refuses anonymous decisions."""
    params = dict(locals())
    params.pop("access_token", None)  # never persist a raw token to the audit log

    def run():
        reviewer_id, error = authenticate(access_token, reviewer_employee_id)
        if error:
            return error
        rec = recommendation_repository.get_by_id(recommendation_id)
        if rec is None:
            return {"error": f"recommendation {recommendation_id} not found"}
        if decision not in ("Approve", "Reject", "RequestMoreAnalysis"):
            return {"error": f"decision must be Approve, Reject or RequestMoreAnalysis; got {decision!r}"}
        status_map = {"Approve": "Approved", "Reject": "Rejected", "RequestMoreAnalysis": "MoreAnalysisRequested"}
        decision_id = recommendation_repository.save_decision(
            recommendation_id=recommendation_id, decision=decision, decision_reason=reason or None,
            decided_by=reviewer_id,
        )
        recommendation_repository.update_status(recommendation_id, status_map[decision])
        return {"decision_id": decision_id, "recommendation": model_dict(recommendation_repository.get_by_id(recommendation_id))}

    return audited("submit_recommendation_decision", params, run)
