"""Turns a validated InsightQuerySpec into parameterised SQL and runs it.

This is the module app.insights exists around. An LLM never writes SQL here
and never sees a raw column or table name accepted uncontested - every
identifier that reaches the query text below first passed through
app.insights.whitelist (an EntityConfig's dimensions and joins), so the only
strings that can be interpolated into the SQL are the ones a human wrote into
that file. Every *value* (a severity, a date, a data center name) is still
bound as a parameter through app.repositories.base.fetch_all's
sqlalchemy.text(), exactly like every other query in this codebase (see that
module's docstring) - the whitelist is the security boundary, the binding is
the safety net under it, not the other way round.

JOINS ARE APPLIED ONLY WHEN REFERENCED
---------------------------------------
"How many Sev1 incidents" never touches sad.CmdbApplication or
sad.InfrastructureCluster - those joins are only emitted when a query's
group_by or filters actually reference a dimension that needs one
(Dimension.join in whitelist.py). A query that never asks about an
application or a data center never pays for, or risks a row-count change
from, a join it did not ask for.
"""

from __future__ import annotations

from datetime import datetime

from app.insights.whitelist import (
    ENTITIES,
    MEASURES,
    normalize_bool,
    normalize_ci_class,
    normalize_entity,
    normalize_severity,
    valid_dimensions,
    valid_entities,
    valid_measures,
)
from app.models.insights import InsightQuerySpec
from app.repositories.base import T, fetch_all


class InsightValidationError(ValueError):
    """A query spec named an entity, measure, dimension or filter this layer
    does not recognise, or a malformed date bound.

    Carries the real vocabulary in its message (see GUARDRAILS: "Unknown
    dimension = refuse and list valid ones") - refusing silently or coercing
    to the nearest known value would hide the fact that the question the
    model heard was not the one it answered.
    """


#: A GROUP BY over these whitelisted, enum-like columns returns at most a few
#: hundred distinct combinations even joined across every entity here -
#: nowhere near settings.service.max_rows=500's usual working set, but
#: bounded explicitly rather than left to that endpoint-wide default, so this
#: layer's cap is a decision this module owns rather than an accident of some
#: other caller's needs (see app.repositories.base.RowLimitExceeded).
MAX_INSIGHT_ROWS = 2_000


def normalize_spec(spec: InsightQuerySpec) -> InsightQuerySpec:
    """Apply the whitelist's explicit synonym mappings before validation.

    "P1" is not a value this schema has ever used - Severity is Sev1..Sev4
    (app.models.enums.IncidentSeverity) - and "PRB" is not an entity name -
    it is "problem" (app.models.entities.Problem). Both are mapped here, one
    literal key at a time, rather than pattern-matched. A value with no
    mapping is passed through unchanged and left for validate_spec to accept
    or refuse, never silently dropped.
    """
    entity = normalize_entity(spec.entity)
    updates: dict[str, object] = {"entity": entity}
    filters = spec.filters
    if "severity" in filters:
        filters = {**filters, "severity": [normalize_severity(v) for v in filters["severity"]]}
    # "how many servers" arrives as ci_class=['servers'] because that is the
    # word the reader used. ClassName holds ServiceNow's sys_class_name, so an
    # unmapped value matches nothing and the honest-looking answer is zero -
    # the same shape of failure as answering "no record" over a populated
    # table. Mapped here, one literal key at a time, like severity above.
    if "ci_class" in filters:
        filters = {**filters, "ci_class": [normalize_ci_class(v) for v in filters["ci_class"]]}
    if filters is not spec.filters:
        updates["filters"] = filters
    return spec.model_copy(update=updates)


def validate_spec(spec: InsightQuerySpec) -> None:
    if spec.entity not in ENTITIES:
        raise InsightValidationError(f"Unknown entity {spec.entity!r}. Valid entities: {valid_entities()}")
    config = ENTITIES[spec.entity]

    if spec.measure not in MEASURES:
        raise InsightValidationError(f"Unknown measure {spec.measure!r}. Valid measures: {valid_measures()}")

    for dim in spec.group_by:
        if dim not in config.dimensions:
            raise InsightValidationError(
                f"Unknown dimension {dim!r} for entity {spec.entity!r}. "
                f"Valid dimensions: {valid_dimensions(spec.entity)}"
            )

    for key, values in spec.filters.items():
        if key not in config.dimensions:
            raise InsightValidationError(
                f"Unknown filter {key!r} for entity {spec.entity!r}. "
                f"Valid filters: {valid_dimensions(spec.entity)}"
            )
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise InsightValidationError(f"Filter {key!r} must be a list of strings, got {values!r}")

    for bound_name, raw in (("opened_after", spec.opened_after), ("opened_before", spec.opened_before)):
        if raw is None:
            continue
        if config.date_column is None:
            raise InsightValidationError(f"Entity {spec.entity!r} has no date column to filter {bound_name} against.")
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise InsightValidationError(f"{bound_name} must be an ISO date (YYYY-MM-DD), got {raw!r}") from exc


def _bare_name(qualified_column: str) -> str:
    """"cl.DataCenter" -> "DataCenter" - the alias every result row and every
    narrator lookup uses, independent of which entity's alias produced it."""
    return qualified_column.rsplit(".", 1)[-1]


def build_query(spec: InsightQuerySpec) -> tuple[str, dict, list[str]]:
    """A validated spec -> (sql text, bound params, ordered group-by keys).

    Filter parameter names are generated (f{filter_index}_{value_index})
    rather than derived from the dimension key, the same pattern
    itsm_repository.incident_comments_for uses for its IN-list - it sidesteps
    ever needing a dimension key to also be a legal SQL parameter identifier.
    """
    validate_spec(spec)
    config = ENTITIES[spec.entity]

    needed_joins: set[str] = set()
    for dim_key in (*spec.group_by, *spec.filters):
        join_key = config.dimensions[dim_key].join
        if join_key:
            needed_joins.add(join_key)
    join_sql = " ".join(config.joins[key].clause for key in sorted(needed_joins))

    params: dict[str, object] = {}
    where_clauses: list[str] = []

    for filter_index, (key, values) in enumerate(spec.filters.items()):
        if not values:
            continue
        dimension = config.dimensions[key]
        placeholders = []
        for value_index, raw_value in enumerate(values):
            param_name = f"f{filter_index}_{value_index}"
            if dimension.dtype == "bool":
                bit = normalize_bool(raw_value)
                if bit is None:
                    raise InsightValidationError(
                        f"Filter {key!r} expects a true/false value, got {raw_value!r}"
                    )
                params[param_name] = bit
            else:
                params[param_name] = raw_value
            placeholders.append(f":{param_name}")
        where_clauses.append(f"{dimension.column} IN ({', '.join(placeholders)})")

    if spec.opened_after:
        where_clauses.append(f"{config.date_column} >= :opened_after")
        params["opened_after"] = spec.opened_after
    if spec.opened_before:
        where_clauses.append(f"{config.date_column} < :opened_before")
        params["opened_before"] = spec.opened_before

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    from_sql = f"{T(config.table)} {config.alias} {join_sql}".strip()

    if spec.group_by:
        group_columns = [config.dimensions[key].column for key in spec.group_by]
        select_sql = ", ".join(f"{col} AS {_bare_name(col)}" for col in group_columns)
        group_sql = ", ".join(group_columns)
        sql = (
            f"SELECT {select_sql}, COUNT(*) AS IncidentCount FROM {from_sql} {where_sql} "
            f"GROUP BY {group_sql} ORDER BY IncidentCount DESC"
        )
    else:
        sql = f"SELECT COUNT(*) AS IncidentCount FROM {from_sql} {where_sql}"

    return sql.strip(), params, list(spec.group_by)


def run_query(spec: InsightQuerySpec) -> dict:
    """Executes a spec and returns rows plus the totals a narrator needs to
    state its filters and row count honestly (see GUARDRAILS: never a bare
    number).

    An empty ``rows`` list (nothing matched the filters) is returned as such,
    not raised as an error - GUARDRAILS: "Empty result is a valid answer, not
    an error."
    """
    spec = normalize_spec(spec)
    sql, params, group_by = build_query(spec)
    rows = fetch_all(sql, params, max_rows=MAX_INSIGHT_ROWS)

    total_count = sum(int(row["IncidentCount"]) for row in rows)
    return {
        "entity": spec.entity,
        "group_by": group_by,
        "filters": spec.filters,
        "opened_after": spec.opened_after,
        "opened_before": spec.opened_before,
        "rows": rows,
        "total_count": total_count,
        "distinct_groups": len(rows),
    }
