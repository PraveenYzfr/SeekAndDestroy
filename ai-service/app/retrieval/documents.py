"""Builds the natural-language documents indexed for RAG.

The cluster document format follows the worked example in the specification
almost verbatim; every other entity kind follows the same "plain sentences
describing the record" pattern so retrieval quality is consistent across
kinds.
"""

from __future__ import annotations

from app.models.entities import (
    ApplicationDependency,
    ApplicationHosting,
    ClusterNode,
    CmdbApplication,
    Incident,
    InfrastructureCluster,
)
from app.models.enums import EntityKind
from app.models.retrieval import RetrievalDocument


def application_document(app: CmdbApplication) -> RetrievalDocument:
    text = (
        f"Application {app.ApplicationCode} ({app.ApplicationName}) is a {app.BusinessCriticality}-criticality "
        f"{app.Environment} workload running on {app.TechnologyPlatform} ({app.OperatingSystemRequirement}). "
        f"It requires {app.CpuRequirement} CPU cores, {app.MemoryRequirementGb} GB memory and "
        f"{app.StorageRequirementGb} GB storage, with expected annual growth of "
        f"{app.ExpectedAnnualGrowthPercent}%. It needs {app.AvailabilityTier} availability and handles "
        f"{app.DataClassification} data"
        + (f", preferably located in {app.PreferredLocation}" if app.PreferredLocation else "")
        + f". Lifecycle status: {app.LifecycleStatus}."
    )
    return RetrievalDocument(
        id=f"application:{app.ApplicationId}", text=text, entity_type=EntityKind.APPLICATION,
        entity_id=app.ApplicationId, environment=app.Environment, platform=app.TechnologyPlatform,
        location=app.PreferredLocation, lifecycle_status=app.LifecycleStatus,
        availability_tier=app.AvailabilityTier, compliance_classification=app.DataClassification,
        source_timestamp=app.UpdatedAt,
    )


def cluster_document(
    cluster: InfrastructureCluster, *, current_cpu_percent: float | None = None,
    current_memory_percent: float | None = None,
) -> RetrievalDocument:
    text = (
        f"Cluster {cluster.ClusterCode} is a {cluster.Environment.lower()} {cluster.ClusterType} cluster "
        f"located in {cluster.DataCenter} ({cluster.Region}). "
        f"It has {cluster.TotalCpuCores} CPU cores, {cluster.TotalMemoryGb} GB memory and "
        f"{cluster.TotalStorageGb} GB storage. "
    )
    if current_cpu_percent is not None and current_memory_percent is not None:
        text += (
            f"Current average CPU utilization is {current_cpu_percent}%. "
            f"Current memory utilization is {current_memory_percent}%. "
        )
    text += (
        f"The cluster supports {cluster.AvailabilityTier} availability and "
        f"{cluster.ComplianceClassification} data. Lifecycle status: {cluster.LifecycleStatus}."
    )
    return RetrievalDocument(
        id=f"cluster:{cluster.ClusterId}", text=text, entity_type=EntityKind.CLUSTER,
        entity_id=cluster.ClusterId, environment=cluster.Environment, platform=cluster.Platform,
        location=cluster.DataCenter, lifecycle_status=cluster.LifecycleStatus,
        availability_tier=cluster.AvailabilityTier, compliance_classification=cluster.ComplianceClassification,
        source_timestamp=cluster.UpdatedAt,
    )


# Cost is deliberately absent from every document below. MonthlyCost stays in
# the database, but anything written here is embedded into the vector store
# and handed back to the model as evidence - which is how narration ended up
# ranking clusters by "highest monthly cost" when cost is not the question
# this platform answers. Capacity, utilization and eligibility are.
def node_document(node: ClusterNode, cluster_code: str) -> RetrievalDocument:
    text = (
        f"Node {node.HostName} ({node.IpAddress}) belongs to cluster {cluster_code}. "
        f"It has {node.CpuCores} CPU cores, {node.MemoryGb} GB memory and {node.StorageGb} GB storage. "
        f"Lifecycle status: {node.LifecycleStatus}."
    )
    return RetrievalDocument(
        id=f"node:{node.NodeId}", text=text, entity_type=EntityKind.NODE, entity_id=node.NodeId,
        lifecycle_status=node.LifecycleStatus, source_timestamp=node.UpdatedAt,
    )


def hosting_document(hosting: ApplicationHosting, app_code: str, cluster_code: str) -> RetrievalDocument:
    text = (
        f"Application {app_code} is hosted on cluster {cluster_code} with status {hosting.HostingStatus} "
        f"({'primary' if hosting.IsPrimary else 'secondary'} hosting). "
        f"Allocated: {hosting.AllocatedCpuCores} CPU cores, {hosting.AllocatedMemoryGb} GB memory, "
        f"{hosting.AllocatedStorageGb} GB storage. Hosted since {hosting.HostedSince.date().isoformat()}."
    )
    return RetrievalDocument(
        id=f"hosting:{hosting.HostingId}", text=text, entity_type=EntityKind.HOSTING,
        entity_id=hosting.HostingId, environment=hosting.Environment, source_timestamp=hosting.UpdatedAt,
    )


def incident_document(incident: Incident, subject_description: str) -> RetrievalDocument:
    text = (
        f"{incident.Severity} incident on {subject_description}, opened {incident.OpenedAt.isoformat()}, "
        f"status {incident.Status}, root cause category {incident.RootCauseCategory}."
    )
    if incident.ClosedAt:
        text += f" Closed {incident.ClosedAt.isoformat()}."
    return RetrievalDocument(
        id=f"incident:{incident.IncidentId}", text=text, entity_type=EntityKind.INCIDENT,
        entity_id=incident.IncidentId, source_timestamp=incident.OpenedAt,
    )


def dependency_document(dep: ApplicationDependency, source_code: str, target_description: str) -> RetrievalDocument:
    text = (
        f"Application {source_code} has a {dep.DependencyType} dependency on {target_description} "
        f"with {dep.LatencySensitivity} latency sensitivity. "
        f"{'This is a critical dependency.' if dep.IsCritical else 'This is a non-critical dependency.'}"
    )
    return RetrievalDocument(
        id=f"dependency:{dep.DependencyId}", text=text, entity_type=EntityKind.DEPENDENCY, entity_id=dep.DependencyId,
    )


def standard_document(doc_id: str, title: str, text: str) -> RetrievalDocument:
    return RetrievalDocument(id=f"standard:{doc_id}", text=f"{title}. {text}", entity_type=EntityKind.STANDARD, entity_id=0)


def recommendation_document(recommendation_id: int, text: str, *, environment: str | None = None) -> RetrievalDocument:
    return RetrievalDocument(
        id=f"recommendation:{recommendation_id}", text=text, entity_type=EntityKind.RECOMMENDATION,
        entity_id=recommendation_id, environment=environment,
    )
