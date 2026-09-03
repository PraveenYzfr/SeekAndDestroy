"""Pydantic contracts the LLM layer must produce structured output against.

These are the only shapes the LLM is ever allowed to fill in. None of them
carry a raw numeric score field the model could invent - scores, costs and
utilization numbers are always passed in as already-computed context and the
model is instructed (and, for the candidate/right-sizing/forecast explanation
types, verified by app.agents.guards) to echo them back unchanged.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ApplicationHostingRequirement(BaseModel):
    """Extracted from a natural-language hosting request."""

    application_code: Optional[str] = Field(None, description="Existing application code if referenced, e.g. APP-PAYMENTS")
    environment: Optional[str] = Field(None, description="Production | Staging | Test | Development")
    cpu_cores: Optional[float] = None
    memory_gb: Optional[float] = None
    storage_gb: Optional[float] = None
    platform: Optional[str] = Field(None, description="Kubernetes | VMware | OpenShift | BareMetal | Hyper-V")
    availability_tier: Optional[str] = Field(None, description="Tier-1 | Tier-2 | Tier-3")
    data_classification: Optional[str] = Field(None, description="Public | Internal | Confidential | Restricted")
    preferred_location: Optional[str] = None
    expected_growth_percent: Optional[float] = None
    notes: str = ""


class CapacityRequirement(BaseModel):
    """A raw Scenario-B "new space" requirement extracted from free text.

    EVERY DIMENSION IS OPTIONAL, BECAUSE THE ENGINEER IS NOT OBLIGED TO STATE IT.

    These fields were required and non-nullable, which sounds like strictness and
    behaved as the opposite. "Where can I host a Tier-1 production Java app
    needing 32 cores and 128 GB?" says nothing about storage or data
    classification, so the model correctly returned null for both - and pydantic
    rejected its own perfectly good answer:

        storage_gb          Input should be a valid number, input_value=None
        data_classification Input should be a valid string, input_value=None

    The rejection then triggered the repair retry in app.agents.structured, which
    asked the model to fix JSON that was never malformed. It returned the same
    correct object, was rejected again, and the whole extraction fell through to
    regex. Measured in production: nine calls, 100% failure, 68s average and 101s
    worst - two full model calls burned per investigation to arrive at an answer
    the FIRST one already had.

    A contract that forbids null does not make the value known. It only moves the
    invention somewhere less visible: either the model fabricates a number to
    satisfy the schema - which is far worse than null, because a fabricated 500 GB
    is indistinguishable from a stated one - or extraction fails and the platform
    defaults it anyway, having paid twice for nothing.

    So null is now sayable. app.graph.nodes resolves each missing dimension
    against _CAPACITY_DEFAULTS and records it in ``assumed_defaults``, exactly as
    the regex path has always done, which keeps the guarantee that matters: a
    reviewer approving 8 cores can see that nobody asked for 8.
    """

    environment: Optional[str] = None
    cpu_cores: Optional[float] = None
    memory_gb: Optional[float] = None
    storage_gb: Optional[float] = None
    platform: Optional[str] = None
    availability_tier: Optional[str] = None
    data_classification: Optional[str] = None
    preferred_location: Optional[str] = None
    # Nullable for the same reason as the rest: a model asked for growth
    # nobody mentioned should be able to say "not stated" rather than pick
    # 0.0 and have it read as a deliberate flat-growth assumption.
    expected_growth_percent: Optional[float] = None
    required_by_days: Optional[int] = None


class InvestigationStep(BaseModel):
    step: str
    tool_name: Optional[str] = None
    rationale: str


class InvestigationPlan(BaseModel):
    investigation_type: str = Field(description="Hosting | Capacity | RightSizing | Consolidation | Forecast | Question | Refused")
    summary: str
    steps: list[InvestigationStep]
    requires_human_review: bool = True


class CandidateExplanation(BaseModel):
    """Narration over one already-scored candidate. ``overall_score`` is echoed
    from evidence, never invented - app.agents.guards.assert_no_number_drift
    enforces this at call time.

    Cost is deliberately absent. It is an internal chargeback rate rather than
    spend, so a sentence like "an estimated monthly cost of 7000.0" reads as a
    real figure driving the recommendation when it is neither. A model can only
    quote a number it was handed, so the field is gone from the evidence too.
    """

    cluster_code: str
    eligibility_status: str
    overall_score: Optional[float] = None
    summary: str
    key_strengths: list[str] = []
    key_risks: list[str] = []


class RightSizingExplanation(BaseModel):
    cluster_or_application_code: str
    classification: str
    summary: str
    recommended_action: str
    estimated_monthly_savings: Optional[float] = None


class ForecastExplanation(BaseModel):
    entity_code: str
    resource: str
    summary: str
    recommended_action: str
    exhaustion_date: Optional[str] = None


class TradeOffSummary(BaseModel):
    title: str
    comparison_points: list[str]
    recommendation: str


class GroundedAnswer(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list, description="Entity codes/documents the answer is grounded in")
    confidence: str = Field(description="High | Medium | Low")


class FinalRecommendationReport(BaseModel):
    #: NOT SUPPLIED BY THE MODEL, and it never should have been.
    #:
    #: This was a required int. The platform passes the investigation id INTO the
    #: prompt as evidence and then demanded the model echo it back; models
    #: correctly returned null for a field they have no way to know, and pydantic
    #: threw the entire report away over it.
    #:
    #: Measured on the 100-case golden run: 29 final reports failed to parse and
    #: 28 of them failed on this one field. Every one contained a complete, well
    #: formed report - title, summary, risks, next steps - discarded because an
    #: identifier the platform already held was absent.
    #:
    #: That is what "Report narration unavailable" was on screen: not a model
    #: that could not write, a contract that would not accept what it wrote.
    #:
    #: Optional here and set authoritatively by generate_final_report after
    #: parsing. Asking a model to carry an identifier is also a way to let it
    #: change one - the same reason assert_no_number_drift exists.
    investigation_id: Optional[int] = None
    title: str
    executive_summary: str
    top_recommendation: Optional[str] = None
    alternatives_considered: list[str] = []
    risks: list[str] = []
    next_steps: list[str] = []
    human_action_required: str
