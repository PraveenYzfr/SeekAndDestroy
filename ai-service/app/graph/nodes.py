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
from app.agents.llm_factory import get_chat_model, get_chat_model_for_role
from app.graph import scope
from app.agents.mock_llm import MockChatModel
from app.forecasting.engine import forecast_cluster
from app.graph.state import InfrastructureRecommendationState
from app.config import get_settings
from app.models.enums import (
    AvailabilityTier,
    DataClassification,
    Environment,
    InvestigationType,
    TechnologyPlatform,
)
from app.models.requirements import HostingRequirement
from app.models.scoring import CandidateScore
from app.repositories import (
    application_repository,
    cluster_repository,
    investigation_repository,
    recommendation_repository,
)
from app.retrieval.vector_store import get_vector_store
from app.services import consolidation, node_placement, placement, refinement, rightsizing
from app.utils.json_utils import to_jsonable

logger = structlog.get_logger(__name__)

#: Application codes as the CMDB actually writes them. `APP-[A-Z0-9]+` stopped
#: at the first hyphen, so APP-AML-API0044 was read as APP-AML - a code that
#: does not exist - and the investigation answered "Application APP-AML not
#: found in CMDB" for a perfectly valid request. 1,160 of the 1,200
#: applications in the ITSM corpus carry two or more hyphens, so this hit 97%
#: of them. It was invisible while the corpus had 40 single-hyphen codes.
#:
#: The trailing repeat is deliberate: APP-PAYMENTS-GW0012 must match whole,
#: not as APP-PAYMENTS with a fragment left behind.
_APP_CODE_RE = re.compile(r"\bAPP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
#: Cluster codes as they actually appear in the CMDB: `atl-03` and `cmh-p212`.
#: This used to be `\bCL-[A-Z0-9-]+\b`, which matched 0 of the 256 clusters in
#: the database - it had never fired once. Two things were quietly broken by
#: that and neither produced an error:
#:   * run_capacity_forecast could never find a cluster named in the query, so
#:     "forecast cmh-p212" silently returned a fleet-wide forecast instead
#:   * conversation.names_a_cluster was permanently False
#: Case-insensitive because callers search both the raw query and query.upper().
#: A node name (`cmh-p212-NODE-04`) yields its cluster, which is the useful
#: reading of "why was cmh-p212-NODE-04 rejected".
_CLUSTER_CODE_RE = re.compile(r"\b[a-z]{3}-p?\d{2,3}\b", re.IGNORECASE)

#: "Why was X rejected/not eligible for Y" - a question about a rule verdict,
#: not about documents. See _rejection_rule_evidence.
_REJECTION_QUESTION_RE = re.compile(
    r"\bwhy\b[^?]*\b(rejected|not eligible|ineligible|not chosen|not selected|excluded|ruled out|failed)\b",
    re.IGNORECASE,
)

_REFUSAL_KEYWORDS = [
    "provision", "deploy this", "migrate the", "decommission", "shut down", "shutdown",
    "execute the", "apply this change", "delete the cluster", "scale up now", "perform the migration",
    "carry out the move", "go ahead and", "make the change",
]
_FORECAST_KEYWORDS = ["forecast"]
_RIGHTSIZING_KEYWORDS = ["right-siz", "right siz", "underutilized", "under-utilized", "high-cost", "high cost", "overprovisioned"]
_CONSOLIDATION_KEYWORDS = ["consolidat"]
_QUESTION_KEYWORDS = ["why was", "why is", "compare", "show clusters", "at least", "generate a", "report"]

#: A number attached to a resource unit - "32 core", "8 vCPU", "64GB RAM",
#: "500 GB storage". This is what a capacity request actually looks like when
#: someone types it, and requiring the literal words "cpu" AND "ram" missed
#: most of them: "find a 32 core box for a java app" classified as a general
#: question, went to grounded Q&A, and answered "I don't have enough grounded
#: information" - technically true and completely useless.
_RESOURCE_QUANTITY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:x\s*)?"
    r"(?:core|cores|cpu|cpus|vcpu|vcpus|"
    r"gb|gib|tb|mb|gig|gigs|"
    r"ram|memory|storage|disk)\b",
    re.IGNORECASE,
)

#: Words that make a resource quantity a *request for placement* rather than a
#: statement about existing infrastructure. Without this, "which clusters have
#: 32 cores free" would be dragged into a capacity request instead of being
#: answered as the question it is.
_PROVISIONING_WORDS = ("need", "want", "find", "host", "place", "provision", "require", "looking for", "get me")


#: Greetings and pleasantries. These are not infrastructure questions and must
#: not become Investigation rows - "hi" producing "Investigation Report for hi:
#: I don't have enough grounded information" is a correct answer to a question
#: nobody asked, and it buries the real investigations in noise.
_SMALLTALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|hiya|howdy|thanks|thank you|ta|cheers|ok|okay|cool|nice|"
    r"good (morning|afternoon|evening)|how\s*a?r\s*e?\s*y?o?u|how r u|what'?s up|sup)"
    r"[\s!.?]*$",
    re.IGNORECASE,
)

#: Words that make a request infrastructure-shaped without saying enough to act
#: on. "hosting java app" is a real ask with the specifics missing - answering
#: "I don't have enough grounded information" is technically true and useless
#: when the honest reply is "which application, or how much CPU and memory?".
_INFRA_INTENT_WORDS = ("host", "cluster", "capacity", "node", "server", "deploy", "place", "workload", "app")


def quick_reply(query: str) -> str | None:
    """A direct answer for input that should never reach the graph.

    Returns the reply text, or None when the query is a real investigation.
    Two cases: conversational openers, and infrastructure asks too vague to
    act on. Both previously created an Investigation row, ran the full
    pipeline, and reported that the retrieved context was empty.
    """
    text = query.strip()
    if not text:
        return "Ask me where to host an application, how much spare capacity a cluster has, or which clusters could be right-sized."

    if _SMALLTALK_RE.match(text):
        return (
            "Hello. I find infrastructure for workloads - tell me an application code "
            "(for example APP-CRM), or the resources you need such as \"32 cores and 128 GB RAM\", "
            "and I will rank the clusters and hosts that can take it."
        )

    # Nothing from this estate in it at all - not an identifier, not a resource
    # quantity, not one word of domain vocabulary. "Who is the best actor in
    # India?" ran the whole graph and reported, at High confidence, that
    # dal-p056-NODE-01 holds no information about actors: an Investigation row,
    # a model call and a retrieval spent to say so.
    #
    # Checked before the classifier, because classify_investigation_type() has
    # no "none of the above" - everything it does not recognise becomes a
    # Question, which is precisely how an off-topic query reaches the model.
    out_of_scope = scope.out_of_scope_reply(text)
    if out_of_scope is not None:
        return out_of_scope

    lower = text.lower()
    # Only ever intercept queries that would otherwise fall through to the
    # generic Question path. "Which clusters are underutilized?" mentions a
    # cluster and is short, but it classifies as RightSizing and is perfectly
    # answerable - asking the engineer to be more specific would be worse than
    # useless. Deferring to the classifier keeps this heuristic from swallowing
    # every real query that happens to be brief.
    if classify_investigation_type(text) != InvestigationType.QUESTION:
        return None

    looks_infra = any(w in lower for w in _INFRA_INTENT_WORDS)
    has_app_code = bool(_APP_CODE_RE.search(text.upper()))
    has_quantity = bool(_RESOURCE_QUANTITY_RE.search(lower))
    # Infrastructure-shaped, but with nothing to compute against. Short and
    # specific beats a full investigation that can only report emptiness.
    if looks_infra and not has_app_code and not has_quantity and len(text.split()) <= 6:
        return (
            "I need a bit more to work with. Either name the application "
            "(for example \"find hosting for APP-CRM\") or give me the resources it needs "
            "(for example \"32 cores, 128 GB RAM and 2 TB storage in production\")."
        )
    return None


def effective_query(state: InfrastructureRecommendationState) -> str:
    """The text the pipeline reasons over.

    ``resolved_query`` is ``user_query`` with the previous subject carried
    forward when this turn is a follow-up (app.graph.conversation), and equal
    to ``user_query`` otherwise. Classification and requirement extraction read
    this; anything shown back to the engineer reads ``user_query``, which is
    always the literal text they typed.
    """
    return state.get("resolved_query") or state["user_query"]


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
    # A quantity of a resource, asked for in provisioning terms - "find a 32
    # core box", "I need 8 vCPU and 64GB". The old test required the literal
    # words "cpu" AND ("ram" OR "memory" OR "storage") together, which missed
    # every phrasing that used "core" or gave a single dimension.
    if _RESOURCE_QUANTITY_RE.search(lower) and any(w in lower for w in _PROVISIONING_WORDS):
        return InvestigationType.CAPACITY
    # Kept as a fallback for the multi-dimension phrasing even when no
    # provisioning verb is present ("8 CPU, 32 GB RAM, 500 GB storage").
    if "cpu" in lower and ("ram" in lower or "memory" in lower or "storage" in lower):
        return InvestigationType.CAPACITY
    return InvestigationType.QUESTION


# =============================================================================
# 1. parse_user_request
# =============================================================================


def parse_user_request(state: InfrastructureRecommendationState) -> dict:
    from app.config import get_settings

    max_chars = get_settings().service.max_query_chars
    query = state["user_query"][:max_chars]
    # A follow-up classifies on the carried-forward subject, not on its own
    # words: "what about in staging?" contains nothing to route on, and
    # routing it as a general question is exactly the bug conversations exist
    # to fix.
    resolved = effective_query(state)[:max_chars]
    # str(...) - InvestigationType is a StrEnum; LangGraph's checkpoint
    # serializer only round-trips plain str/int/etc without a deprecation
    # warning, so state never carries the enum instance itself.
    investigation_type = str(classify_investigation_type(resolved))
    llm = get_chat_model_for_role("planning")
    try:
        plan = parse_investigation_plan(llm, resolved)
        parsed_intent = plan.model_dump()
    except Exception as exc:  # noqa: BLE001 - narration failure must not break the pipeline
        logger.warning("graph.parse_user_request.llm_failed", error=str(exc))
        parsed_intent = {"investigation_type": investigation_type, "summary": resolved, "steps": []}
    return {
        "investigation_type": investigation_type, "parsed_intent": parsed_intent,
        "user_query": query, "resolved_query": resolved,
    }


# =============================================================================
# 2. load_application_requirements
# =============================================================================


def load_application_requirements(state: InfrastructureRecommendationState) -> dict:
    # The resolved query, so a follow-up finds the application code or the
    # capacity figures carried over from the request it continues.
    query = effective_query(state)
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
            environment, env_ok = _coerce_enum(extracted.environment, Environment, Environment.PRODUCTION)
            platform, platform_ok = _coerce_enum(extracted.platform, TechnologyPlatform, TechnologyPlatform.KUBERNETES)
            tier, tier_ok = _coerce_enum(extracted.availability_tier, AvailabilityTier, AvailabilityTier.TIER_2)
            classification, class_ok = _coerce_enum(
                extracted.data_classification, DataClassification, DataClassification.INTERNAL
            )
            coerced = [
                name for name, ok in (
                    ("environment", env_ok), ("platform", platform_ok),
                    ("availability_tier", tier_ok), ("data_classification", class_ok),
                ) if not ok
            ]
            if coerced:
                logger.warning(
                    "graph.capacity_extraction_coerced", fields=coerced,
                    environment=extracted.environment, platform=extracted.platform,
                    availability_tier=extracted.availability_tier,
                    data_classification=extracted.data_classification,
                )
            req = HostingRequirement(
                environment=environment, platform=platform, os_requirement="Any",
                cpu_cores=extracted.cpu_cores, memory_gb=extracted.memory_gb, storage_gb=extracted.storage_gb,
                growth_percent=extracted.expected_growth_percent, availability_tier=tier,
                data_classification=classification, preferred_location=extracted.preferred_location,
                criticality="Medium",
            )
            return {
                "capacity_requirements": {
                    "cpu_cores": extracted.cpu_cores, "memory_gb": extracted.memory_gb,
                    "storage_gb": extracted.storage_gb, "extraction_method": "llm",
                    "coerced_fields": coerced,
                },
                "requirement": to_jsonable(req),
            }
        return _capacity_requirement_from_regex(query)

    return {}


def _coerce_enum(value: str | None, enum_cls, fallback: str) -> tuple[str, bool]:
    """Force a free-text value onto a real enum member, reporting whether it
    already was one.

    The extraction contract types environment/platform/tier/classification as
    plain ``str``, so a model can return anything and it lands directly in a
    hard eligibility rule. Real Gemini output for "find a 32 core for hosting
    java app" was ``environment='production'`` (lowercase),
    ``platform='Java'`` (a language, not a platform) and
    ``availability_tier='Standard'`` (not a tier at all). The result was all
    133 clusters rejected with reasons that read like nonsense - *"Production/
    production workloads may not be placed on 'Production' infrastructure"*.

    This is the trust boundary applied to categories rather than numbers: a
    model may transcribe what an engineer stated, but it does not get to
    invent a value that a deterministic rule will then treat as authoritative.
    """
    if value:
        text = str(value).strip().casefold()
        for member in enum_cls:
            if text == str(member).casefold():
                return str(member), True
    return str(fallback), False


def _extract_capacity_requirement_via_llm(query: str):
    """Real free-text understanding when a real LLM provider is configured.

    The offline MockChatModel has no NLU - it can only echo numbers embedded
    verbatim as JSON in its own prompt (see app.agents.mock_llm), so prose
    like "8 CPUs" would fall through to its random filler instead of the
    literal 8. Regex extraction is strictly more correct there, so this only
    engages the LLM chain when a real provider (openai/azure-openai/ollama)
    is configured.
    """
    llm = get_chat_model_for_role("extraction")
    if isinstance(llm, MockChatModel):
        return None
    try:
        return extract_capacity_requirement(llm, query)
    except Exception as exc:  # noqa: BLE001 - extraction failure must not break the pipeline
        logger.warning("graph.load_application_requirements.llm_extraction_failed", error=str(exc))
        return None


#: Defaults for dimensions the user did not state. Recorded as "assumed" in
#: capacity_requirements rather than silently blended with stated values - a
#: number the platform invented must never be indistinguishable from one the
#: engineer gave it.
_CAPACITY_DEFAULTS = {"cpu_cores": 8.0, "memory_gb": 32.0, "storage_gb": 500.0}


def _capacity_requirement_from_regex(query: str) -> dict:
    """Deterministic extraction, used when no real LLM is available or the
    extraction call fails.

    "core"/"vcpu" matter as much as "cpu": the old pattern was `N cpu` only,
    so "find a 32 core box" matched nothing and silently fell back to the
    8-core default - the engineer asks for 32 and gets sized for 8, with no
    indication anywhere that the number had been replaced.
    """
    cpu = _first_number(query, r"(\d+(?:\.\d+)?)\s*(?:x\s*)?(?:core|cores|cpu|cpus|vcpu|vcpus)\b")
    mem = _first_number(query, r"(\d+(?:\.\d+)?)\s*(?:gb|gib|gig|gigs)\s*(?:of\s*)?(?:ram|memory)\b")
    if mem is None:
        # "64GB RAM" is the common phrasing, but "16 vCPU 64GB box" is too -
        # a bare GB figure that is not storage is memory.
        mem = _first_number(query, r"(\d+(?:\.\d+)?)\s*(?:gb|gib)\b(?!\s*(?:storage|disk|ssd))")
    storage_tb = _first_number(query, r"(\d+(?:\.\d+)?)\s*tb\b")
    storage_gb = _first_number(query, r"(\d+(?:\.\d+)?)\s*(?:gb|gib)\s*(?:of\s*)?(?:storage|disk|ssd)\b")
    storage = (storage_tb * 1000) if storage_tb else storage_gb

    stated = {"cpu_cores": cpu, "memory_gb": mem, "storage_gb": storage}
    assumed = [name for name, value in stated.items() if value is None]
    resolved = {name: (value if value is not None else _CAPACITY_DEFAULTS[name]) for name, value in stated.items()}

    req = HostingRequirement(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=resolved["cpu_cores"], memory_gb=resolved["memory_gb"],
        storage_gb=resolved["storage_gb"],
        growth_percent=0.0, availability_tier="Tier-2", data_classification="Internal",
        criticality="Medium",
    )
    return {
        "capacity_requirements": {
            **resolved,
            "extraction_method": "regex",
            # Which figures came from the engineer and which the platform
            # supplied. A reviewer approving 8 cores should be able to see
            # that nobody actually asked for 8.
            "assumed_defaults": assumed,
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
        query = effective_query(state)
        cluster_match = _CLUSTER_CODE_RE.search(query.upper())
        horizon_match = re.search(r"(\d+)\s*day", query.lower())
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


def _rejection_rule_evidence(state: InfrastructureRecommendationState) -> list[dict]:
    """Rule verdicts for "why was <cluster> rejected for <app>", or [].

    This exists because that question was being answered from the vector index.
    A rejection is not a fact about documents - it is the output of
    rules.eligibility, which returns a rule id, a pass/fail and a written
    reason for every rule. Answering from retrieved prose meant the narration
    could disagree with the engine that actually rejected the cluster, and
    nothing anywhere would flag the contradiction.

    So the rules are re-evaluated for exactly the pair named in the question and
    handed to the model as evidence. The model still writes the sentence; it no
    longer decides the verdict, and every claim it can make is now traceable to
    a rule id. That keeps this on the same footing as the rest of the platform,
    where Python decides and the model narrates.

    Returns [] whenever the question is not of this shape, either name is
    missing, or either name does not resolve - in which case retrieval proceeds
    exactly as before. A wrong guess here would be worse than no answer.
    """
    query = effective_query(state)
    if not _REJECTION_QUESTION_RE.search(query):
        return []

    cluster_match = _CLUSTER_CODE_RE.search(query)
    app_match = _APP_CODE_RE.search(query.upper())
    if not cluster_match or not app_match:
        return []

    try:
        app = application_repository.get_by_code(app_match.group(0))
        cluster = cluster_repository.get_by_code(cluster_match.group(0).lower())
        if app is None or cluster is None:
            return []
        requirement = placement.requirement_for_application(app)
        candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
    except Exception as exc:  # noqa: BLE001
        # Same posture as retrieval below: evidence is best-effort, and a
        # failure here degrades to the old behaviour rather than failing the
        # whole investigation.
        logger.warning("graph.rejection_rule_evidence_failed", error=str(exc))
        return []

    results = list(candidate.rule_results or [])
    # Failures first. They are the answer to "why", and the model reads this
    # list in order; burying two failures under nine passes invites a summary
    # that leads with what went right.
    results.sort(key=lambda r: bool(r.get("passed")))

    status = candidate.eligibility_status
    docs = [
        {
            "text": (
                f"Eligibility rule {r.get('rule_id')} ({r.get('name')}) "
                f"{'PASSED' if r.get('passed') else 'FAILED'} when {app.ApplicationCode} was "
                f"evaluated against cluster {cluster.ClusterCode}: {r.get('reason')}"
            ),
            "score": 1.0,
            "entity_type": "eligibility_rule",
        }
        for r in results
    ]
    docs.insert(
        0,
        {
            "text": (
                f"Cluster {cluster.ClusterCode} is {status} for application "
                f"{app.ApplicationCode}. This verdict comes from the deterministic "
                f"eligibility engine, not from retrieved documents. "
                f"{sum(1 for r in results if not r.get('passed'))} of {len(results)} rules failed."
            ),
            "score": 1.0,
            "entity_type": "eligibility_verdict",
        },
    )
    logger.info(
        "graph.rejection_rule_evidence",
        application=app.ApplicationCode,
        cluster=cluster.ClusterCode,
        status=status,
        failed_rules=sum(1 for r in results if not r.get("passed")),
    )
    return docs


def retrieve_related_context(state: InfrastructureRecommendationState) -> dict:
    # The previous investigation's own evidence, when this turn is a follow-up.
    # It goes first and is never displaced by a vector hit: "why was that
    # rejected?" is a question about a specific run, and a similarity search
    # over the whole estate has no way of knowing which one. Without this the
    # Question path answered "I don't have enough grounded information" - true,
    # and useless, when the answer was sitting in the previous turn.
    prior_docs = list(state.get("prior_context_docs") or [])

    # Rule verdicts first, ahead of both prior context and the vector hits. A
    # "why was X rejected" question is answered by rules.eligibility, and a
    # similarity search over the estate cannot produce that verdict - it can
    # only produce prose that resembles it.
    rule_docs = _rejection_rule_evidence(state)

    # Retrieval is optional grounding for narration, never a hard dependency -
    # a runtime embedding failure (e.g. a real API embedder going down after a
    # successful startup probe) degrades to no retrieved context rather than
    # failing the whole investigation.
    try:
        store = get_vector_store()
        results = store.search(effective_query(state), top_k=6)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph.retrieve_related_context_failed", error=str(exc))
        return {"retrieved_context": rule_docs + prior_docs}
    retrieved = [
        {"text": r.document.text, "score": r.score, "entity_type": r.document.entity_type}
        for r in results
    ]
    return {"retrieved_context": rule_docs + prior_docs + retrieved}


# =============================================================================
# 13. generate_recommendation_explanations
# =============================================================================


def generate_recommendation_explanations(state: InfrastructureRecommendationState) -> dict:
    itype = state["investigation_type"]
    # Two roles in one node: the HOSTING/CAPACITY and RIGHT_SIZING branches
    # narrate a decision Python already made, while the QUESTION branch answers
    # from retrieved evidence. Those reward different models - one readable
    # prose, the other a willingness to say "I do not know" - so resolving once
    # at the top would collapse a real distinction the admin screen exposes.
    app_code = (state.get("application_requirements") or {}).get("application_code")

    if itype in (InvestigationType.HOSTING, InvestigationType.CAPACITY):
        llm = get_chat_model_for_role("narration")
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
        llm = get_chat_model_for_role("narration")
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
            answer = answer_grounded_question(
                get_chat_model_for_role("grounded_qa"), state["user_query"], state.get("retrieved_context", [])
            )
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


def build_review_payload(state: InfrastructureRecommendationState | dict) -> dict:
    """The shortlist as a reviewer sees it.

    Shared with app.graph.graph, which rebuilds this payload when a follow-up
    asks for a still-open shortlist again. Two constructions of this shape
    would drift, and the half that drifted would be the one the engineer
    actually clicks Approve on.
    """
    top = (state.get("candidate_scores") or [])[: get_settings().policy.top_clusters]
    return {
        "investigation_id": state.get("investigation_id"),
        "investigation_type": state.get("investigation_type"),
        # A reviewer is choosing *one* placement, not rubber-stamping a list.
        # Each option carries the capacity figures the decision actually turns
        # on - how big the cluster/host is, how much is already committed, and
        # what is left - because "score 99.83" is a summary of those numbers,
        # not a substitute for seeing them.
        "options": [_review_option(c) for c in top],
        # Retained for older callers; options above is the richer form.
        "top_candidates": [c.get("cluster_code") for c in top],
        "top_hosts_by_cluster": {
            c.get("cluster_code"): [n.get("host_name") for n in (c.get("top_nodes") or [])]
            for c in top
        },
        "cluster_eligibility": {
            c.get("cluster_code"): c.get("eligibility_status") for c in top
        },
        "confidence": state.get("confidence"),
        "message": "Choose one cluster and host, then approve - or reject the shortlist.",
        # What to offer when the shortlist is not good enough. This is a search:
        # when the results are usable the engineer picks one and leaves, and
        # next_steps reports sufficient=True so the UI stays quiet. When they
        # are not, the useful move is a choice - see more, or ask for less -
        # rather than an explanation of every rule that failed.
        "next_steps": refinement.next_steps(
            state.get("candidate_scores") or [],
            state.get("requirement") or {},
            shown=len(top),
        ),
    }


def human_review_interrupt(state: InfrastructureRecommendationState) -> dict:
    from langgraph.types import interrupt

    summary = build_review_payload(state)
    investigation_repository.update_status(state["investigation_id"], "AwaitingReview")
    resumed = interrupt(summary)
    return {
        "decision": resumed.get("decision"),
        "reviewer_employee_id": resumed.get("reviewer_employee_id"),
        "review_comments": resumed.get("comments"),
        # Which option the reviewer actually picked. persist_recommendations
        # marks that row Approved and the rest NotSelected, so the stored
        # outcome records a choice rather than a blanket yes.
        "selected_cluster_code": resumed.get("selected_cluster_code"),
        "selected_host_name": resumed.get("selected_host_name"),
    }


def _capacity_view(snapshot: dict | None) -> dict | None:
    """total / used / free for each resource, straight off an already-computed
    snapshot. No arithmetic happens here - ``available`` is what the capacity
    engine calculated, not ``total - used`` re-derived in a display helper,
    which would quietly diverge the moment reservation handling changed.
    """
    if not snapshot:
        return None
    return {
        "cpu_cores": {
            "total": snapshot.get("effective_cpu_cores"),
            "used": snapshot.get("consumed_cpu_cores"),
            "free": snapshot.get("available_cpu_cores"),
            "used_percent": snapshot.get("current_cpu_utilization_percent"),
        },
        "memory_gb": {
            "total": snapshot.get("effective_memory_gb"),
            "used": snapshot.get("consumed_memory_gb"),
            "free": snapshot.get("available_memory_gb"),
            "used_percent": snapshot.get("current_memory_utilization_percent"),
        },
        "storage_gb": {
            "total": snapshot.get("effective_storage_gb"),
            "used": snapshot.get("consumed_storage_gb"),
            "free": snapshot.get("available_storage_gb"),
            "used_percent": snapshot.get("current_storage_utilization_percent"),
        },
    }


def _review_option(candidate: dict) -> dict:
    projected = candidate.get("projected") or {}
    return {
        "cluster_code": candidate.get("cluster_code"),
        "cluster_id": candidate.get("cluster_id"),
        "eligibility_status": candidate.get("eligibility_status"),
        "overall_score": candidate.get("overall_score"),
        "projected_headroom_percent": projected.get("projected_headroom_percent"),
        "capacity": _capacity_view(candidate.get("snapshot")),
        "hosts": [
            {
                "host_name": n.get("host_name"),
                "node_id": n.get("node_id"),
                "overall_score": n.get("overall_score"),
                "projected_headroom_percent": (n.get("projected") or {}).get("projected_headroom_percent"),
                "capacity": _capacity_view(n.get("snapshot")),
            }
            for n in (candidate.get("top_nodes") or [])
        ],
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

    llm = get_chat_model_for_role("reporting")
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

    # What the reviewer actually chose. A shortlist of three that all come back
    # "PendingReview" records no decision at all - the point of the review step
    # is that one of them was picked.
    selected_cluster = state.get("selected_cluster_code")
    selected_host = state.get("selected_host_name")
    approved = state.get("decision") == "Approve"

    def status_for(cluster_code: str, host_name: str | None) -> str:
        """'Superseded' rather than a new 'NotSelected' status: the schema's
        CHECK constraint already carries that vocabulary, and it says exactly
        the right thing - these options were displaced by the one chosen, not
        rejected on their merits.
        """
        if not approved:
            return "Rejected" if state.get("decision") == "Reject" else "PendingReview"
        if not selected_cluster:
            # Approved without naming an option - the pre-selection behaviour.
            # Everything stays pending rather than silently approving all three.
            return "PendingReview"
        if cluster_code != selected_cluster:
            return "Superseded"
        if host_name is None:
            return "Approved"
        return "Approved" if (selected_host is None or host_name == selected_host) else "Superseded"

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
            "Status": status_for(c["cluster_code"], None),
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
                        "Status": status_for(c["cluster_code"], n["host_name"]),
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
