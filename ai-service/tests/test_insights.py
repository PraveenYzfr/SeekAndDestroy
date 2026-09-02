"""Tests for the CMDB Insighter query layer (app.insights).

Runs against the live seeded database, like the rest of this suite (see
conftest.py) - and asserts against SQL computed independently right here in
the test, never against a figure copied from a prior run. The corpus is
regenerated periodically (see scripts/generate_seed.py); a hardcoded expected
count would encode one snapshot as if it were a fact about the schema.
"""

from __future__ import annotations

import pytest

from app.agents.guards import NumberDriftError
from app.agents.mock_llm import MockChatModel
from app.evaluation.graders import number_fidelity
from app.insights import narrator, query_builder
from app.insights.query_builder import InsightValidationError, build_query, run_query, validate_spec
from app.insights.whitelist import normalize_severity
from app.models.insights import InsightNarrative, InsightQuerySpec
from app.repositories.base import T, fetch_all


# =============================================================================
# Whitelist validation - GUARDRAILS: unknown dimension = refuse and list valid ones
# =============================================================================
def test_validate_spec_rejects_unknown_dimension():
    spec = InsightQuerySpec(group_by=["cost_center"])
    with pytest.raises(InsightValidationError, match="cost_center"):
        validate_spec(spec)


def test_validate_spec_rejects_unknown_filter():
    spec = InsightQuerySpec(filters={"monthly_cost": ["1000"]})
    with pytest.raises(InsightValidationError, match="monthly_cost"):
        validate_spec(spec)


def test_validate_spec_rejects_unknown_measure():
    spec = InsightQuerySpec(measure="sum_cost")
    with pytest.raises(InsightValidationError, match="sum_cost"):
        validate_spec(spec)


def test_validate_spec_accepts_whitelisted_fields():
    spec = InsightQuerySpec(group_by=["severity", "root_cause_category"], filters={"status": ["Closed"]})
    validate_spec(spec)  # must not raise


def test_validate_spec_rejects_unknown_entity():
    spec = InsightQuerySpec(entity="server")
    with pytest.raises(InsightValidationError, match="server"):
        validate_spec(spec)


def test_validate_spec_rejects_dimension_from_a_different_entity():
    # close_code is a Change dimension - asking for it on the incident entity
    # must be refused, not silently ignored or answered against the wrong table.
    spec = InsightQuerySpec(entity="incident", group_by=["close_code"])
    with pytest.raises(InsightValidationError, match="close_code"):
        validate_spec(spec)


# =============================================================================
# Severity synonym mapping - "P1" is ITSM shorthand this schema does not use
# =============================================================================
@pytest.mark.parametrize("raw,expected", [
    ("p1", "Sev1"), ("P1", "Sev1"), ("priority 1", "Sev1"), ("Priority1", "Sev1"),
    ("sev1", "Sev1"), ("Sev1", "Sev1"),
    ("p4", "Sev4"),
])
def test_normalize_severity_maps_itsm_shorthand(raw, expected):
    assert normalize_severity(raw) == expected


def test_normalize_severity_passes_through_unmapped_value():
    # Not a known synonym and not a real Severity value either - left for
    # validate_spec/SQL to refuse or return zero rows, never silently altered.
    assert normalize_severity("Sev99") == "Sev99"


def test_run_query_normalizes_p1_filter_before_hitting_sql():
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["P1"]})
    result = run_query(spec)
    # The result states what was actually queried (Sev1), not the raw ITSM
    # shorthand the caller passed in - "P1" is not a real value in this
    # schema, so echoing it back in the stated filters would be the same
    # kind of dishonesty GUARDRAILS warns against for a bare number.
    assert result["filters"]["severity"] == ["Sev1"]

    expected_total = fetch_all(
        f"SELECT COUNT(*) AS N FROM {T('Incident')} WHERE Severity = :sev", {"sev": "Sev1"},
    )[0]["N"]
    assert result["total_count"] == expected_total


# =============================================================================
# SQL is parameterised, never string-built from values
# =============================================================================
def test_build_query_binds_filter_values_as_parameters():
    spec = InsightQuerySpec(group_by=["severity"], filters={"status": ["Closed", "Resolved"]})
    sql, params, group_by = build_query(spec)
    assert "Closed" not in sql and "Resolved" not in sql
    assert set(params.values()) == {"Closed", "Resolved"}
    assert group_by == ["severity"]


def test_build_query_ungrouped_is_a_plain_count():
    spec = InsightQuerySpec()
    sql, params, group_by = build_query(spec)
    assert group_by == []
    assert "GROUP BY" not in sql
    assert params == {}


# =============================================================================
# THE ACCEPTANCE CASE: "how many P1s and categorise them with root causes"
# =============================================================================
def test_acceptance_case_sev1_by_root_cause_matches_independent_sql():
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["P1"]})
    result = run_query(spec)

    # Independent SQL, written fresh here rather than reusing query_builder's
    # own query - the point is catching a bug IN query_builder, which a
    # self-comparison could never do.
    expected_rows = fetch_all(
        f"SELECT RootCauseCategory, COUNT(*) AS N FROM {T('Incident')} "
        f"WHERE Severity = :sev GROUP BY RootCauseCategory",
        {"sev": "Sev1"},
    )
    expected_total = sum(r["N"] for r in expected_rows)
    expected_by_category = {r["RootCauseCategory"]: r["N"] for r in expected_rows}

    assert result["total_count"] == expected_total
    actual_by_category = {r["RootCauseCategory"]: r["IncidentCount"] for r in result["rows"]}
    assert actual_by_category == expected_by_category

    # GUARDRAILS: NULLs are data - Unknown must appear if it exists in the
    # corpus, not be silently dropped. Assert on presence relative to the
    # independent query, never a hardcoded count - the corpus is regenerated
    # and any fixed number here would encode a snapshot as truth.
    if "Unknown" in expected_by_category:
        assert "Unknown" in actual_by_category
        assert actual_by_category["Unknown"] == expected_by_category["Unknown"]


def test_acceptance_case_row_count_and_filters_are_stated():
    """GUARDRAILS: every response states its filters and row count - never a
    bare number. This checks the *evidence* the narrator would be handed
    carries what it needs to say that, independent of any LLM call."""
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["Sev1"]})
    result = run_query(spec)
    assert result["filters"] == {"severity": ["Sev1"]}
    assert result["distinct_groups"] == len(result["rows"])
    assert result["total_count"] >= 0


# =============================================================================
# Empty result is a valid answer, not an error
# =============================================================================
def test_empty_result_is_returned_not_raised():
    # 'Cancelled' is not a value CK_Incident_Status permits, so this can never
    # match a row - a deterministic way to produce zero matches without
    # depending on which combinations happen to be absent from this seed run.
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"status": ["Cancelled"]})
    result = run_query(spec)
    assert result["rows"] == []
    assert result["total_count"] == 0
    assert result["distinct_groups"] == 0


# =============================================================================
# The 500-row default cap would break this layer at corpus scale (2d's
# warning) - confirm run_query passes its own explicit cap, not the default.
# =============================================================================
def test_run_query_passes_explicit_max_rows(monkeypatch):
    captured = {}
    real_fetch_all = query_builder.fetch_all

    def spy(sql, params=None, *, max_rows=None):
        captured["max_rows"] = max_rows
        return real_fetch_all(sql, params, max_rows=max_rows)

    monkeypatch.setattr(query_builder, "fetch_all", spy)
    run_query(InsightQuerySpec(group_by=["severity"]))
    assert captured["max_rows"] == query_builder.MAX_INSIGHT_ROWS


# =============================================================================
# Narration: bounded to the SQL result, checked against it
# =============================================================================
def test_narrate_end_to_end_with_mock_provider():
    """No API key needed - SAD_LLM__PROVIDER=mock is this platform's
    zero-config default, and MockChatModel echoes total_count straight out of
    the evidence JSON it is given (see app.agents.mock_llm)."""
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["Sev1"]})
    result = run_query(spec)
    narrative = narrator.narrate(MockChatModel(), "How many Sev1 incidents and what are the root causes?", result)
    assert narrative.total_count == result["total_count"]


def test_narrate_rejects_tampered_total_count(monkeypatch):
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["Sev1"]})
    result = run_query(spec)

    tampered = InsightNarrative(
        headline="753 Sev1 incidents.", narrative="...", insight="", caveats=[],
        total_count=result["total_count"] + 1,
    )
    monkeypatch.setattr(narrator, "run_structured", lambda *a, **k: tampered)
    with pytest.raises(NumberDriftError):
        narrator.narrate(MockChatModel(), "How many Sev1 incidents?", result)


def test_narrate_rejects_ungrounded_number_in_prose(monkeypatch):
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["Sev1"]})
    result = run_query(spec)

    # total_count is correct (passes assert_no_number_drift) but the prose
    # invents a figure that appears nowhere in the evidence - the failure
    # mode number_fidelity exists to catch that a structured-field check
    # alone would miss.
    fabricated = InsightNarrative(
        headline=f"{result['total_count']} Sev1 incidents.",
        narrative="Of these, 91827 were traced to a single misconfigured load balancer.",
        insight="", caveats=[], total_count=result["total_count"],
    )
    monkeypatch.setattr(narrator, "run_structured", lambda *a, **k: fabricated)
    with pytest.raises(narrator.InsightNarrationError):
        narrator.narrate(MockChatModel(), "How many Sev1 incidents?", result)


# =============================================================================
# Multi-entity expansion: change, problem, hosting - and joins to
# application/cluster/data-center, applied only when referenced
# =============================================================================
def test_prb_entity_synonym_maps_to_problem():
    spec = InsightQuerySpec(entity="PRB", group_by=["state"])
    result = run_query(spec)
    assert result["entity"] == "problem"


def test_build_query_does_not_join_when_no_dimension_needs_it():
    sql, _, _ = build_query(InsightQuerySpec(entity="incident", group_by=["severity"]))
    assert "JOIN" not in sql


def test_build_query_joins_only_the_tables_a_referenced_dimension_needs():
    sql, _, _ = build_query(InsightQuerySpec(entity="incident", group_by=["data_center"]))
    assert "InfrastructureCluster" in sql
    assert "CmdbApplication" not in sql  # application_code was never referenced


def test_change_entity_close_code_matches_independent_sql():
    spec = InsightQuerySpec(entity="change", group_by=["close_code"])
    result = run_query(spec)

    expected_rows = fetch_all(f"SELECT CloseCode, COUNT(*) AS N FROM {T('Change')} GROUP BY CloseCode")
    expected_by_code = {r["CloseCode"]: r["N"] for r in expected_rows}
    actual_by_code = {r["CloseCode"]: r["IncidentCount"] for r in result["rows"]}

    assert actual_by_code == expected_by_code
    assert result["total_count"] == sum(expected_by_code.values())


def test_problem_entity_bool_filter_matches_independent_sql():
    spec = InsightQuerySpec(entity="problem", filters={"is_known_error": ["true"]})
    result = run_query(spec)

    expected_total = fetch_all(f"SELECT COUNT(*) AS N FROM {T('Problem')} WHERE IsKnownError = 1")[0]["N"]
    assert result["total_count"] == expected_total


def test_problem_entity_bool_filter_rejects_unmappable_value():
    spec = InsightQuerySpec(entity="problem", filters={"is_known_error": ["banana"]})
    with pytest.raises(InsightValidationError, match="banana"):
        build_query(spec)


def test_incident_joined_to_cluster_for_data_center_breakdown():
    """Praveen's ask: 'which app lives in which data center', applied to
    incidents - a join this layer did not support before this expansion."""
    spec = InsightQuerySpec(entity="incident", group_by=["data_center"], filters={"severity": ["Sev1"]})
    result = run_query(spec)

    expected_rows = fetch_all(
        f"SELECT cl.DataCenter AS DataCenter, COUNT(*) AS N FROM {T('Incident')} i "
        f"LEFT JOIN {T('InfrastructureCluster')} cl ON i.ClusterId = cl.ClusterId "
        f"WHERE i.Severity = :sev GROUP BY cl.DataCenter",
        {"sev": "Sev1"},
    )
    expected_by_dc = {r["DataCenter"]: r["N"] for r in expected_rows}
    actual_by_dc = {r["DataCenter"]: r["IncidentCount"] for r in result["rows"]}
    assert actual_by_dc == expected_by_dc


# =============================================================================
# Business service rollup - e7's "enterprise CMDB tool" ask: an executive
# question ("which business services took Sev1s"), not just an infra one
# =============================================================================
#: A genuinely independent re-derivation of "which service does this CI map
#: to", used by the tests below instead of copying whitelist.py's join. Two
#: differently-shaped queries agreeing is real evidence; the same shape
#: checked against itself is not - this exact gap let a bug through once
#: already (see whitelist.py's business_service join docstring): a first
#: version filtered "parent is a service" only in the second join of a
#: LEFT JOIN chain, and this file's own "independent" SQL copied the same
#: shape, so a query that fanned out on unrelated app-to-app Depends-on
#: edges matched an equally-wrong expectation and the test passed anyway.
#: Pre-filtering to valid app->service edges in a CTE, then joining
#: incidents onto that, cannot share that specific mistake.
_INDEPENDENT_APP_SERVICE_CTE = (
    "WITH app_service AS ("
    "  SELECT r.ChildCiId AS AppCiId, svc.CiId AS ServiceCiId, svc.Name AS ServiceName, sv.Criticality "
    "  FROM sad.CiRelationship r "
    "  JOIN sad.ConfigurationItem svc ON svc.CiId = r.ParentCiId AND svc.ClassName = 'cmdb_ci_service' "
    "  JOIN sad.CiBusinessService sv ON sv.CiId = svc.CiId "
    "  WHERE r.TypeId = 4"
    ") "
)


def test_incident_by_business_service_matches_independent_sql():
    """Confirms the join direction: cmdb_ci_service is the PARENT of the
    Depends-on edge, the application is the CHILD - documented in
    whitelist.py as confirmed empirically, checked again here independently.

    Also the test that caught a real bug: this used to assert `None in
    actual`, expecting applications with no business-service edge to show a
    NULL group. That was true when only 379 of 1,200 applications had one;
    once e7's generator mapped all 1,200, running this again surfaced 259
    incidents in a NULL group that should not have existed - not missing
    coverage, but a join bug (see whitelist.py) letting unrelated
    application-to-application Depends-on edges leak in as spurious NULL
    rows alongside an incident's real service match. Fixed there; this test
    now asserts the NULL group is gone, which is the current, correct,
    complete-coverage state - not a permanent assumption.
    """
    spec = InsightQuerySpec(entity="incident", group_by=["business_service"])
    result = run_query(spec)

    expected_rows = fetch_all(
        _INDEPENDENT_APP_SERVICE_CTE
        + f"SELECT aps.ServiceName, COUNT(*) AS N FROM {T('Incident')} i "
        f"LEFT JOIN app_service aps ON aps.AppCiId = i.CmdbCiId "
        f"GROUP BY aps.ServiceName"
    )
    expected = {r["ServiceName"]: r["N"] for r in expected_rows}
    actual = {r["Name"]: r["IncidentCount"] for r in result["rows"]}
    assert actual == expected
    # Service coverage is complete as of this seed (e7's migration 015) -
    # every application maps to a service, so every incident does too.
    assert None not in actual


def test_business_service_fanout_can_exceed_total_incident_count():
    """Documents the real, non-bug property: an application mapped to more
    than one business service makes an incident count once per service, so
    the sum across every business_service group can exceed the number of
    underlying incidents. Compared against a genuine ungrouped COUNT(*) on
    Incident - NOT against run_query's own total_count, which is itself
    defined as the sum of the returned groups and would make this
    comparison tautological for a fan-out dimension. That distinction
    matters: total_count is an honest "sum of what you asked to see", not a
    claim about how many distinct incidents exist, and the two coincide for
    every other dimension in this whitelist precisely because none of them
    fan out the way business_service does.
    """
    spec = InsightQuerySpec(entity="incident", group_by=["business_service"])
    result = run_query(spec)
    grouped_sum = sum(r["IncidentCount"] for r in result["rows"])
    true_incident_count = fetch_all(f"SELECT COUNT(*) AS N FROM {T('Incident')}")[0]["N"]
    assert grouped_sum >= true_incident_count


def test_business_service_criticality_dimension_matches_independent_sql():
    spec = InsightQuerySpec(entity="incident", group_by=["business_service_criticality"], filters={"severity": ["Sev1"]})
    result = run_query(spec)

    expected_rows = fetch_all(
        _INDEPENDENT_APP_SERVICE_CTE
        + f"SELECT aps.Criticality, COUNT(*) AS N FROM {T('Incident')} i "
        f"LEFT JOIN app_service aps ON aps.AppCiId = i.CmdbCiId "
        f"WHERE i.Severity = :sev GROUP BY aps.Criticality",
        {"sev": "Sev1"},
    )
    expected = {r["Criticality"]: r["N"] for r in expected_rows}
    actual = {r["Criticality"]: r["IncidentCount"] for r in result["rows"]}
    assert actual == expected


def test_hosting_entity_which_app_lives_where():
    spec = InsightQuerySpec(entity="hosting", group_by=["application_code", "data_center"])
    result = run_query(spec)

    expected_rows = fetch_all(
        f"SELECT app.ApplicationCode AS ApplicationCode, cl.DataCenter AS DataCenter, COUNT(*) AS N "
        f"FROM {T('ApplicationHosting')} h "
        f"LEFT JOIN {T('CmdbApplication')} app ON h.ApplicationId = app.ApplicationId "
        f"LEFT JOIN {T('InfrastructureCluster')} cl ON h.ClusterId = cl.ClusterId "
        f"GROUP BY app.ApplicationCode, cl.DataCenter",
        max_rows=query_builder.MAX_INSIGHT_ROWS,
    )
    expected = {(r["ApplicationCode"], r["DataCenter"]): r["N"] for r in expected_rows}
    actual = {(r["ApplicationCode"], r["DataCenter"]): r["IncidentCount"] for r in result["rows"]}
    assert actual == expected


def test_number_fidelity_accepts_rows_actually_in_evidence():
    """Sanity check on the evidence shape itself: a narrative that only cites
    counts/percentages present in _evidence_for must show zero ungrounded
    figures - guards against the fidelity check above being trivially strict
    (rejecting everything) rather than precise."""
    spec = InsightQuerySpec(group_by=["root_cause_category"], filters={"severity": ["Sev1"]})
    result = run_query(spec)
    evidence = narrator._evidence_for(result)
    top_row = max(evidence["rows"], key=lambda r: r["count"]) if evidence["rows"] else None

    prose = f"Total: {evidence['total_count']}."
    if top_row is not None:
        prose += f" The largest group had {top_row['count']} incidents ({top_row['percent_of_total']}%)."

    fidelity = number_fidelity(prose, evidence)
    assert fidelity.ungrounded == []
