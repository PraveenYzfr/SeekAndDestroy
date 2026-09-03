"""Whitelisted vocabulary for the CMDB Insighter's query layer.

This is the security boundary, not the parameterised binding in
query_builder.py. sqlalchemy.text() with bound parameters stops a malicious
*value* from becoming SQL; it does nothing to stop an LLM from naming a
column that does not exist, or one that exists but was never meant to be
grouped or filtered on. That is this file's job: every table, join and
column the query layer will ever put in a FROM/JOIN/GROUP BY/WHERE clause is
listed here, by literal Python identifier, or it is refused.

Extending what the Insighter can group or filter by means adding an entry to
one of the structures below. It never means relaxing the validation in
query_builder.py to accept something not listed here.

FOUR ENTITIES, NOT ONE BIG JOIN
--------------------------------
"How many incidents" and "how many changes failed" are questions about
different fact tables, not the same table asked two ways - forcing them into
one universal join graph would either produce nonsense cross-joins or need a
graph-traversal planner nobody could audit at a glance. Each entity below
names its own base table and its own whitelist of dimensions; a dimension
reachable only via a join (an application's code, a cluster's data center)
declares which named join it needs, and query_builder only emits a join
clause when a query actually references a dimension that requires it.

    incident  sad.Incident      - what broke
    change    sad.Change        - what was done to the estate
    problem   sad.Problem       - why something keeps recurring (ITSM
                                  shorthand: PRB)
    hosting   sad.ApplicationHosting - which app lives on which cluster/
                                  data center, independent of any incident

Deliberately excluded everywhere: free-text columns (ShortDescription,
Description, CloseNotes, RootCause, Workaround, FixNotes) - those are RAG's
evidence, never a SQL dimension - and MonthlyCost - not part of what this
feature computes at all, let alone renders (see the UI-layer guardrail from
2d: no cost or currency on any screen).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Join:
    #: The literal SQL join clause, e.g.
    #: "LEFT JOIN sad.CmdbApplication app ON i.ApplicationId = app.ApplicationId".
    #: Never derived from user input - every value here is hand-written.
    clause: str


@dataclass(frozen=True)
class Dimension:
    #: Qualified column reference against the entity's aliased tables, e.g.
    #: "i.Severity" or "app.Environment". Never derived from user input.
    column: str
    #: Reader-facing label, used in narrative evidence and refusal messages.
    #: "root cause category" reads to someone who has never seen the schema;
    #: "RootCauseCategory" does not.
    label: str
    #: Key into the owning EntityConfig.joins, if reaching this column
    #: requires one. None means the column lives on the base table itself.
    join: str | None = None
    #: "string" (default) or "bool". IsPrimary/IsKnownError are SQL Server
    #: BIT columns - filter values for them are converted from "true"/"false"/
    #: "1"/"0" to an actual bit rather than bound as a string, which a BIT
    #: column will not reliably compare against.
    dtype: str = "string"


@dataclass(frozen=True)
class EntityConfig:
    #: Base table name, passed to app.repositories.base.T().
    table: str
    #: Alias the base table is given in the generated SQL.
    alias: str
    dimensions: dict[str, Dimension]
    joins: dict[str, Join] = field(default_factory=dict)
    #: Qualified datetime column opened_after/opened_before filter against,
    #: or None if this entity does not support a date-range filter.
    date_column: str | None = None


#: migration_008_configuration_items.sql gave Incident/Change/Problem a
#: BusinessServiceCiId column, but the generator does not populate it -
#: checked directly, 0 of 10,000 incidents carry one. The real path from an
#: incident to its business service is the relationship graph: incident ->
#: its CI -> a Depends on::Used by edge (TypeId 4) -> the business service.
#:
#: Confirmed empirically, not assumed, which way round that edge runs:
#: cmdb_ci_service is the PARENT, the application is the CHILD (a service
#: "depends on::used by" several applications - several children per parent).
#: So reaching a CI's business service means matching it as the CHILD and
#: reading the PARENT, the opposite of what the column name suggests.
#:
#: SOME CIs GENUINELY MAP TO MORE THAN ONE SERVICE, GROUPING WILL FAN OUT
#: An application can be the child of more than one cmdb_ci_service parent -
#: real in this data, not a defect. Grouping incidents by business_service
#: therefore counts one incident once per service it maps to: an incident on
#: an app tied to two services appears in both services' counts. That means
#: the sum across every business_service group can exceed the ungrouped
#: total_count - a real property of the data, not a bug, and it must be
#: stated in any narrative built on this dimension rather than silently
#: producing a total that does not reconcile.
#:
#: 821 of 1,200 applications today have NO business-service edge at all -
#: their incidents group under NULL, never dropped (GUARDRAILS: NULLs are
#: data).
def _ci_joins(alias: str) -> dict[str, Join]:
    return {
        "ci": Join(f"LEFT JOIN sad.ConfigurationItem ci ON {alias}.CmdbCiId = ci.CiId"),
        # TypeId 4 (Depends on::Used by) is reused for BOTH "app depends on a
        # business service" and plain application-to-application dependency -
        # there is no separate edge type for the two. Filtering
        # bs.ClassName='cmdb_ci_service' only on the SECOND join looked
        # correct and was not: a bsrel row pointing at another application's
        # ParentCiId still survives the LEFT JOIN as a row with bs.Name NULL,
        # sitting alongside the row(s) that do resolve to a real service for
        # the same incident - inflating the NULL group with incidents that
        # HAVE a service, just also have an unrelated app-to-app dependency.
        # Caught empirically (APP-PAYMENTS: a real service link plus a
        # non-service Depends-on edge, both TypeId 4, produced three rows for
        # the same incident - one correct, two spurious NULLs) rather than
        # assumed correct from the shape of the join. The EXISTS guard keeps
        # only type-4 edges whose parent actually is a service, so an
        # application with no service edge at all still LEFT JOINs to a
        # single true NULL row, and one with an unrelated app dependency
        # never contributes an extra one.
        "business_service": Join(
            f"LEFT JOIN sad.CiRelationship bsrel ON bsrel.ChildCiId = {alias}.CmdbCiId AND bsrel.TypeId = 4 "
            f"AND EXISTS (SELECT 1 FROM sad.ConfigurationItem svc WHERE svc.CiId = bsrel.ParentCiId AND svc.ClassName = 'cmdb_ci_service') "
            f"LEFT JOIN sad.ConfigurationItem bs ON bs.CiId = bsrel.ParentCiId "
            f"LEFT JOIN sad.CiBusinessService bsvc ON bsvc.CiId = bs.CiId"
        ),
    }


_CI_DIMENSIONS = {
    "ci_class": Dimension("ci.ClassName", "CI class", join="ci"),
    "business_service": Dimension("bs.Name", "business service", join="business_service"),
    "business_service_criticality": Dimension("bsvc.Criticality", "business service criticality", join="business_service"),
}

INCIDENT_ENTITY = EntityConfig(
    table="Incident", alias="i",
    date_column="i.OpenedAt",
    joins={
        "application": Join("LEFT JOIN " "sad.CmdbApplication app ON i.ApplicationId = app.ApplicationId"),
        "cluster": Join("LEFT JOIN sad.InfrastructureCluster cl ON i.ClusterId = cl.ClusterId"),
        **_ci_joins("i"),
    },
    dimensions={
        "severity": Dimension("i.Severity", "severity"),
        "root_cause_category": Dimension("i.RootCauseCategory", "root cause category"),
        "status": Dimension("i.Status", "status"),
        "assignment_group": Dimension("i.AssignmentGroup", "assignment group"),
        "impact": Dimension("i.Impact", "impact"),
        "urgency": Dimension("i.Urgency", "urgency"),
        "application_code": Dimension("app.ApplicationCode", "application", join="application"),
        "cluster_code": Dimension("cl.ClusterCode", "cluster", join="cluster"),
        "data_center": Dimension("cl.DataCenter", "data center", join="cluster"),
        "region": Dimension("cl.Region", "region", join="cluster"),
        **_CI_DIMENSIONS,
    },
)

CHANGE_ENTITY = EntityConfig(
    table="Change", alias="c",
    # Planned/authorised changes have no ActualEnd yet, so a date-range filter
    # on this entity is implicitly "changes that have actually run" - worth
    # stating as a caveat in any narrative that uses one.
    date_column="c.ActualEnd",
    joins={
        "application": Join("LEFT JOIN sad.CmdbApplication app ON c.ApplicationId = app.ApplicationId"),
        "cluster": Join("LEFT JOIN sad.InfrastructureCluster cl ON c.ClusterId = cl.ClusterId"),
        **_ci_joins("c"),
    },
    dimensions={
        "type": Dimension("c.Type", "change type"),
        "state": Dimension("c.State", "state"),
        "close_code": Dimension("c.CloseCode", "close code"),
        "assignment_group": Dimension("c.AssignmentGroup", "assignment group"),
        "application_code": Dimension("app.ApplicationCode", "application", join="application"),
        "cluster_code": Dimension("cl.ClusterCode", "cluster", join="cluster"),
        "data_center": Dimension("cl.DataCenter", "data center", join="cluster"),
        **_CI_DIMENSIONS,
    },
)

PROBLEM_ENTITY = EntityConfig(
    table="Problem", alias="p",
    date_column="p.OpenedAt",
    joins={
        "application": Join("LEFT JOIN sad.CmdbApplication app ON p.ApplicationId = app.ApplicationId"),
        "cluster": Join("LEFT JOIN sad.InfrastructureCluster cl ON p.ClusterId = cl.ClusterId"),
        **_ci_joins("p"),
    },
    dimensions={
        "state": Dimension("p.State", "state"),
        "is_known_error": Dimension("p.IsKnownError", "known-error status", dtype="bool"),
        "application_code": Dimension("app.ApplicationCode", "application", join="application"),
        "cluster_code": Dimension("cl.ClusterCode", "cluster", join="cluster"),
        "data_center": Dimension("cl.DataCenter", "data center", join="cluster"),
        **_CI_DIMENSIONS,
    },
)

HOSTING_ENTITY = EntityConfig(
    table="ApplicationHosting", alias="h",
    date_column="h.HostedSince",
    joins={
        "application": Join("LEFT JOIN sad.CmdbApplication app ON h.ApplicationId = app.ApplicationId"),
        "cluster": Join("LEFT JOIN sad.InfrastructureCluster cl ON h.ClusterId = cl.ClusterId"),
    },
    dimensions={
        "application_code": Dimension("app.ApplicationCode", "application", join="application"),
        "cluster_code": Dimension("cl.ClusterCode", "cluster", join="cluster"),
        "data_center": Dimension("cl.DataCenter", "data center", join="cluster"),
        "region": Dimension("cl.Region", "region", join="cluster"),
        "environment": Dimension("h.Environment", "environment"),
        "hosting_status": Dimension("h.HostingStatus", "hosting status"),
        "is_primary": Dimension("h.IsPrimary", "primary-hosting status", dtype="bool"),
    },
)


#: THE ESTATE ITSELF, not something that happened to it.
#:
#: The other four entities are event or relationship tables - what broke, what
#: was done, what recurs, what lives where. None of them can answer "how many
#: servers do we have", and that question reached the chat, fell through to
#: retrieval, and came back "I have no record of how many servers are in the
#: database" while sad.ConfigurationItem held 10,943 rows of ClassName
#: 'cmdb_ci_server'. Retrieval cannot count - it returns the top-k chunks most
#: similar to a question - so the honest-sounding refusal was produced by a
#: model that had genuinely been handed nothing, and the platform reported the
#: absence of data it owns.
#:
#: ConfigurationItem is the right base table rather than CiServer or
#: ClusterNode: it is the one place every class is countable on the same
#: footing, so "how many servers", "how many VMs" and "how many CIs by class"
#: are one query shape with a filter, not three special cases. The per-class
#: detail tables carry different columns and would each need their own entity
#: to answer the same question.
#:
#: NO free-text or identity columns - Name and SysId identify a specific box
#: and are not dimensions anyone groups by. Deliberately absent for the same
#: reason ShortDescription is absent from Incident.
CI_ENTITY = EntityConfig(
    table="ConfigurationItem", alias="ci",
    #: FirstDiscovered, not LastDiscovered. "How many servers did we add last
    #: month" is a question about when a CI entered the estate; LastDiscovered
    #: moves every time discovery runs and would make a date filter mean
    #: "scanned recently", which is a staleness question wearing the same
    #: words. Staleness has its own report in app.insights.cmdb_health.
    date_column="ci.FirstDiscovered",
    joins={
        "support_group": Join(
            "LEFT JOIN sad.SupportGroup sg ON ci.SupportGroupId = sg.SupportGroupId"
        ),
    },
    dimensions={
        #: The classes are ServiceNow's sys_class_name values, CHECK-constrained
        #: in migration_008. 'server' and 'vm' as spoken by a person are mapped
        #: to these by CI_CLASS_SYNONYMS below - the model is never asked to
        #: invent a class string.
        "ci_class": Dimension("ci.ClassName", "CI class"),
        "environment": Dimension("ci.Environment", "environment"),
        "operational_status": Dimension("ci.OperationalStatus", "operational status"),
        "install_status": Dimension("ci.InstallStatus", "install status"),
        "data_classification": Dimension("ci.DataClassification", "data classification"),
        "regulatory_scope": Dimension("ci.RegulatoryScope", "regulatory scope"),
        "discovery_source": Dimension("ci.DiscoverySource", "discovery source"),
        "support_group": Dimension("sg.GroupName", "support group", join="support_group"),
    },
)

#: What a person says -> the sys_class_name they mean. Applied to FILTER VALUES
#: on ci_class, so "how many servers" becomes ClassName = 'cmdb_ci_server'
#: rather than a model guessing at a string it has never been shown.
#:
#: "server" does NOT include vm_instance. A person asking how many servers we
#: have and a person asking how many VMs are asking different questions, and
#: silently folding 30,105 VM instances into a server count would be a wrong
#: answer delivered confidently. If someone wants both they can ask for both,
#: and the narrator states which class it counted.
CI_CLASS_SYNONYMS: dict[str, str] = {
    "server": "cmdb_ci_server", "servers": "cmdb_ci_server",
    "host": "cmdb_ci_server", "hosts": "cmdb_ci_server",
    "vm": "cmdb_ci_vm_instance", "vms": "cmdb_ci_vm_instance",
    "virtual machine": "cmdb_ci_vm_instance", "virtual machines": "cmdb_ci_vm_instance",
    "application": "cmdb_ci_appl", "applications": "cmdb_ci_appl", "app": "cmdb_ci_appl",
    "apps": "cmdb_ci_appl",
    "cluster": "cmdb_ci_cluster", "clusters": "cmdb_ci_cluster",
    "node": "cmdb_ci_cluster_node", "nodes": "cmdb_ci_cluster_node",
    "database": "cmdb_ci_db_instance", "databases": "cmdb_ci_db_instance",
    "db": "cmdb_ci_db_instance", "dbs": "cmdb_ci_db_instance",
    "load balancer": "cmdb_ci_lb", "load balancers": "cmdb_ci_lb", "lb": "cmdb_ci_lb",
    "data center": "cmdb_ci_datacenter", "data centre": "cmdb_ci_datacenter",
    "data centers": "cmdb_ci_datacenter", "data centres": "cmdb_ci_datacenter",
    "zone": "cmdb_ci_zone", "zones": "cmdb_ci_zone",
    "service": "cmdb_ci_service", "services": "cmdb_ci_service",
    "storage volume": "cmdb_ci_storage_volume", "storage volumes": "cmdb_ci_storage_volume",
    "storage array": "cmdb_ci_storage_array", "storage arrays": "cmdb_ci_storage_array",
    "network device": "cmdb_ci_netgear", "network devices": "cmdb_ci_netgear",
}


#: "incident"/"change"/"problem" are this schema's own words; "PRB" is the
#: ITSM shorthand for a Problem record, same idea as the severity synonyms
#: below - a question that says PRB should not silently come back empty
#: because the model repeated ITSM jargon this schema does not use for its
#: entity names either.
ENTITIES: dict[str, EntityConfig] = {
    "incident": INCIDENT_ENTITY,
    "change": CHANGE_ENTITY,
    "problem": PROBLEM_ENTITY,
    "hosting": HOSTING_ENTITY,
    "ci": CI_ENTITY,
}

ENTITY_SYNONYMS: dict[str, str] = {
    #: The estate entity answers "how many X do we have". People say "CI",
    #: "configuration item", "asset" and "inventory" for the same table, and
    #: they say "server" for a row in it - the last is an entity hint AND a
    #: class filter, which normalize_ci_class resolves separately.
    "ci": "ci", "cis": "ci", "configuration item": "ci",
    "configuration items": "ci", "asset": "ci", "assets": "ci",
    "inventory": "ci", "estate": "ci",
    "prb": "problem", "problems": "problem",
    "incidents": "incident", "inc": "incident",
    "changes": "change", "chg": "change",
    "hostings": "hosting", "placement": "hosting", "placements": "hosting",
}

#: The only aggregation this layer computes today. Adding "avg_mttr_hours" or
#: similar (mission capability 6) is future work - it needs OpenedAt/ClosedAt
#: arithmetic and an explicit exclude-still-open rule, not just a new string
#: in this set.
MEASURES: frozenset[str] = frozenset({"count"})

#: ITSM shorthand this estate's schema does not speak - severities here are
#: Sev1..Sev4 (app.models.enums.IncidentSeverity), not P1..P4. Mapped
#: explicitly, one literal key at a time, rather than pattern-matched: two
#: regexes elsewhere in this codebase described an imagined corpus instead of
#: this one and silently returned zero rows for weeks. A question this maps
#: gets an answer; a question it does not map falls through to validation and
#: is refused with the real vocabulary, never silently zeroed out.
SEVERITY_SYNONYMS: dict[str, str] = {
    "p1": "Sev1", "priority1": "Sev1", "priority 1": "Sev1", "sev1": "Sev1", "sev 1": "Sev1",
    "severity1": "Sev1", "severity 1": "Sev1",
    "p2": "Sev2", "priority2": "Sev2", "priority 2": "Sev2", "sev2": "Sev2", "sev 2": "Sev2",
    "severity2": "Sev2", "severity 2": "Sev2",
    "p3": "Sev3", "priority3": "Sev3", "priority 3": "Sev3", "sev3": "Sev3", "sev 3": "Sev3",
    "severity3": "Sev3", "severity 3": "Sev3",
    "p4": "Sev4", "priority4": "Sev4", "priority 4": "Sev4", "sev4": "Sev4", "sev 4": "Sev4",
    "severity4": "Sev4", "severity 4": "Sev4",
}

#: Bit-column filter values a model or caller might reasonably write for a
#: "bool" dtype Dimension. Anything else is left unmapped and will fail SQL
#: Server's implicit conversion loudly rather than being guessed at.
BOOL_SYNONYMS: dict[str, int] = {
    "true": 1, "1": 1, "yes": 1, "known": 1,
    "false": 0, "0": 0, "no": 0, "unknown": 0,
}


def normalize_severity(raw: str) -> str:
    """Map ITSM shorthand ("P1") onto this schema's actual values ("Sev1").

    A value not found in the synonym table is returned unchanged and left for
    validate_spec to accept (if it happens to already be a real Severity
    value) or refuse (naming what IS valid) - this function only ever adds a
    mapping, never a silent pass-through default.
    """
    return SEVERITY_SYNONYMS.get(raw.strip().lower(), raw)


def normalize_entity(raw: str) -> str:
    """Map ITSM shorthand ("PRB") onto this layer's entity keys ("problem")."""
    return ENTITY_SYNONYMS.get(raw.strip().lower(), raw)


def normalize_bool(raw: str) -> int | None:
    """A bit-column filter value -> 0/1, or None if it maps to neither."""
    return BOOL_SYNONYMS.get(raw.strip().lower())


def valid_entities() -> list[str]:
    return sorted(ENTITIES)


def valid_dimensions(entity: str) -> list[str]:
    config = ENTITIES.get(entity)
    return sorted(config.dimensions) if config else []


def valid_measures() -> list[str]:
    return sorted(MEASURES)


#: How a class is written back to the reader. The reader's own noun is what
#: gets matched, but echoing it verbatim produces "30,105 vms." - correct and
#: sloppy. A count is often pasted into a change record or a mail, so the
#: sentence has to survive being read by someone who did not ask the question.
CI_CLASS_DISPLAY: dict[str, str] = {
    "cmdb_ci_server": "servers",
    "cmdb_ci_vm_instance": "VMs",
    "cmdb_ci_cluster_node": "cluster nodes",
    "cmdb_ci_db_instance": "database instances",
    "cmdb_ci_appl": "applications",
    "cmdb_ci_cluster": "clusters",
    "cmdb_ci_lb": "load balancers",
    "cmdb_ci_datacenter": "data centres",
    "cmdb_ci_zone": "zones",
    "cmdb_ci_service": "business services",
    "cmdb_ci_storage_volume": "storage volumes",
    "cmdb_ci_storage_array": "storage arrays",
    "cmdb_ci_netgear": "network devices",
}


def normalize_ci_class(value: str) -> str:
    """Map a spoken class name onto a real sys_class_name, or pass it through.

    "servers" -> "cmdb_ci_server". A value already in ServiceNow form, or one
    with no mapping at all, is returned unchanged and left for validate_spec
    to accept or refuse - never silently dropped, which would turn a filter
    the reader asked for into a count of the whole estate.
    """
    return CI_CLASS_SYNONYMS.get(str(value).strip().casefold(), value)


def dimension_labels(entity: str) -> list[str]:
    """Reader-facing labels for an entity's dimensions, for use in a refusal.

    Labels, never column names or keys. The Dimension dataclass carries a
    label precisely so a message can say "data classification" rather than
    "DataClassification", and this is the function that keeps that promise -
    a refusal that names columns teaches the reader the schema, which is the
    disclosure shape removed from capability_reply on 2026-09-04.

    Saying what CAN be grouped is not the same as enumerating what the estate
    CONTAINS. The first is the vocabulary of the question; the second is
    inventory, and it stays out of user-facing text.
    """
    config = ENTITIES.get(entity)
    return sorted(d.label for d in config.dimensions.values()) if config else []
