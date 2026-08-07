"""The 19 nodes of InfrastructureRecommendationGraph.

Every node is a plain function ``(state) -> partial_state_update``. Numbers
only ever come from app.services / app.rules / app.scoring / app.forecasting;
the LLM (app.agents.chains) is used exclusively for narration, and its output
is validated against the same evidence before being trusted (see
app.agents.guards.assert_no_number_drift).

Routing (which investigation_type a query becomes) is decided here with
plain keyword/regex matching, not by asking the LLM to "choose" - this is
the same trust-boundary rule enforced everywhere else in the platform: the
LLM explains, it does not decide.
"""

from __future__ import annotations

import json
import re

import structlog

from app.agents.chains import (
    answer_grounded_question,
    explain_candidate,
    explain_cluster_right_sizing,
    extract_capacity_requirement,
    generate_final_report as generate_final_report_chain,
    parse_investigation_plan,
)
from app.agents.llm_factory import get_chat_model
from app.agents.mock_llm import MockChatModel
from app.forecasting.engine import forecast_cluster
from app.graph.state import InfrastructureRecommendationState
from app.config import get_settings
from app.models.enums import InvestigationType
from app.models.requirements import HostingRequirement
from app.models.scoring import CandidateScore
from app.repositories import (
    application_repository,
    cluster_repository,
    investigation_repository,
    recommendation_repository,
)
from app.retrieval.vector_store import get_vector_store
from app.services import consolidation, node_placement, placement, rightsizing
from app.utils.json_utils import to_jsonable

logger = structlog.get_logger(__name__)

_APP_CODE_RE = re.compile(r"\bAPP-[A-Z0-9]+\b")
_CLUSTER_CODE_RE = re.compile(r"\bCL-[A-Z0-9-]+\b")

_REFUSAL_KEYWORDS = [
    "provision", "deploy this", "migrate the", "decommission", "shut down", "shutdown",
    "execute the", "apply this change", "delete the cluster", "scale up now", "perform the migration",
    "carry out the move", "go ahead and", "make the change",
]
_FORECAST_KEYWORDS = ["forecast"]
_RIGHTSIZING_KEYWORDS = ["right-siz", "right siz", "underutilized", "under-utilized", "high-cost", "high cost", "overprovisioned"]
_CONSOLIDATION_KEYWORDS = ["consolidat"]
_QUESTION_KEYWORDS = ["why was", "why is", "compare", "show clusters", "at least", "generate a", "report"]


def classify_investigation_type(query: str) -> str:
    lower = query.lower()
    if any(k in lower for k in _REFUSAL_KEYWORDS):
        return InvestigationType.REFUSED
    if any(k in lower for k in _FORECAST_KEYWORDS):
        return InvestigationType.FORECAST
    if any(k in lower for k in _RIGHTSIZING_KEYWORDS):
        return InvestigationType.RIGHT_SIZING
    if any(k in lower for k in _CONSOLIDATION_KEYWORDS):
        return InvestigationType.CONSOLIDATION
    if any(k in lower for k in _QUESTION_KEYWORDS):
        return InvestigationType.QUESTION
    if _APP_CODE_RE.search(query.upper()) and ("host" in lower or "find" in lower or "place" in lower):
        return InvestigationType.HOSTING
    if "cpu" in lower and ("ram" in lower or "memory" in lower or "storage" in lower):
        return InvestigationType.CAPACITY
    return InvestigationType.QUESTION


# =============================================================================
# 1. parse_user_request
# =============================================================================


def parse_user_request(state: InfrastructureRecommendationState) -> dict:
    from app.config import get_settings

    query = state["user_query"][: get_settings().service.max_query_chars]
    # str(...) - InvestigationType is a StrEnum; LangGraph's checkpoint
    # serializer only round-trips plain str/int/etc without a deprecation
    # warning, so state never carries the enum instance itself.
    investigation_type = str(classify_investigation_type(query))
    llm = get_chat_model()
    try:
        plan = parse_investigation_plan(llm, query)
        parsed_intent = plan.model_dump()
    except Exception as exc:  # noqa: BLE001 - narration failure must not break the pipeline
        logger.warning("graph.parse_user_request.llm_failed", error=str(exc))
        parsed_intent = {"investigation_type": investigation_type, "summary": query, "steps": []}
    return {"investigation_type": investigation_type, "parsed_intent": parsed_intent, "user_query": query}


# =============================================================================
# 2. load_application_requirements
# =============================================================================


def load_application_requirements(state: InfrastructureRecommendationState) -> dict:
    query = state["user_query"]
    app_match = _APP_CODE_RE.search(query.upper())
    itype = state["investigation_type"]

    if itype in (InvestigationType.QUESTION, InvestigationType.REFUSED):
        return {}

    if app_match:
        app = application_repository.get_by_code(app_match.group(0))
        if app is None:
            return {"errors": [f"Application {app_match.group(0)} not found in CMDB."]}
        requirement = placement.requirement_for_application(app)
        return {
            "application_requirements": {"application_code": app.ApplicationCode, "application_id": app.ApplicationId},
            "requirement": to_jsonable(requirement),
        }

    if itype == InvestigationType.CAPACITY:
        extracted = _extract_capacity_requirement_via_llm(query)
        if extracted is not None:
            req = HostingRequirement(
                environment=extracted.environment, platform=extracted.platform, os_requirement="Any",
                cpu_cores=extracted.cpu_cores, memory_gb=extracted.memory_gb, storage_gb=extracted.storage_gb,
                growth_percent=extracted.expected_growth_percent, availability_tier=extracted.availability_tier,
                data_classification=extracted.data_classification, preferred_location=extracted.preferred_location,
                criticality="Medium",
            )
            return {
                "capacity_requirements": {
                    "cpu_cores": extracted.cpu_cores, "memory_gb": extracted.memory_gb,
                    "storage_gb": extracted.storage_gb, "extraction_method": "llm",
                },
                "requirement": to_jsonable(req),
            }
        return _capacity_requirement_from_regex(query)

    return {}


def _extract_capacity_requirement_via_llm(query: str):
    """Real free-text understanding when a real LLM provider is configured.

    The offline MockChatModel has no NLU - it can only echo numbers embedded
    verbatim as JSON in its own prompt (see app.agents.mock_llm), so prose
    like "8 CPUs" would fall through to its random filler instead of the
    literal 8. Regex extraction is strictly more correct there, so this only
    engages the LLM chain when a real provider (openai/azure-openai/ollama)
    is configured.
    """
    llm = get_chat_model()
    if isinstance(llm, MockChatModel):
        return None
    try:
        return extract_capacity_requirement(llm, query)
    except Exception as exc:  # noqa: BLE001 - extraction failure must not break the pipeline
        logger.warning("graph.load_application_requirements.llm_extraction_failed", error=str(exc))
        return None


def _capacity_requirement_from_regex(query: str) -> dict:
    cpu = _first_number(query, r"(\d+(?:\.\d+)?)\s*cpu")
    mem = _first_number(query, r"(\d+(?:\.\d+)?)\s*gb\s*(?:ram|memory)")
    storage_tb = _first_number(query, r"(\d+(?:\.\d+)?)\s*tb")
    storage_gb = _first_number(query, r"(\d+(?:\.\d+)?)\s*gb\s*storage")
    storage = (storage_tb * 1000) if storage_tb else (storage_gb or 500.0)
    req = HostingRequirement(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=cpu or 8.0, memory_gb=mem or 32.0, storage_gb=storage,
        growth_percent=0.0, availability_tier="Tier-2", data_classification="Internal",
        criticality="Medium",
    )
    return {
        "capacity_requirements": {
            "cpu_cores": cpu or 8.0, "memory_gb": mem or 32.0, "storage_gb": storage,
            "extraction_method": "regex",
        },
        "requirement": to_jsonable(req),
    }


def _first_number(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text.lower())
    return float(m.group(1)) if m else None


# =============================================================================
# 3. create_investigation_plan
# =============================================================================


def create_investigation_plan(state: InfrastructureRecommendationState) -> dict:
    # investigation_id is created by app.graph.graph.run_investigation() BEFORE
    # the graph starts (its value seeds the LangGraph thread_id, which must be
    # known up front so resume_investigation() can reconnect to this run).
    investigation_repository.update_status(state["investigation_id"], "Running")
    return {"investigation_plan": state.get("parsed_intent")}


# =============================================================================
# 4. identify_candidate_infrastructure
# =============================================================================


def identify_candidate_infrastructure(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype in (InvestigationType.HOSTING, InvestigationType.CAPACITY) and state.get("requirement"):
        requirement = HostingRequirement.model_validate(state["requirement"])
        clusters = placement.discover_candidate_clusters(requirement)
    else:
        clusters = cluster_repository.list_all(limit=500)
    return {"candidate_clusters": [to_jsonable(c) for c in clusters]}


# =============================================================================
# 5. apply_hard_eligibility_rules
# =============================================================================


def apply_hard_eligibility_rules(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype not in (InvestigationType.HOSTING, InvestigationType.CAPACITY) or not state.get("requirement"):
        return {"eligible_candidates": [], "rejected_candidates": []}

    requirement = HostingRequirement.model_validate(state["requirement"])
    ranked = placement.find_and_score_candidates(requirement)
    eligible = [to_jsonable(c) for c in ranked if c.eligibility_status == "Eligible"]
    rejected = [to_jsonable(c) for c in ranked if c.eligibility_status != "Eligible"]
    # Cached here so downstream nodes (which conceptually run capacity/scoring
    # again) can reuse the already-ranked result instead of recomputing.
    return {"eligible_candidates": eligible, "rejected_candidates": rejected, "candidate_scores": eligible + rejected}


# =============================================================================
# 6. calculate_current_capacity / 7. calculate_projected_utilization
# =============================================================================


def calculate_current_capacity(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype == InvestigationType.RIGHT_SIZING:
        clusters = cluster_repository.list_all(limit=500)
        results = [to_jsonable(rightsizing.analyze_cluster_right_sizing(c)) for c in clusters]
        return {"capacity_calculations": {"right_sizing": results}}
    if itype == InvestigationType.CONSOLIDATION:
        apps = application_repository.list_all(limit=200)
        results = [to_jsonable(r) for r in consolidation.find_consolidation_candidates(apps)]
        return {"capacity_calculations": {"consolidation": results}}
    # Hosting/Capacity: snapshots already carried on each candidate from step 5.
    snapshots = {c["cluster_code"]: c.get("snapshot") for c in state.get("candidate_scores", [])}
    return {"capacity_calculations": {"cluster_snapshots": snapshots}}


def calculate_projected_utilization(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype not in (InvestigationType.HOSTING, InvestigationType.CAPACITY):
        return {}
    projections = {c["cluster_code"]: c.get("projected") for c in state.get("candidate_scores", [])}
    calc = dict(state.get("capacity_calculations", {}))
    calc["projected_utilization"] = projections
    return {"capacity_calculations": calc}


# =============================================================================
# 8. run_capacity_forecast
# =============================================================================


def run_capacity_forecast(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype == InvestigationType.FORECAST:
        cluster_match = _CLUSTER_CODE_RE.search(state["user_query"].upper())
        horizon_match = re.search(r"(\d+)\s*day", state["user_query"].lower())
        horizon = int(horizon_match.group(1)) if horizon_match else 90
        if not cluster_match:
            return {"errors": ["No cluster code found in forecast request."]}
        cluster = cluster_repository.get_by_code(cluster_match.group(0))
        if cluster is None:
            return {"errors": [f"Cluster {cluster_match.group(0)} not found."]}
        result = forecast_cluster(cluster, horizon_days=horizon)
        return {"forecast_results": {cluster.ClusterCode: to_jsonable(result)}}

    if itype in (InvestigationType.HOSTING, InvestigationType.CAPACITY):
        top = state.get("candidate_scores", [])[:3]
        results = {}
        for c in top:
            if c.get("eligibility_status") != "Eligible":
                continue
            cluster = cluster_repository.get_by_code(c["cluster_code"])
            if cluster is None:
                continue
            try:
                results[cluster.ClusterCode] = to_jsonable(forecast_cluster(cluster, horizon_days=90))
            except ValueError:
                continue
        return {"forecast_results": results}

    return {}


# =============================================================================
# 9. analyze_dependencies
# =============================================================================


def analyze_dependencies(state: InfrastructureRecommendationState) -> dict:
    if not state.get("requirement"):
        return {}
    requirement = HostingRequirement.model_validate(state["requirement"])
    return {"application_requirements": {**(state.get("application_requirements") or {}), "dependency_count": len(requirement.dependency_checks)}}


# =============================================================================
# 10. calculate_candidate_scores / 11. rank_candidates
# =============================================================================


def calculate_candidate_scores(state: InfrastructureRecommendationState) -> dict:
    # Scoring already happened in apply_hard_eligibility_rules (via
    # placement.find_and_score_candidates) for Hosting/Capacity. This node is
    # the named pipeline stage the specification calls for; it is a
    # pass-through here by design - see module docstring.
    return {}


def rank_candidates(state: InfrastructureRecommendationState) -> dict:
    scores = state.get("candidate_scores", [])
    ranked = sorted(scores, key=lambda c: (c.get("eligibility_status") != "Eligible", c.get("rank") or 999))
    return {"candidate_scores": ranked}


# =============================================================================
# 11b. select_candidate_nodes
#
# Added after the original 18-node spec: recommendations stopped at the
# cluster boundary, which left an infra engineer to pick a host by hand. This
# node drills the leading clusters down to their best individual hosts. It is
# placed after ranking on purpose - it needs the final cluster order to know
# which clusters are worth the per-host queries.
# =============================================================================


def select_candidate_nodes(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    if itype not in (InvestigationType.HOSTING, InvestigationType.CAPACITY) or not state.get("requirement"):
        return {}

    requirement = HostingRequirement.model_validate(state["requirement"])
    try:
        scored = [CandidateScore.model_validate(c) for c in state.get("candidate_scores", [])]
        node_placement.attach_top_nodes(requirement, scored)
    except Exception as exc:  # noqa: BLE001
        # Node drill-down is an enrichment, not a precondition: a failure here
        # degrades to cluster-only recommendations rather than losing the
        # whole investigation.
        logger.warning("graph.select_candidate_nodes_failed", error=str(exc))
        return {"errors": [f"Node-level selection failed: {exc}"]}

    candidate_scores = [to_jsonable(c) for c in scored]
    return {
        "candidate_scores": candidate_scores,
        "eligible_candidates": [c for c in candidate_scores if c.get("eligibility_status") == "Eligible"],
        "rejected_candidates": [c for c in candidate_scores if c.get("eligibility_status") != "Eligible"],
        "candidate_nodes": [to_jsonable(n) for c in scored for n in c.top_nodes],
    }


# =============================================================================
# 12. retrieve_related_context
# =============================================================================


def retrieve_related_context(state: InfrastructureRecommendationState) -> dict:
    # Retrieval is optional grounding for narration, never a hard dependency -
    # a runtime embedding failure (e.g. a real API embedder going down after a
    # successful startup probe) degrades to no retrieved context rather than
    # failing the whole investigation.
    try:
        store = get_vector_store()
        results = store.search(state["user_query"], top_k=6)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph.retrieve_related_context_failed", error=str(exc))
        return {"retrieved_context": []}
    return {"retrieved_context": [{"text": r.document.text, "score": r.score, "entity_type": r.document.entity_type} for r in results]}


# =============================================================================
# 13. generate_recommendation_explanations
# =============================================================================


def generate_recommendation_explanations(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    llm = get_chat_model()
    app_code = (state.get("application_requirements") or {}).get("application_code")

    if itype in (InvestigationType.HOSTING, InvestigationType.CAPACITY):
        top = [c for c in state.get("candidate_scores", []) if c.get("eligibility_status") == "Eligible"][:3]
        explanations = []
        for c in top:
            try:
                candidate = CandidateScore.model_validate(c)
                explanation = explain_candidate(llm, candidate, app_code)
                explanations.append(explanation.model_dump())
            except Exception as exc:  # noqa: BLE001
                logger.warning("graph.explain_candidate_failed", cluster=c.get("cluster_code"), error=str(exc))
        return {"recommendation_explanations": explanations}

    if itype == InvestigationType.RIGHT_SIZING:
        results = state.get("capacity_calculations", {}).get("right_sizing", [])
        flagged = [r for r in results if r["classification"] != "Healthy"][:5]
        explanations = []
        for r in flagged:
            try:
                from app.models.rightsizing import ClusterRightSizingResult

                explanation = explain_cluster_right_sizing(llm, ClusterRightSizingResult.model_validate(r))
                explanations.append(explanation.model_dump())
            except Exception as exc:  # noqa: BLE001
                logger.warning("graph.explain_rightsizing_failed", error=str(exc))
        return {"recommendation_explanations": explanations}

    if itype == InvestigationType.QUESTION:
        try:
            answer = answer_grounded_question(llm, state["user_query"], state.get("retrieved_context", []))
            return {"recommendation_explanations": [answer.model_dump()]}
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph.grounded_qa_failed", error=str(exc))
            return {"recommendation_explanations": []}

    return {}


# =============================================================================
# 14. assess_risk_and_confidence
# =============================================================================


def assess_risk_and_confidence(state: InfrastructureRecommendationState) -> dict:
    from app.config import get_settings

    itype = state["investigation_type"]
    if itype in (InvestigationType.QUESTION, InvestigationType.FORECAST):
        # Informational only - no recommendation is being proposed for approval.
        return {"confidence": "High", "human_review_required": False}

    eligible = [c for c in state.get("candidate_scores", []) if c.get("eligibility_status") == "Eligible"]
    if not eligible:
        return {"confidence": "Low", "human_review_required": True}
    top_score = float(eligible[0].get("overall_score") or 0)
    threshold = get_settings().scoring.min_confident_score
    confidence = "High" if top_score >= threshold else "Low"
    return {"confidence": confidence, "human_review_required": True}


# =============================================================================
# 15. human_review_interrupt
# =============================================================================


def human_review_interrupt(state: InfrastructureRecommendationState) -> dict:
    from langgraph.types import interrupt

    top = state.get("candidate_scores", [])[: get_settings().policy.top_clusters]
    summary = {
        "investigation_id": state.get("investigation_id"),
        "investigation_type": state.get("investigation_type"),
        "top_candidates": [c.get("cluster_code") for c in top],
        # The hosts inside each shortlisted cluster - the reviewer approves a
        # cluster *and* the hosts proposed within it, so both belong here.
        "top_hosts_by_cluster": {
            c.get("cluster_code"): [n.get("host_name") for n in (c.get("top_nodes") or [])]
            for c in top
        },
        "confidence": state.get("confidence"),
        "message": "Human review required before this recommendation is finalized.",
    }
    investigation_repository.update_status(state["investigation_id"], "AwaitingReview")
    resumed = interrupt(summary)
    return {
        "decision": resumed.get("decision"),
        "reviewer_employee_id": resumed.get("reviewer_employee_id"),
        "review_comments": resumed.get("comments"),
    }


# =============================================================================
# 16. generate_final_report
# =============================================================================


def generate_final_report(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    investigation_id = state.get("investigation_id")

    if itype == InvestigationType.REFUSED:
        report = {
            "investigation_id": investigation_id, "title": "Request refused",
            "executive_summary": (
                "This request asks the platform to execute an infrastructure change "
                "(provisioning, decommissioning or migration). SeekAndDestroy produces "
                "recommendations only - it never executes infrastructure changes."
            ),
            "top_recommendation": None, "alternatives_considered": [], "risks": [],
            "next_steps": ["Have an infrastructure engineer review the underlying recommendation and execute the change through your standard change-management process."],
            "human_action_required": "None from this platform - route the approved change through your change-management process.",
        }
        return {"final_report": report}

    llm = get_chat_model()
    evidence = {
        "top_candidates": state.get("candidate_scores", [])[:5],
        "explanations": state.get("recommendation_explanations", []),
        "confidence": state.get("confidence"),
        "forecast_results": state.get("forecast_results"),
        "capacity_calculations": state.get("capacity_calculations"),
        "decision": state.get("decision"),
    }
    title = f"{itype} investigation for: {state['user_query'][:80]}"
    try:
        report = generate_final_report_chain(llm, investigation_id, title, evidence)
        return {"final_report": report.model_dump()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph.generate_final_report_failed", error=str(exc))
        return {
            "final_report": {
                "investigation_id": investigation_id, "title": title,
                "executive_summary": "Report narration unavailable; see candidate_scores and recommendation_explanations for grounded results.",
                "top_recommendation": (state.get("candidate_scores") or [{}])[0].get("cluster_code"),
                "alternatives_considered": [], "risks": [], "next_steps": [],
                "human_action_required": "Review recommendations and submit a decision.",
            }
        }


# =============================================================================
# 17. persist_recommendations
# =============================================================================


def _node_explanation(node: dict) -> str:
    """Deterministic one-line summary for a node recommendation.

    Built from the node's own computed values, never narrated by the LLM -
    node-level explanations carry numbers, and numbers do not come from a
    language model anywhere in this platform.
    """
    projected = node.get("projected") or {}
    snapshot = node.get("snapshot") or {}
    return (
        f"Host {node.get('host_name')} in cluster {node.get('cluster_code')}: projected "
        f"CPU {projected.get('projected_cpu_utilization_percent')}%, "
        f"memory {projected.get('projected_memory_utilization_percent')}%, "
        f"storage {projected.get('projected_storage_utilization_percent')}% after placement, "
        f"leaving {projected.get('projected_headroom_percent')}% headroom "
        f"({snapshot.get('measurement_sample_count', 0)} utilization samples in window)."
    )


def persist_recommendations(state: InfrastructureRecommendationState) -> dict:
    """Writes the shortlist a human will review: the top N clusters and, inside
    each, the top M hosts (``SAD_POLICY__TOP_CLUSTERS`` /
    ``SAD_POLICY__TOP_NODES_PER_CLUSTER``, both 3 by default).

    Node rows point at the same investigation and are linked to their cluster
    through ``ClusterNode.ClusterId``, plus ``parent_cluster_code`` in
    EvidenceJson so a reader never has to join to know what a host belongs to.
    Node rows leave CompatibilityScore/ResiliencyScore/DependencyScore NULL by
    design - those are cluster properties, identical for every sibling host,
    and are already recorded on the parent cluster's row.
    """
    itype = state["investigation_type"]
    if itype not in (InvestigationType.HOSTING, InvestigationType.CAPACITY):
        return {}

    settings = get_settings()
    investigation_id = state["investigation_id"]
    app_id = (state.get("application_requirements") or {}).get("application_id")
    explanations_by_cluster = {e.get("cluster_code"): e for e in state.get("recommendation_explanations", [])}
    rec_type = "HostingPlacement" if itype == InvestigationType.HOSTING else "NewCapacity"

    saved_ids = []
    for c in state.get("candidate_scores", [])[: settings.policy.top_clusters]:
        explanation = explanations_by_cluster.get(c["cluster_code"])
        sub = c.get("subscores") or {}
        rec = {
            "InvestigationId": investigation_id, "CapacityRequestId": None, "ApplicationId": app_id,
            "RecommendationType": rec_type,
            "CandidateEntityType": "Cluster", "CandidateEntityId": c["cluster_id"], "Rank": c["rank"],
            "EligibilityStatus": c["eligibility_status"], "OverallScore": c.get("overall_score"),
            "CapacityScore": sub.get("capacity"), "CompatibilityScore": sub.get("compatibility"),
            "CostScore": sub.get("cost"), "ResiliencyScore": sub.get("resiliency"),
            "DependencyScore": sub.get("dependency"), "RiskScore": sub.get("risk"),
            "ProjectedCpuUtilization": (c.get("projected") or {}).get("projected_cpu_utilization_percent"),
            "ProjectedMemoryUtilization": (c.get("projected") or {}).get("projected_memory_utilization_percent"),
            "ProjectedStorageUtilization": (c.get("projected") or {}).get("projected_storage_utilization_percent"),
            "ProjectedHeadroomPercent": (c.get("projected") or {}).get("projected_headroom_percent"),
            "EstimatedMonthlyCost": c.get("estimated_monthly_cost"),
            "Explanation": explanation.get("summary") if explanation else None,
            "EvidenceJson": json.dumps(c, default=str),
            "Status": "PendingReview",
        }
        saved_ids.append(recommendation_repository.save(rec))

        for n in (c.get("top_nodes") or [])[: settings.policy.top_nodes_per_cluster]:
            nsub = n.get("subscores") or {}
            nprojected = n.get("projected") or {}
            saved_ids.append(
                recommendation_repository.save(
                    {
                        "InvestigationId": investigation_id, "CapacityRequestId": None, "ApplicationId": app_id,
                        "RecommendationType": rec_type,
                        "CandidateEntityType": "Node", "CandidateEntityId": n["node_id"], "Rank": n["rank"],
                        "EligibilityStatus": n["eligibility_status"], "OverallScore": n.get("overall_score"),
                        "CapacityScore": nsub.get("capacity"), "CompatibilityScore": None,
                        "CostScore": nsub.get("cost"), "ResiliencyScore": None,
                        "DependencyScore": None, "RiskScore": nsub.get("risk"),
                        "ProjectedCpuUtilization": nprojected.get("projected_cpu_utilization_percent"),
                        "ProjectedMemoryUtilization": nprojected.get("projected_memory_utilization_percent"),
                        "ProjectedStorageUtilization": nprojected.get("projected_storage_utilization_percent"),
                        "ProjectedHeadroomPercent": nprojected.get("projected_headroom_percent"),
                        "EstimatedMonthlyCost": n.get("estimated_monthly_cost"),
                        "Explanation": _node_explanation(n),
                        "EvidenceJson": json.dumps(
                            {
                                **n,
                                "parent_cluster_id": c["cluster_id"],
                                "parent_cluster_code": c["cluster_code"],
                                "parent_cluster_rank": c["rank"],
                                "reliability_score": nsub.get("reliability"),
                            },
                            default=str,
                        ),
                        "Status": "PendingReview",
                    }
                )
            )

    return {"errors": [] if saved_ids else ["No candidates were persisted."]}


# =============================================================================
# 18. complete_investigation
# =============================================================================


def complete_investigation(state: InfrastructureRecommendationState) -> dict:
    investigation_id = state.get("investigation_id")
    if investigation_id:
        status = "AwaitingReview" if state.get("human_review_required") and not state.get("decision") else "Completed"
        if state["investigation_type"] in (InvestigationType.QUESTION, InvestigationType.REFUSED):
            status = "Completed"
        investigation_repository.mark_completed(investigation_id, status=status)
    return {}
