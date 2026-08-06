from __future__ import annotations

from decimal import Decimal

from app.models.enums import (
    availability_satisfies,
    classification_permits,
    environment_compatible,
    os_is_compatible,
    platform_compatible,
)
from app.models.requirements import DependencyLocalityCheck, HostingRequirement
from app.repositories import cluster_repository
from app.rules import eligibility
from app.services import placement


def test_availability_satisfies_is_a_total_order():
    assert availability_satisfies("Tier-1", "Tier-1")
    assert availability_satisfies("Tier-1", "Tier-3")  # stronger candidate satisfies weaker requirement
    assert not availability_satisfies("Tier-3", "Tier-1")


def test_classification_permits_requires_at_least_equal_level():
    assert classification_permits("Restricted", "Confidential")
    assert classification_permits("Confidential", "Confidential")
    assert not classification_permits("Internal", "Confidential")


def test_environment_compatible_is_strict_for_nonprod():
    assert environment_compatible("Production", "Production")
    assert not environment_compatible("Production", "Staging")
    assert environment_compatible("Staging", "Staging")
    assert not environment_compatible("Staging", "Test")


def test_platform_compatible_allows_kubernetes_on_openshift_not_reverse():
    assert platform_compatible("Kubernetes", "OpenShift")
    assert platform_compatible("Kubernetes", "Kubernetes")
    assert not platform_compatible("OpenShift", "Kubernetes")


def test_os_is_compatible_matches_family_prefix_only():
    assert os_is_compatible("Linux/RHEL9", "Linux/Ubuntu22")
    assert not os_is_compatible("Windows/2022", "Linux/RHEL9")
    assert os_is_compatible("Any", "Linux/RHEL9")


def test_rule_008_hard_fails_on_cross_region_critical_high_latency_dependency():
    cluster = cluster_repository.get_by_code("nyc-03")  # IN-West
    requirement = HostingRequirement(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=Decimal("2"), memory_gb=Decimal("8"), storage_gb=Decimal("100"),
        growth_percent=Decimal("0"), availability_tier="Tier-3", data_classification="Internal",
        criticality="Medium",
        dependency_checks=[
            DependencyLocalityCheck(
                dependency_id=1, dependency_type="SynchronousApi", is_critical=True,
                latency_sensitivity="High", target_description="cluster atl-03",
                target_region="IN-South", target_data_center="Atlanta-DC1",
            )
        ],
    )
    candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
    rule_008 = next(r for r in candidate.rule_results if r["rule_id"] == "RULE-008")
    assert rule_008["passed"] is False


def test_rule_006_hard_fails_only_for_restricted_location_mismatch():
    cluster = cluster_repository.get_by_code("den-03")  # Atlanta-DC1, Confidential
    restricted_req = HostingRequirement(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=Decimal("1"), memory_gb=Decimal("1"), storage_gb=Decimal("1"),
        growth_percent=Decimal("0"), availability_tier="Tier-3", data_classification="Confidential",
        preferred_location="New York-DC1", criticality="Low",
    )
    ctx, snapshot = placement.build_eligibility_context(restricted_req, cluster)
    result = eligibility.rule_006_location(ctx)
    assert result.passed is True  # soft mismatch for non-Restricted data
