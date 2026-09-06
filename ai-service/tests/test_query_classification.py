"""Routing a free-text request, and surviving what a real model returns.

Both halves here come from one real failure. "find a 32 core for hosting java
app" classified as a general Question, went to grounded Q&A, and answered "I
don't have enough grounded information" - true, and useless. Fixing the
routing then exposed the worse problem underneath: Gemini's extraction
returned platform='Java' and availability_tier='Standard', which flowed
straight into the hard eligibility rules and rejected all 133 clusters with
reasons that read like nonsense.
"""

from __future__ import annotations

import pytest

from app.graph.nodes import (
    _capacity_requirement_from_regex,
    _coerce_enum,
    classify_investigation_type,
)
from app.models.enums import (
    AvailabilityTier,
    DataClassification,
    Environment,
    InvestigationType,
    TechnologyPlatform,
)

# =============================================================================
# Classification
# =============================================================================


@pytest.mark.parametrize(
    "query",
    [
        "find a 32 core for hosting java app",     # the original failure
        "I need 32 CPU cores for a Java application",
        "need a 16 vCPU 64GB box",
        "looking for 12 cores",
        "get me 2TB of storage",
        "I need 8 CPU, 32 GB RAM and 500 GB storage for a production Kubernetes workload.",
    ],
)
def test_a_quantity_asked_for_in_provisioning_terms_is_a_capacity_request(query):
    assert classify_investigation_type(query) == InvestigationType.CAPACITY


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Find the best clusters for hosting APP-CRM", InvestigationType.HOSTING),
        ("Why was nyc-03 rejected for APP-CRM?", InvestigationType.QUESTION),
        ("Which clusters are underutilized?", InvestigationType.RIGHT_SIZING),
        ("forecast capacity for nyc-03", InvestigationType.FORECAST),
        ("consolidate workloads in production", InvestigationType.CONSOLIDATION),
        ("decommission nyc-03", InvestigationType.REFUSED),
    ],
)
def test_existing_routes_are_unchanged(query, expected):
    assert classify_investigation_type(query) == expected


def test_refusal_still_wins_over_a_capacity_shaped_request():
    """"provision 32 cores" is a request to *act*, and this platform never
    executes infrastructure changes - that has to outrank the capacity match.
    """
    assert classify_investigation_type("provision 32 cores for me") == InvestigationType.REFUSED


def test_a_bare_quantity_without_a_provisioning_verb_stays_a_question():
    """"which clusters have 32 cores free" is a question about what exists,
    not a request for new space.
    """
    assert classify_investigation_type("which clusters have 32 cores free") == InvestigationType.QUESTION


# =============================================================================
# Regex extraction
# =============================================================================


def test_cores_and_vcpu_are_recognised_not_just_cpu():
    """The regression: the pattern was `N cpu` only, so "32 core" matched
    nothing and silently became the 8-core default - asked for 32, sized for 8.
    """
    assert _capacity_requirement_from_regex("find a 32 core box")["capacity_requirements"]["cpu_cores"] == 32.0
    assert _capacity_requirement_from_regex("need 16 vCPU")["capacity_requirements"]["cpu_cores"] == 16.0
    assert _capacity_requirement_from_regex("need 4 cpus")["capacity_requirements"]["cpu_cores"] == 4.0


def test_a_bare_gb_figure_is_read_as_memory():
    assert _capacity_requirement_from_regex("16 vCPU 64GB box")["capacity_requirements"]["memory_gb"] == 64.0


def test_storage_in_tb_is_converted_and_not_mistaken_for_memory():
    result = _capacity_requirement_from_regex("a 2TB box with 12 cores")["capacity_requirements"]
    assert result["storage_gb"] == 2000.0
    assert result["memory_gb"] == 32.0  # defaulted, not taken from the TB figure


def test_gb_storage_is_storage_not_memory():
    result = _capacity_requirement_from_regex("8 cpu 32 gb ram 500 gb storage")["capacity_requirements"]
    assert (result["cpu_cores"], result["memory_gb"], result["storage_gb"]) == (8.0, 32.0, 500.0)
    assert result["assumed_defaults"] == []


def test_assumed_dimensions_are_named_not_hidden():
    """A reviewer approving 32 GB of memory must be able to see that nobody
    asked for 32 GB - the platform supplied it.
    """
    result = _capacity_requirement_from_regex("find a 32 core box")["capacity_requirements"]
    assert set(result["assumed_defaults"]) == {"memory_gb", "storage_gb"}
    assert "cpu_cores" not in result["assumed_defaults"]


# =============================================================================
# Enum coercion - the trust boundary applied to categories
# =============================================================================


def test_a_valid_value_passes_through_and_reports_no_coercion():
    assert _coerce_enum("Production", Environment, Environment.STAGING) == ("Production", True)


def test_case_differences_are_accepted_rather_than_rejected():
    """Real Gemini output was environment='production'. Treating that as
    invalid produced *"Production/production workloads may not be placed on
    'Production' infrastructure"* - a rule failing on letter case.
    """
    assert _coerce_enum("production", Environment, Environment.STAGING) == ("Production", True)
    assert _coerce_enum("  TIER-1 ", AvailabilityTier, AvailabilityTier.TIER_2) == ("Tier-1", True)


@pytest.mark.parametrize(
    ("value", "enum_cls", "fallback"),
    [
        ("Java", TechnologyPlatform, TechnologyPlatform.KUBERNETES),          # a language
        ("Standard", AvailabilityTier, AvailabilityTier.TIER_2),              # not a tier
        ("Top Secret", DataClassification, DataClassification.INTERNAL),      # not a classification
        ("", Environment, Environment.PRODUCTION),
        (None, Environment, Environment.PRODUCTION),
    ],
)
def test_an_invented_value_falls_back_and_is_flagged(value, enum_cls, fallback):
    coerced, was_valid = _coerce_enum(value, enum_cls, fallback)
    assert coerced == str(fallback)
    assert was_valid is False


def test_every_fallback_is_itself_a_real_enum_member():
    """A fallback that is not a valid member would fail the same rules it is
    meant to rescue.
    """
    for enum_cls, fallback in (
        (Environment, Environment.PRODUCTION),
        (TechnologyPlatform, TechnologyPlatform.KUBERNETES),
        (AvailabilityTier, AvailabilityTier.TIER_2),
        (DataClassification, DataClassification.INTERNAL),
    ):
        assert str(fallback) in {str(m) for m in enum_cls}
