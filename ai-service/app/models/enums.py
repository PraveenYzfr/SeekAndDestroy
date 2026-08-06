"""Controlled vocabularies shared by the database, the rules and the UI.

These values are mirrored by CHECK constraints in ``database/schema.sql``. The
ordering helpers here define the *semantics* used by the hard eligibility rules
(RULE-004 availability, RULE-005 data classification).
"""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    PRODUCTION = "Production"
    STAGING = "Staging"
    TEST = "Test"
    DEVELOPMENT = "Development"


#: Production workloads may only be placed on production infrastructure
#: (RULE-001). Non-production workloads must match exactly.
PRODUCTION_ENVIRONMENTS: frozenset[str] = frozenset({Environment.PRODUCTION})
NON_PRODUCTION_ENVIRONMENTS: frozenset[str] = frozenset(
    {Environment.STAGING, Environment.TEST, Environment.DEVELOPMENT}
)


class AvailabilityTier(StrEnum):
    TIER_1 = "Tier-1"
    TIER_2 = "Tier-2"
    TIER_3 = "Tier-3"


#: Lower rank == stronger guarantee. A candidate satisfies a requirement when
#: ``rank(candidate) <= rank(required)``.
AVAILABILITY_RANK: dict[str, int] = {
    AvailabilityTier.TIER_1: 1,
    AvailabilityTier.TIER_2: 2,
    AvailabilityTier.TIER_3: 3,
}


class DataClassification(StrEnum):
    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"


#: Higher level == more sensitive. A cluster may host data at or below its own
#: compliance classification.
CLASSIFICATION_LEVEL: dict[str, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
}


class BusinessCriticality(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


CRITICALITY_LEVEL: dict[str, int] = {
    BusinessCriticality.CRITICAL: 3,
    BusinessCriticality.HIGH: 2,
    BusinessCriticality.MEDIUM: 1,
    BusinessCriticality.LOW: 0,
}


class LifecycleStatus(StrEnum):
    PLANNED = "Planned"
    ACTIVE = "Active"
    DEPRECATED = "Deprecated"
    DECOMMISSIONING = "Decommissioning"
    RETIRED = "Retired"
    BLOCKED = "Blocked"
    UNSUPPORTED = "Unsupported"


#: RULE-007 - infrastructure in these states is never recommended.
INELIGIBLE_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        LifecycleStatus.RETIRED,
        LifecycleStatus.DECOMMISSIONING,
        LifecycleStatus.BLOCKED,
        LifecycleStatus.UNSUPPORTED,
    }
)

#: Applications in these states are not considered live workloads.
INACTIVE_APPLICATION_STATES: frozenset[str] = frozenset(
    {LifecycleStatus.RETIRED, LifecycleStatus.DECOMMISSIONING}
)


class TechnologyPlatform(StrEnum):
    KUBERNETES = "Kubernetes"
    VMWARE = "VMware"
    OPENSHIFT = "OpenShift"
    BARE_METAL = "BareMetal"
    HYPER_V = "Hyper-V"


class ClusterType(StrEnum):
    KUBERNETES = "Kubernetes"
    VMWARE = "VMware"
    OPENSHIFT = "OpenShift"
    BARE_METAL = "BareMetal"
    HYPER_V = "Hyper-V"


#: Which cluster platforms can host a given application platform requirement
#: (RULE-002). OpenShift is a Kubernetes distribution, so it can host plain
#: Kubernetes workloads; the reverse is not true because OpenShift workloads
#: rely on OpenShift-specific primitives.
PLATFORM_COMPATIBILITY: dict[str, frozenset[str]] = {
    TechnologyPlatform.KUBERNETES: frozenset(
        {ClusterType.KUBERNETES, ClusterType.OPENSHIFT}
    ),
    TechnologyPlatform.OPENSHIFT: frozenset({ClusterType.OPENSHIFT}),
    TechnologyPlatform.VMWARE: frozenset({ClusterType.VMWARE}),
    TechnologyPlatform.HYPER_V: frozenset({ClusterType.HYPER_V}),
    TechnologyPlatform.BARE_METAL: frozenset({ClusterType.BARE_METAL}),
}

#: Platform pairs that are compatible but not the engineer's first choice.
#: Used by the compatibility sub-score, never by the hard rule.
PLATFORM_EXACT_MATCH_BONUS = 100.0
PLATFORM_COMPATIBLE_BONUS = 82.0


class OperatingSystemFamily(StrEnum):
    LINUX = "Linux"
    WINDOWS = "Windows"
    ANY = "Any"


def os_is_compatible(required: str, offered: str) -> bool:
    """RULE-002 operating-system half.

    ``Any`` on either side means the requirement is unconstrained. Otherwise the
    families must match; the version suffix (``Linux/RHEL9``) is advisory.
    """
    req = (required or "").strip()
    off = (offered or "").strip()
    if not req or req == OperatingSystemFamily.ANY:
        return True
    if not off or off == OperatingSystemFamily.ANY:
        return True
    return req.split("/")[0].casefold() == off.split("/")[0].casefold()


class HostingStatus(StrEnum):
    ACTIVE = "Active"
    PLANNED = "Planned"
    MIGRATING = "Migrating"
    RETIRED = "Retired"


ACTIVE_HOSTING_STATES: frozenset[str] = frozenset(
    {HostingStatus.ACTIVE, HostingStatus.MIGRATING}
)


class DependencyType(StrEnum):
    SYNCHRONOUS_API = "SynchronousApi"
    ASYNCHRONOUS_MESSAGING = "AsynchronousMessaging"
    DATABASE = "Database"
    FILE_TRANSFER = "FileTransfer"
    AUTHENTICATION = "Authentication"


class LatencySensitivity(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


LATENCY_SENSITIVITY_LEVEL: dict[str, int] = {
    LatencySensitivity.HIGH: 2,
    LatencySensitivity.MEDIUM: 1,
    LatencySensitivity.LOW: 0,
}


class IncidentSeverity(StrEnum):
    SEV1 = "Sev1"
    SEV2 = "Sev2"
    SEV3 = "Sev3"
    SEV4 = "Sev4"


INCIDENT_SEVERITY_WEIGHT: dict[str, float] = {
    IncidentSeverity.SEV1: 10.0,
    IncidentSeverity.SEV2: 5.0,
    IncidentSeverity.SEV3: 2.0,
    IncidentSeverity.SEV4: 1.0,
}


class IncidentStatus(StrEnum):
    OPEN = "Open"
    IN_PROGRESS = "InProgress"
    RESOLVED = "Resolved"
    CLOSED = "Closed"


class RootCauseCategory(StrEnum):
    CAPACITY = "Capacity"
    CONFIGURATION = "Configuration"
    HARDWARE = "Hardware"
    NETWORK = "Network"
    SOFTWARE = "Software"
    DEPENDENCY = "Dependency"
    UNKNOWN = "Unknown"


class CapacityRequestStatus(StrEnum):
    OPEN = "Open"
    IN_ANALYSIS = "InAnalysis"
    RECOMMENDED = "Recommended"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"


class RecommendationType(StrEnum):
    HOSTING_PLACEMENT = "HostingPlacement"
    NEW_CAPACITY = "NewCapacity"
    CLUSTER_RIGHT_SIZING = "ClusterRightSizing"
    APPLICATION_RIGHT_SIZING = "ApplicationRightSizing"
    CONSOLIDATION = "Consolidation"
    CAPACITY_FORECAST = "CapacityForecast"


class CandidateEntityType(StrEnum):
    CLUSTER = "Cluster"
    NODE = "Node"
    APPLICATION = "Application"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "Eligible"
    REJECTED = "Rejected"
    CONDITIONAL = "Conditional"


class RecommendationStatus(StrEnum):
    PROPOSED = "Proposed"
    PENDING_REVIEW = "PendingReview"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    MORE_ANALYSIS = "MoreAnalysisRequested"
    SUPERSEDED = "Superseded"


class DecisionType(StrEnum):
    APPROVE = "Approve"
    REJECT = "Reject"
    REQUEST_MORE_ANALYSIS = "RequestMoreAnalysis"


class InvestigationType(StrEnum):
    HOSTING = "Hosting"
    CAPACITY = "Capacity"
    RIGHT_SIZING = "RightSizing"
    CONSOLIDATION = "Consolidation"
    FORECAST = "Forecast"
    QUESTION = "Question"
    REFUSED = "Refused"


class InvestigationStatus(StrEnum):
    CREATED = "Created"
    RUNNING = "Running"
    AWAITING_REVIEW = "AwaitingReview"
    COMPLETED = "Completed"
    FAILED = "Failed"


class EntityKind(StrEnum):
    """Vector-store document kinds."""

    APPLICATION = "application"
    CLUSTER = "cluster"
    NODE = "node"
    HOSTING = "hosting"
    INCIDENT = "incident"
    DEPENDENCY = "dependency"
    STANDARD = "standard"
    RECOMMENDATION = "recommendation"


def availability_satisfies(candidate_tier: str, required_tier: str) -> bool:
    """RULE-004: candidate must offer an equal or stronger availability tier."""
    cand = AVAILABILITY_RANK.get(candidate_tier)
    req = AVAILABILITY_RANK.get(required_tier)
    if cand is None or req is None:
        return False
    return cand <= req


def classification_permits(cluster_classification: str, data_classification: str) -> bool:
    """RULE-005: cluster must be certified at or above the data classification."""
    cluster_level = CLASSIFICATION_LEVEL.get(cluster_classification)
    data_level = CLASSIFICATION_LEVEL.get(data_classification)
    if cluster_level is None or data_level is None:
        return False
    return cluster_level >= data_level


def environment_compatible(app_environment: str, cluster_environment: str) -> bool:
    """RULE-001: production stays on production; non-production matches exactly."""
    if app_environment in PRODUCTION_ENVIRONMENTS:
        return cluster_environment in PRODUCTION_ENVIRONMENTS
    return app_environment == cluster_environment


def platform_compatible(required_platform: str, cluster_platform: str) -> bool:
    """RULE-002 platform half."""
    allowed = PLATFORM_COMPATIBILITY.get(required_platform)
    if allowed is None:
        return False
    return cluster_platform in allowed
