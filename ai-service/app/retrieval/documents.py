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


# =============================================================================
# ITSM chunking - a ticket is not a chunk
# =============================================================================
#
# The sections of a ticket answer different questions: what broke, what was
# tried, what fixed it. Embedding them as one document averages the vector over
# all of them, so the single work note that answers a question is diluted by the
# ten that do not. At 10,000 incidents carrying 89,912 work notes that stops
# being a preference and becomes the difference between retrieval working and
# not.
#
# CONTEXTUAL PREFIXES ARE MAINLY FOR THE SPARSE HALF
# ---------------------------------------------------
# Every chunk is prefixed with its identity - record number, severity, cluster,
# application, position in the ticket. A comment on its own is unmoored:
# "Confirmed the ballooning driver is disabled on this host" is useless without
# knowing which host.
#
# But the prefix earns its place for a less obvious reason. INC1000015 and
# atl-p075 are exactly the tokens the BM25 tokeniser preserves whole, and they
# appear nowhere in the body of a work note. Without the prefix the sparse half
# cannot find a comment by its ticket number at all - which is the single most
# common way an engineer searches.
#
# NOISE IS FILTERED, NOT EMBEDDED
# --------------------------------
# Roughly a third of work notes are "Assigned to Network team", "Monitoring",
# "Acknowledged". Embedding those thousands of times poisons the neighbourhood
# around every query: the nearest neighbours of anything become the most common
# boilerplate. They are skipped here and remain in the database, retrievable by
# ticket, just not by similarity.

#: Comments at or below this length matching a routine pattern are not embedded.
_NOISE_MAX_CHARS = 90

_NOISE_PATTERNS = (
    "assigned to", "reassigned to", "acknowledged", "monitoring", "no further updates",
    "awaiting vendor", "bridge call", "updated the stakeholders", "closing -",
)

#: Longest chunk before it is split. Work notes carry stack traces and log
#: excerpts; a 6,000-character note embedded whole is one vector representing a
#: dozen unrelated statements.
_MAX_CHUNK_CHARS = 1000
_CHUNK_OVERLAP = 150


def is_noise(text: str) -> bool:
    """True for routine ticket boilerplate that should not be embedded."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if len(stripped) > _NOISE_MAX_CHARS:
        return False
    lowered = stripped.lower()
    return any(lowered.startswith(p) or lowered == p.rstrip(" -") for p in _NOISE_PATTERNS)


def _split(text: str) -> list[str]:
    """Split over-long text on sentence boundaries, with overlap.

    Overlap rather than clean cuts because the sentence that spans a boundary is
    often the one that matters - a cut mid-explanation leaves both halves
    unable to answer the question the whole would have.
    """
    text = (text or "").strip()
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text] if text else []
    parts, start = [], 0
    while start < len(text):
        end = min(start + _MAX_CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind(". ", start + _MAX_CHUNK_CHARS // 2, end)
            if cut > start:
                end = cut + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - _CHUNK_OVERLAP)
    return [p for p in parts if p]


def _incident_prefix(incident, cluster_code, app_code, position=None) -> str:
    bits = [incident.Number or f"incident:{incident.IncidentId}", incident.Severity]
    if cluster_code:
        bits.append(cluster_code)
    if app_code:
        bits.append(app_code)
    bits.append(incident.OpenedAt.date().isoformat())
    if incident.RootCauseCategory:
        bits.append(incident.RootCauseCategory)
    if position:
        bits.append(position)
    return "[" + " · ".join(str(b) for b in bits if b) + "]"


def incident_chunks(incident, comments, *, cluster_code="", app_code="",
                    subject_description="") -> list[RetrievalDocument]:
    """A ticket as several documents: header, each substantive note, resolution.

    ``comments`` is the ordered list of IncidentComment rows for this incident.
    Passing an empty list yields just the header and resolution, which is what a
    ticket with no work notes should produce.
    """
    docs: list[RetrievalDocument] = []
    prefix = _incident_prefix(incident, cluster_code, app_code)
    meta = dict(
        entity_type=EntityKind.INCIDENT, entity_id=incident.IncidentId,
        source_timestamp=incident.OpenedAt,
    )

    header = " ".join(
        p for p in (
            incident.ShortDescription,
            incident.Description,
            f"Status {incident.Status}." if incident.Status else "",
            f"Assigned to {incident.AssignmentGroup}." if incident.AssignmentGroup else "",
        ) if p
    ).strip()
    if not header:
        # Pre-migration rows have no text at all. Fall back to the generated
        # sentence the old indexer produced rather than skipping the ticket.
        header = (
            f"{incident.Severity} incident on {subject_description or cluster_code}, "
            f"opened {incident.OpenedAt.isoformat()}, status {incident.Status}, "
            f"root cause category {incident.RootCauseCategory}."
        )
    for i, part in enumerate(_split(header)):
        docs.append(RetrievalDocument(
            id=f"incident:{incident.IncidentId}:header:{i}", text=f"{prefix} {part}", **meta))

    total = len(comments)
    for c in comments:
        text = c.get("Text") if isinstance(c, dict) else c.Text
        seq = c.get("Sequence") if isinstance(c, dict) else c.Sequence
        if is_noise(text):
            continue
        note_prefix = _incident_prefix(incident, cluster_code, app_code, f"note {seq}/{total}")
        for i, part in enumerate(_split(text)):
            docs.append(RetrievalDocument(
                id=f"incident:{incident.IncidentId}:note:{seq}:{i}",
                text=f"{note_prefix} {part}", **meta))

    if incident.CloseNotes:
        res_prefix = _incident_prefix(incident, cluster_code, app_code, "resolution")
        for i, part in enumerate(_split(incident.CloseNotes)):
            docs.append(RetrievalDocument(
                id=f"incident:{incident.IncidentId}:resolution:{i}",
                text=f"{res_prefix} {part}", **meta))
    return docs


def change_chunks(change, comments, *, cluster_code="", app_code="") -> list[RetrievalDocument]:
    """A change as header, plan, backout, risk and outcome.

    The plan and the backout are separated deliberately: "what were we going to
    do if this failed" is a different question from "what were we doing", and an
    engineer looking at a failed change is asking the second one.
    """
    docs: list[RetrievalDocument] = []
    bits = [change.Number, change.Type, change.State]
    if cluster_code:
        bits.append(cluster_code)
    if change.CloseCode:
        bits.append(change.CloseCode)
    when = change.PlannedStart or change.ActualStart
    if when:
        bits.append(when.date().isoformat())
    prefix = "[" + " · ".join(str(b) for b in bits if b) + "]"
    meta = dict(entity_type=EntityKind.CHANGE, entity_id=change.ChangeId,
                source_timestamp=change.PlannedStart or change.ActualStart)

    sections = [
        ("header", " ".join(p for p in (change.ShortDescription, change.Description) if p)),
        ("plan", change.ImplementationPlan),
        ("backout", change.BackoutPlan),
        ("risk", change.RiskAssessment),
        ("outcome", change.CloseNotes),
    ]
    for name, body in sections:
        if not body:
            continue
        for i, part in enumerate(_split(body)):
            docs.append(RetrievalDocument(
                id=f"change:{change.ChangeId}:{name}:{i}",
                text=f"{prefix} {part}", **meta))

    total = len(comments)
    for c in comments:
        text = c.get("Text") if isinstance(c, dict) else c.Text
        seq = c.get("Sequence") if isinstance(c, dict) else c.Sequence
        if is_noise(text):
            continue
        for i, part in enumerate(_split(text)):
            docs.append(RetrievalDocument(
                id=f"change:{change.ChangeId}:note:{seq}:{i}",
                text=f"{prefix} note {seq}/{total}: {part}", **meta))
    return docs


def problem_chunks(problem, *, cluster_code="") -> list[RetrievalDocument]:
    """A problem as header, root cause, workaround and fix - separately.

    These are the highest-value sections in the whole corpus. A problem record
    is written to answer "why did this keep happening", which is the question a
    capacity planner is actually asking, and the three sections answer three
    different halves of it.
    """
    docs: list[RetrievalDocument] = []
    bits = [problem.Number, problem.State]
    if cluster_code:
        bits.append(cluster_code)
    if problem.IsKnownError:
        bits.append("known error")
    prefix = "[" + " · ".join(str(b) for b in bits if b) + "]"
    meta = dict(entity_type=EntityKind.PROBLEM, entity_id=problem.ProblemId,
                source_timestamp=problem.OpenedAt)

    for name, body in (
        ("header", " ".join(p for p in (problem.ShortDescription, problem.Description) if p)),
        ("rootcause", problem.RootCause),
        ("workaround", problem.Workaround),
        ("fix", problem.FixNotes),
    ):
        if not body:
            continue
        for i, part in enumerate(_split(body)):
            docs.append(RetrievalDocument(
                id=f"problem:{problem.ProblemId}:{name}:{i}",
                text=f"{prefix} {part}", **meta))
    return docs
