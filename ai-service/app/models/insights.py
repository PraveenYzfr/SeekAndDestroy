"""Pydantic contracts for the CMDB Insighter (see app.insights).

Two shapes the LLM is ever allowed to produce for this feature:

``InsightQuerySpec`` constrains *intent*, not trust - every field is checked
against app.insights.whitelist by app.insights.query_builder before it
touches SQL, exactly like every other LLM-facing contract in this codebase is
verified rather than taken on faith (see app.models.agent_contracts).

``InsightNarrative`` constrains the *output*: prose bounded to a SQL result
the model was handed, never a number it computed. ``total_count`` is the one
field app.agents.guards.assert_no_number_drift can check structurally; every
other figure in the free-text fields is checked by
app.evaluation.graders.number_fidelity at narration time (see
app.insights.narrator) - belt and suspenders for the one property this
feature exists to guarantee.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class InsightQuerySpec(BaseModel):
    """What the LLM maps a natural-language analytics question onto.

    Every string field is validated against app.insights.whitelist - a value
    the model invents (a column that does not exist, a measure this layer
    does not compute) is refused with the real vocabulary listed, never
    silently coerced or dropped.
    """

    entity: str = Field(
        "incident",
        description="One of app.insights.whitelist.ENTITIES - which fact table the question is about: "
        "'incident' (what broke), 'change' (what was done to the estate), 'problem' (why something keeps "
        "recurring - ITSM shorthand 'PRB' is mapped automatically), or 'hosting' (which application lives "
        "on which cluster/data center, independent of any incident).",
    )
    measure: str = Field("count", description="One of app.insights.whitelist.MEASURES - 'count' today")
    group_by: list[str] = Field(
        default_factory=list,
        description="Dimension keys to break the count down by, e.g. ['root_cause_category']. "
        "Empty means an ungrouped total.",
    )
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Dimension key -> allowed raw values, e.g. {'severity': ['Sev1']}. "
        "Use the schema's own vocabulary where known (Sev1..Sev4); ITSM shorthand like "
        "'P1' is also accepted and mapped automatically.",
    )
    opened_after: str | None = Field(None, description="ISO date YYYY-MM-DD, inclusive lower bound on when the incident opened")
    opened_before: str | None = Field(None, description="ISO date YYYY-MM-DD, exclusive upper bound on when the incident opened")


class InsightNarrative(BaseModel):
    """Prose around an already-computed app.insights.query_builder result.

    No field here holds a per-group count - those come straight from SQL and
    are rendered by Python, never re-typed by the model (a nested list field
    could not be checked by assert_no_number_drift anyway). total_count is
    the one aggregate worth the model restating, because restating it
    correctly is itself evidence the narrative is reading the table it was
    given rather than a different one.
    """

    headline: str = Field(description="One sentence stating the total and what it was filtered/grouped by")
    narrative: str = Field(description="The breakdown in prose, referencing only rows present in the evidence")
    insight: str = Field("", description="A notable pattern in the evidence worth calling out, e.g. a distribution that inverts the overall estate")
    caveats: list[str] = Field(default_factory=list, description="What this figure does NOT cover - e.g. open incidents excluded, a filter narrowing scope")
    total_count: int = Field(description="Must equal the evidence's total_count exactly")
