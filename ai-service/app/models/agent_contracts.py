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
    """A raw Scenario-B "new space" requirement extracted from free text."""

    environment: str
    cpu_cores: float
    memory_gb: float
    storage_gb: float
    platform: str
    availability_tier: str
    data_classification: str
    preferred_location: Optional[str] = None
    expected_growth_percent: float = 0.0
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
    """Narration over one already-scored candidate. ``overall_score`` and
    ``estimated_monthly_cost`` are echoed from evidence, never invented -
    app.agents.guards.assert_no_number_drift enforces this at call time.
    """

    cluster_code: str
    eligibility_status: str
    overall_score: Optional[float] = None
    estimated_monthly_cost: Optional[float] = None
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
    investigation_id: int
    title: str
    executive_summary: str
    top_recommendation: Optional[str] = None
    alternatives_considered: list[str] = []
    risks: list[str] = []
    next_steps: list[str] = []
    human_action_required: str
