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
}

ENTITY_SYNONYMS: dict[str, str] = {
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
