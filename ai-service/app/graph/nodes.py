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
    answer_rejection_question,
    explain_candidate,
    explain_cluster_right_sizing,
    extract_capacity_requirement,
    generate_final_report as generate_final_report_chain,
)
# get_chat_model is deliberately NOT imported. Every call site here uses
# get_chat_model_for_role, and keeping the unused name in this namespace is
# what allowed tests to monkeypatch it successfully while binding nothing -
# monkeypatch.setattr only objects to an attribute that does not exist.
from app.agents.llm_factory import get_chat_model_for_role
from app.graph import scope
from app.agents.mock_llm import MockChatModel
from app.forecasting.engine import forecast_cluster
from app.agents import query_capability
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
#: Wordings that mean "this is a question about the estate", whatever else the
#: sentence contains. Retrospective ("why was"), comparative, or a request for a
#: document rather than a placement.
_QUESTION_KEYWORDS = ["why was", "why is", "compare", "show clusters", "generate a"]

#: Question-ish, but WEAKER THAN AN EXPLICIT PROVISIONING REQUEST.
#:
#: "at least" reads as a filter - "which clusters have at least 20% headroom" -
#: and it is also how people state a floor when they are asking for capacity:
#: "I need at least 32 cores and 128 GB RAM". Matched at the same strength as
#: "why was", it sent that second sentence to grounded Q&A, which answers a
#: placement request with prose and no shortlist.
#:
#: So it is tested AFTER the capacity check rather than before it. A sentence
#: carrying both a resource quantity and a provisioning verb is someone asking
#: for infrastructure; one carrying "at least" and neither is asking a question.
_SOFT_QUESTION_KEYWORDS = ["at least"]

#  "report" USED TO BE IN THE HARD LIST, AS A BARE SUBSTRING, AND IT MATCHED
#  "reporting".
#
#  So "where can I host a Tier-2 reporting service needing 10 cores, 40 GB RAM
#  and 600 GB storage" classified as QUESTION, went to grounded Q&A, and came
#  back with no shortlist at all - reproducibly, three runs out of three, while
#  "batch analytics workload" and "internal web app" both returned twelve
#  options. Every application named "<something> reporting service" - which in
#  a bank is a great many of them - had been unplaceable since the first
#  commit.
#
#  It is gone rather than tightened: "generate a" above already catches the
#  case it was for ("generate a report"), and no other phrasing of "produce a
#  document" reaches this classifier without it.

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
#: "application" is listed in its own right rather than left to a wildcard on
#: "app". A suffix wildcard also matches "apply" and "apparently", which are
#: ordinary words here - see query_capability.mentions_any.
_INFRA_INTENT_WORDS = ("host", "cluster", "capacity", "node", "server", "deploy",
                       "place", "workload", "app", "application")


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

    has_app_code = bool(_APP_CODE_RE.search(text.upper()))
    has_quantity = bool(_RESOURCE_QUANTITY_RE.search(lower))

    # Asked for something the CMDB does not record, or asked to place something
    # without saying what. Checked before the length heuristic below because it
    # is a fact rather than a guess, and because the heuristic misses exactly
    # the queries this catches.
    #
    # "give me best dc for java apps" reached the full graph and returned a
    # report that read as a retrieval miss - "the evidence does not include any
    # record of which clusters host Java applications" - for something no amount
    # of retrieval could ever produce: there is no runtime-language column. It
    # then listed five unrelated applications, which is the retriever's top-k
    # showing through a refusal.
    #
    # The guard below SHOULD have caught it and did not, by one word: it fires
    # at six and that query is seven. Same query without "java" was handled
    # correctly. See app.agents.query_capability for why the length test was
    # the wrong instrument.
    capability = query_capability.capability_reply(
        text, has_app_code=has_app_code, has_quantity=has_quantity
    )
    if capability is not None:
        return capability

    # Datacentre vocabulary belongs in the infra check too - "dc" is how people
    # write it and it appeared in none of _INFRA_INTENT_WORDS, so the example
    # query only registered as infrastructure-shaped at all because "apps"
    # contains "app".
    # WORD matching, not substring. `"app" in "what happened"` is True - and
    # that single substring match refused every incident lookup in the golden
    # set, four cases, for containing an ordinary English word.
    looks_infra = query_capability.mentions_any(
        text, _INFRA_INTENT_WORDS + query_capability.DATACENTRE_WORDS
    )
    # Infrastructure-shaped, but with nothing to compute against. Short and
    # specific beats a full investigation that can only report emptiness.
    #
    # The length test survives HERE, narrowed to what it is actually good at:
    # a brief infrastructure fragment with no placement verb ("capacity?",
    # "cluster for us"). It is not load-bearing for placement any more - that is
    # decided above by intent - so a long placement request no longer escapes.
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
    #
    # ABOVE the soft question words on purpose: both signals present means
    # somebody is asking for infrastructure and happens to have used the word
    # "at least" to state a floor.
    if _RESOURCE_QUANTITY_RE.search(lower) and any(w in lower for w in _PROVISIONING_WORDS):
        return InvestigationType.CAPACITY
    if any(k in lower for k in _SOFT_QUESTION_KEYWORDS):
        return InvestigationType.QUESTION
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
    # This used to also call the "planning" role (parse_investigation_plan)
    # to build parsed_intent/investigation_plan - averaging 8.2s and up to 21s
    # over 48 measured calls, on every single investigation. Removed after
    # tracing every reader of that value: router.route_after_plan only reads
    # investigation_type (computed above, deterministically, before that call
    # ever ran); nothing else in app.graph, the API response types, or the UI
    # reads parsed_intent or investigation_plan's content, and it was never
    # persisted. A full LLM call computing a value nothing downstream reads is
    # not a plan the platform acts on, it is latency with a docstring.
    return {
        "investigation_type": investigation_type,
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
            #  THE TWO SECURITY-RELEVANT FALLBACKS FAIL CLOSED. See
            #  _SECURITY_COERCIONS below for why they used to fail open.
            environment, env_ok = _coerce_enum(extracted.environment, Environment, Environment.PRODUCTION)
            platform, platform_ok = _coerce_enum(extracted.platform, TechnologyPlatform, TechnologyPlatform.KUBERNETES)
            tier, tier_ok = _coerce_enum(extracted.availability_tier, AvailabilityTier, AvailabilityTier.TIER_1)
            classification, class_ok = _coerce_enum(
                extracted.data_classification, DataClassification, DataClassification.RESTRICTED
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
            #  ASK, having already failed closed. The substituted value is the
            #  strictest one, so an engineer who never answers is protected by
            #  the narrow shortlist rather than by a promise to follow up.
            clarification = _clarification_for(coerced, state.get("user_query") or "")
            # A dimension the engineer never mentioned comes back as null, and
            # null is resolved HERE rather than being forbidden in the contract.
            # The regex path has always defaulted-and-declared; this one used to
            # reject the model's honest null instead, pay for a repair retry, and
            # fall through to regex - which then applied these very same defaults.
            # Same numbers, two wasted model calls and 68 seconds later.
            stated = {
                "cpu_cores": extracted.cpu_cores,
                "memory_gb": extracted.memory_gb,
                "storage_gb": extracted.storage_gb,
            }
            assumed = [name for name, value in stated.items() if value is None]
            resolved = {
                name: (value if value is not None else _CAPACITY_DEFAULTS[name])
                for name, value in stated.items()
            }
            if assumed:
                logger.info("graph.capacity_extraction_defaulted", fields=assumed, method="llm")

            req = HostingRequirement(
                environment=environment, platform=platform, os_requirement="Any",
                cpu_cores=resolved["cpu_cores"], memory_gb=resolved["memory_gb"],
                storage_gb=resolved["storage_gb"],
                growth_percent=extracted.expected_growth_percent or 0.0, availability_tier=tier,
                data_classification=classification, preferred_location=extracted.preferred_location,
                criticality="Medium",
            )
            return {
                "clarification_prompt": clarification,
                "capacity_requirements": {
                    **resolved,
                    "extraction_method": "llm",
                    "coerced_fields": coerced,
                    # Same key the regex path emits, for the same reason: a
                    # reviewer must be able to tell a figure the engineer gave
                    # from one the platform supplied on their behalf.
                    "assumed_defaults": assumed,
                },
                "requirement": to_jsonable(req),
            }
        return _capacity_requirement_from_regex(query)

    return {}


#: The coerced fields whose fallback CHANGES WHO MAY HOST THE WORKLOAD, with
#: the question to ask and the options to offer. environment and platform are
#: deliberately absent: Production is the strict end of RULE-001, and a wrong
#: platform narrows to a family rather than widening the estate.
_SECURITY_COERCIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "data_classification": (
        "I could not tell the data classification, so I assumed Restricted - "
        "the strictest - and searched only clusters certified to hold it. "
        "Which is it really?",
        ("Restricted", "Confidential", "Internal", "Public"),
    ),
    "availability_tier": (
        "I could not tell the availability tier, so I assumed Tier-1 - the "
        "strictest. Which is it really?",
        ("Tier-1", "Tier-2", "Tier-3"),
    ),
}


def _clarification_for(coerced: list[str], user_query: str) -> dict | None:
    """The question to put back to the engineer when a value that decides
    ELIGIBILITY had to be invented, or None when nothing security-relevant was.

    WHY THIS EXISTS, AND WHY IT IS NOT JUST A BETTER DEFAULT.

    data_classification used to fall back to Internal, which is level 1 of 4 in
    CLASSIFICATION_LEVEL, and RULE-005 admits a cluster when
    cluster_level >= data_level. So an unparseable classification did not merely
    guess - it guessed in the direction that WIDENS the estate. A workload whose
    real classification was Restricted was offered clusters certified only for
    Internal, and the control whose entire purpose is preventing that had failed
    open. availability_tier -> Tier-2 had the same direction against RULE-004,
    where rank(candidate) <= rank(required).

    Praveen's instruction was: ask, and fall back to the most restrictive value
    if nobody answers. Both halves are here and the ORDER matters. The strict
    value is applied FIRST, so the investigation that runs is already safe; the
    question is then asked about a shortlist that has already been narrowed.
    An engineer who ignores the prompt is protected by what was searched, not by
    a promise to come back to it.

    That inverts the failure mode rather than removing it. A Restricted-by-
    default search will often return nothing on this estate, and "no clusters
    qualify" is a worse answer than a wrong one is dangerous - which is exactly
    why the prompt says WHICH assumption produced the empty result, instead of
    leaving the engineer to conclude the platform is broken.

    ``user_query`` travels with the prompt so the caller can compose a complete
    follow-up rather than a bare word. A button that sends "Restricted" on its
    own would be resolved against the previous turn by the conversation layer,
    which is a different mechanism owned elsewhere; sending the original
    sentence plus the stated classification needs nothing from it.
    """
    fields = [f for f in coerced if f in _SECURITY_COERCIONS]
    if not fields:
        return None
    field = fields[0]
    question, options = _SECURITY_COERCIONS[field]
    return {
        "field": field,
        "question": question,
        "assumed": (
            DataClassification.RESTRICTED if field == "data_classification"
            else AvailabilityTier.TIER_1
        ),
        "options": [
            {"label": opt, "query": f"{user_query.strip()} The {field.replace('_', ' ')} is {opt}."}
            for opt in options
        ],
        #  Every field that was invented, not just the one being asked about,
        #  so the reviewer sees the whole of what the platform supplied.
        "also_assumed": [f for f in coerced if f != field],
    }


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
        # The only drop site without the graph state in scope: this helper is
        # called with just the query. Recorded with what it has rather than not
        # at all - a failure with no investigation id is not less real than one
        # with, and this is the site that cost 68 seconds a call for hours.
        _dropped("graph.load_application_requirements.llm_extraction_failed", {}, exc)
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
        clusters = placement.discover_candidate_clusters(
            requirement, exclude_data_centers=state.get("exclude_data_centers"),
        )
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
    # The exclusion has to be applied HERE as well as in discovery. These two
    # nodes call placement independently, and a filter applied to only one of
    # them produces a shortlist that disagrees with its own candidate list.
    ranked = placement.find_and_score_candidates(
        requirement, exclude_data_centers=state.get("exclude_data_centers"),
    )
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
        #  The whole review deck, not just the first page of it. An engineer
        #  paging to options 4-6 must be able to pick a host there too, and a
        #  cluster that arrives without hosts is indistinguishable from one
        #  that has none. Costs about a second now that the drill runs in
        #  parallel - see node_placement.attach_top_nodes.
        node_placement.attach_top_nodes(
            requirement, scored, top_clusters=get_settings().policy.review_options
        )
    except Exception as exc:  # noqa: BLE001
        # Node drill-down is an enrichment, not a precondition: a failure here
        # degrades to cluster-only recommendations rather than losing the
        # whole investigation.
        _dropped("graph.select_candidate_nodes_failed", state, exc)
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

    # ONLY the failures. They are the answer to "why was this rejected"; the
    # passes are not.
    #
    # This previously sent all ten rules with the failures sorted first, which
    # produced exactly the summary the feature was meant to replace: the model
    # was handed nine things that went right and dutifully narrated them. A
    # rejection is one or two sentences - which rule, and by how much - and the
    # rest is answerable by asking.
    failures = [r for r in (candidate.rule_results or []) if not r.get("passed")]
    results = failures

    status = candidate.eligibility_status
    docs = [
        {
            "text": (
                f"{cluster.ClusterCode} failed {r.get('rule_id')} ({r.get('name')}): {r.get('reason')}"
            ),
            "score": 1.0,
            "entity_type": "eligibility_rule",
            # Carried structurally so the follow-up options can be built from
            # the rule that actually failed, without parsing the sentence above.
            "rule_id": r.get("rule_id"),
        }
        for r in results
    ]
    if status == "Eligible":
        # Nothing failed. Say so in one line rather than listing ten passes.
        docs = [
            {
                "text": f"{cluster.ClusterCode} is recommended for {app.ApplicationCode}. No rule failed.",
                "score": 1.0,
                "entity_type": "eligibility_verdict",
            }
        ]
    else:
        docs.insert(
            0,
            {
                "text": (
                    f"{cluster.ClusterCode} was rejected for {app.ApplicationCode}. "
                    f"{len(results)} rule(s) failed, listed below. This verdict is from the "
                    f"eligibility engine, not from retrieved documents."
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
        failed_rules=len(results),
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
        _dropped("graph.retrieve_related_context_failed", state, exc)
        return {"retrieved_context": rule_docs + prior_docs}
    retrieved = [
        {"text": r.document.text, "score": r.score, "entity_type": r.document.entity_type}
        for r in results
    ]
    return {"retrieved_context": rule_docs + prior_docs + retrieved}


# =============================================================================
# 13. generate_recommendation_explanations
# =============================================================================


#: Independent narrations run together rather than one after another.
#:
#: Measured on production investigation 16, three candidate explanations:
#:     78 at 03:24:51   9.9s
#:     79 at 03:25:01  19.7s
#:     80 at 03:25:20  16.7s
#: Strictly sequential, 46.3 seconds, for three calls that do not read each
#: other's output. A whole investigation was 98 seconds end to end and most of
#: it was this shape - four model calls waiting on a reasoning provider in a
#: row. Not a retry, not a timeout, not a slow query: addition.
#:
#: THREADS RATHER THAN ASYNC, deliberately. app.agents.structured has
#: arun_structured and its docstring describes exactly this problem, but the
#: graph nodes are synchronous and LangGraph calls them synchronously; making
#: one node async would push the change through the whole graph. These calls
#: block on an HTTP response, so a thread pool gets the same win at the size of
#: change the fix deserves.
#:
#: Safe against the audit writes each call makes: app.repositories.base builds
#: its engine with NullPool, so every connect() is its own connection rather
#: than a shared session, and SQLAlchemy engines are thread-safe. Checked before
#: writing this, not assumed.
#:
#: ORDER IS PRESERVED. The list is ranked, and a narration list whose order
#: disagrees with the ranking would put the second-best cluster first in the
#: report. executor.map preserves input order regardless of completion order.
#:
#: Bounded at 4. Beyond the three or five items these paths produce it would
#: only add provider concurrency nobody asked for.
_NARRATION_WORKERS = 4


def _narrate_all(items: list, narrate, on_error) -> list[dict]:
    """Run one narration per item concurrently, in order, skipping failures.

    ``narrate`` returns a pydantic model; ``on_error`` is called with (item,
    exception) so each branch keeps the log line it had. A failure drops that
    item and no other - same behaviour as the loop this replaces, where one
    cluster failing to narrate never cost the others.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not items:
        return []

    def one(item):
        try:
            return narrate(item).model_dump()
        except Exception as exc:  # noqa: BLE001 - a narration failure must not break the graph
            on_error(item, exc)
            return None

    if len(items) == 1:
        result = one(items[0])
        return [result] if result is not None else []

    with ThreadPoolExecutor(max_workers=min(_NARRATION_WORKERS, len(items))) as pool:
        return [r for r in pool.map(one, items) if r is not None]


# =============================================================================
# Failures that used to vanish
# =============================================================================
#: Seven except branches in this module log a warning and continue. What they DO
#: is right - a narration failure must not fail an investigation whose numbers
#: are already computed - but nothing counted them, nothing stored them, and "how
#: often does narration fail" was answerable only by grepping container logs on a
#: production box.
#:
#: "Best effort" describes what the code should do about a failure. It does not
#: decide whether anyone should be told.
#:
#: This is the fourth failure of that shape found in a day: Grafana serving an
#: unauthenticated dashboard behind a 200, containers stuck on old images while
#: the site returned 200, an empty review screen on a 200, and this.


def _dropped(site: str, state: dict, exc: Exception, **extra) -> None:
    """Log a dropped failure AND enqueue it. Never raises.

    The logger call is kept exactly as it was, event name included, so existing
    log searches and anything watching those strings keep working. The enqueue is
    additive.

    Wrapped in its own try/except because observability must not be able to break
    the thing it is watching - and because this function is called from inside
    handlers that are themselves already dealing with a failure.
    """
    logger.warning(site, error=str(exc), **extra)
    try:
        from app.repositories import remediation_repository

        remediation_repository.record(
            site=site,
            source="python",
            investigation_id=state.get("investigation_id") if isinstance(state, dict) else None,
            conversation_id=state.get("conversation_id") if isinstance(state, dict) else None,
            detail=f"{type(exc).__name__}: {exc}",
            **extra,
        )
    except Exception:  # noqa: BLE001
        pass


#: SAYS "FAILED" FIRST, EVERY TIME.
#:
#: Praveen was explicit, and the reason is that the alternatives read as an
#: answer: "I found limited information" and "based on available evidence" are
#: what a reader acts on rather than questions. A degraded answer that does not
#: announce itself is worse than an error, because an error stops somebody.
#:
#: Then it says what SURVIVES the failure. The scores and capacity figures come
#: from the deterministic engines and are unaffected by any narration failure, so
#: withholding them because the prose broke would throw away the correct part of
#: the answer. State the failure, then state what stands - never the other way
#: round.
def _narration_failed_notice(subject: str) -> str:
    return (
        f"The written explanation for {subject} FAILED. "
        "The scores and capacity numbers are correct - those come from the engine, "
        "not the model."
    )


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
        explanations = _narrate_all(
            top,
            lambda c: explain_candidate(llm, CandidateScore.model_validate(c), app_code),
            lambda c, exc: _dropped(
                "graph.explain_candidate_failed", state, exc, cluster=c.get("cluster_code")
            ),
        )
        # A CANDIDATE THAT LOST ITS PROSE SAYS SO, rather than appearing without
        # any. _narrate_all returns only the successes, so a failed narration
        # used to leave a shortlisted cluster silently unexplained - the reader
        # saw two explanations for three options and had no way to tell whether
        # the third was unremarkable or broken.
        #
        # The notice carries the engine's own eligibility and score, because
        # those are unaffected by a narration failure and withholding them would
        # discard the correct part of the answer.
        explained = {e.get("cluster_code") for e in explanations}
        for c in top:
            code = c.get("cluster_code")
            if code in explained:
                continue
            explanations.append({
                "cluster_code": code,
                "eligibility_status": c.get("eligibility_status"),
                "overall_score": c.get("overall_score"),
                "summary": _narration_failed_notice(code or "this cluster"),
                "key_strengths": [],
                "key_risks": [],
                "narration_failed": True,
            })
        return {"recommendation_explanations": explanations}

    if itype == InvestigationType.RIGHT_SIZING:
        results = state.get("capacity_calculations", {}).get("right_sizing", [])
        llm = get_chat_model_for_role("narration")
        #  RANKED, NOT SLICED. This was
        #      [r for r in results if r["classification"] != "Healthy"][:5]
        #  which took the first five non-Healthy rows in whatever order
        #  cluster_repository.list_all returned - so the clusters an engineer
        #  read about were an artefact of SQL, not the ones worth acting on.
        #
        #  The filter was wrong too, in a way the slice hid. "not Healthy" is
        #  not the same as actionable: an Overprovisioned cluster whose node
        #  count is already floored by N-1 tolerance has node_delta 0 and
        #  nothing to do. Those used to be eligible for all five slots.
        ranked = rightsizing.rank_right_sizing(results)
        actionable = [r for r in ranked if r.get("node_delta")]
        flagged = actionable[:5]
        from app.models.rightsizing import ClusterRightSizingResult

        explanations = _narrate_all(
            flagged,
            lambda r: explain_cluster_right_sizing(llm, ClusterRightSizingResult.model_validate(r)),
            lambda r, exc: _dropped(
                "graph.explain_rightsizing_failed", state, exc,
                cluster=r.get("cluster_code") or r.get("cluster_or_application_code"),
            ),
        )
        return {"recommendation_explanations": explanations}

    if itype == InvestigationType.QUESTION:
        context = state.get("retrieved_context", [])
        # A rejection question gets its own, shorter path. The general grounded-QA
        # prompt has no length instruction, so it writes an essay - which is why
        # cutting the evidence from eleven documents to two did not fix the
        # complaint. Fewer documents just produced a long summary of two
        # documents; the instruction was the half that mattered.
        rule_ids = [d.get("rule_id") for d in context if d.get("entity_type") == "eligibility_rule"]
        rule_ids = [r for r in rule_ids if r]
        try:
            if rule_ids:
                from app.services.refinement import rejection_follow_ups

                follow_ups = rejection_follow_ups(rule_ids)
                answer = answer_rejection_question(
                    get_chat_model_for_role("grounded_qa"),
                    state["user_query"],
                    [d for d in context if d.get("entity_type", "").startswith("eligibility")],
                    follow_ups,
                )
                payload = answer.model_dump()
                # Offered as data the UI can render as buttons, not only as prose
                # the model was asked to write. If the model ignores the
                # instruction the options are still there.
                payload["follow_ups"] = follow_ups
                return {"recommendation_explanations": [payload]}

            answer = answer_grounded_question(
                get_chat_model_for_role("grounded_qa"), state["user_query"], context
            )
            return {"recommendation_explanations": [answer.model_dump()]}
        except Exception as exc:  # noqa: BLE001
            _dropped("graph.grounded_qa_failed", state, exc)
            return {"recommendation_explanations": []}

    return {}


def route_after_decision(state: InfrastructureRecommendationState) -> str:
    """Approve writes a report. Reject asks a question.

    This edge used to be unconditional - every decision, either way, ran
    generate_final_report. So rejecting a placement produced an executive summary
    of the thing you had just declined, which is the one document nobody wants at
    that moment.
    """
    #  RequestMoreAnalysis - the "Next choices" button - lands here too. It is
    #  the reviewer declining this shortlist without condemning it, and what
    #  they need next is the same question a rejection gets ("what was wrong
    #  with it?") rather than an executive summary of options they just
    #  skipped. It used to fall through to the report because this compared
    #  against one literal, which meant the one decision in the enum that
    #  nothing sent was also the one that behaved wrongly if anything did.
    return (
        "ask_rejection_reason"
        if state.get("decision") in ("Reject", "RequestMoreAnalysis")
        else "generate_final_report"
    )


def ask_rejection_reason(state: InfrastructureRecommendationState) -> dict:
    """Put the question back to the reviewer instead of narrating at them.

    A human rejecting a candidate the engine scored as eligible knows something
    the engine does not. Guessing which thing - and silently re-ranking on the
    guess - would be the same error as the summary, one step further on: it
    replaces a document nobody asked for with a search nobody asked for.

    No model is called here. The options come from the candidate's own figures.
    """
    from app.services.refinement import rejection_reasons

    # candidate_scores, not "candidates" - there is no such key, and reading it
    # produced an empty option list and a prompt that asked nothing. Silent,
    # because an empty list is a valid shape.
    candidates = state.get("candidate_scores") or state.get("eligible_candidates") or []
    selected = state.get("selected_cluster_code")
    candidate = None
    if selected:
        candidate = next((c for c in candidates if c.get("cluster_code") == selected), None)
    if candidate is None:
        candidate = next((c for c in candidates if c.get("eligibility_status") == "Eligible"), None)

    reasons = rejection_reasons(candidate, state.get("requirement"))
    code = (candidate or {}).get("cluster_code")
    #  Two decisions arrive here and they are not the same act. Rejecting says
    #  this cluster is wrong; "Next choices" says the reviewer has moved past
    #  this page without condemning anything on it. Asking "what was wrong with
    #  it?" about a shortlist nobody criticised puts words in their mouth, and
    #  the answer feeds the next search.
    skipped = state.get("decision") == "RequestMoreAnalysis"
    if skipped:
        question = (
            "Moving on from these. What would make the next set better? "
            "I will use that to narrow the search."
        )
    else:
        question = (
            f"Noted - {code} is out." if code else "Noted."
        ) + " What was wrong with it? I will use that to narrow the next search."
    return {
        "rejection_prompt": {
            "rejected_cluster": None if skipped else code,
            "question": question,
            "options": reasons,
        }
    }


# =============================================================================
# 14. assess_risk_and_confidence
# =============================================================================


def assess_risk_and_confidence(state: InfrastructureRecommendationState) -> dict:
    from app.config import get_settings

    itype = state["investigation_type"]
    if itype in (
        InvestigationType.QUESTION, InvestigationType.FORECAST,
        InvestigationType.RIGHT_SIZING, InvestigationType.CONSOLIDATION,
    ):
        # Informational only - no recommendation is being proposed for approval,
        # so human_review_required stays False. Confidence is a different claim
        # and used to be hard-coded "High" here regardless of what was found.
        #
        # Praveen was shown a report that said, in consecutive sentences, that it
        # had "no top candidates, no forecast results, and no capacity
        # calculations, so no infrastructure recommendation can be produced" and
        # that "overall evidence confidence is High". The platform asserting
        # certainty about a non-answer is the exact failure this system exists to
        # prevent, surfacing in the confidence field instead of in the prose.
        #
        # HIGH IS A MEASURED CLAIM AND MUST STAY ONE. Below, it means the top
        # eligible candidate scored at or above scoring.min_confident_score - a
        # number the engine computed. Spending the same word on "some documents
        # came back from a search" makes the vocabulary mean two different things
        # in one report, and the weaker meaning is the one a reader will not
        # notice.
        #
        # So an informational answer is capped at Medium: it was grounded in
        # something, and nothing has verified that the something answers the
        # question. With no evidence at all it is Low. Neither is derived from
        # the model's own opinion - GroundedAnswer carries a confidence field and
        # it is deliberately not read here, because a score the model assigns
        # itself is not evidence.
        #
        # RIGHT_SIZING and CONSOLIDATION belong here for the same reason as
        # QUESTION/FORECAST, not by extension of the default branch below: they
        # have no candidate/host to approve, only a set of clusters already
        # scored and narrated (capacity_calculations, recommendation_explanations).
        # Before this, both fell through to the default branch, which reads
        # candidate_scores - a key RIGHT_SIZING and CONSOLIDATION never populate -
        # found it empty, and routed to human_review_interrupt anyway. The
        # reviewer saw "choose one cluster and host, then approve" with nothing
        # to choose, and every actual finding (which clusters were flagged, why)
        # sat computed in state and was never reached, because
        # generate_final_report - the node that reads recommendation_explanations
        # and capacity_calculations - only runs on the branch this state never took.
        has_evidence = bool(
            state.get("retrieved_context")
            or state.get("forecast_results")
            or state.get("candidate_scores")
            or state.get("capacity_calculations")
        )
        return {
            "confidence": "Medium" if has_evidence else "Low",
            "human_review_required": False,
        }

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


def _review_message(next_steps: dict, dc_choice: dict | None) -> str:
    """What the search actually found, in one sentence.

    This was a constant - "Choose one cluster and host, then approve" - said
    identically whether three clusters qualified or none did, and identically
    on a first ask and on a re-scope that turned up nothing new.

    That constant is what made "what other options?" read as a non-answer.
    Live-verified on production: the engineer asked, the platform re-ran the
    whole estate, returned the same three clusters, and said the same sentence.
    Nothing in the reply distinguished "here are more" from "there are no
    more", so the only available reading was that the question had been
    ignored. It had not been - there genuinely were no others - but a shortlist
    that cannot say it is complete cannot say anything else either.

    So the message reports the state of the search. The engineer asking a
    second time now reads "these 3 are every cluster that qualifies", which is
    both the answer to their question and the reason there is nothing further.

    Derived from next_steps, which is already computed from the scored
    candidates - no new query, no second opinion about what is eligible, and
    nothing here that could disagree with the list rendered beside it.
    """
    total = next_steps.get("eligible_total") or 0
    shown = next_steps.get("shown") or 0
    more = next_steps.get("more_available") or 0

    if not total:
        #  Nothing qualified. Naming the constraint doing the damage beats
        #  "no results", which leaves the reader to guess whether they asked
        #  for too much or the estate is simply full.
        blocks = [
            f"{b.get('name', '').lower()} ({b.get('count')})"
            for b in (next_steps.get("blocking_reasons") or [])[:2]
            if b.get("name")
        ]
        head = "No cluster qualifies for this request"
        if blocks:
            head += " - blocked by " + ", ".join(blocks)
        if next_steps.get("size_options"):
            return head + ". Asking for less would open some up; see the options below."
        return head + "."

    if more:
        #  Wording tracks the buttons. They say "Select ... and proceed" and
        #  "Next choices"; prose telling the reader to approve or reject names
        #  two controls that are no longer on the screen.
        return (
            f"Showing the top {shown} of {total} clusters that qualify. "
            "Select one and proceed, or show more."
        )

    #  The exhausted case, and the one that used to be silent. Say the number
    #  out loud: it is the difference between a shortlist and a truncation.
    tail = ""
    if dc_choice and not dc_choice.get("has_genuine_alternative"):
        #  Every remaining site has been ruled out across this conversation.
        #  The honest end of a re-scope, and it must not look like a fresh
        #  shortlist arriving.
        tail = " Every other data centre has now been ruled out."
    elif dc_choice:
        remaining = [
            str(d.get("data_center"))
            for d in (dc_choice.get("available_data_centers") or [])
        ]
        if remaining:
            tail = " Remaining data centre(s): " + ", ".join(remaining) + "."
    plural = "" if total == 1 else "s"
    return (
        f"{total} cluster{plural} qualify" if total != 1 else "1 cluster qualifies"
    ) + f" - that is all of them. Select one and proceed, or ask for the next choices.{tail}"


def build_review_payload(state: InfrastructureRecommendationState | dict) -> dict:
    """The shortlist as a reviewer sees it.

    Shared with app.graph.graph, which rebuilds this payload when a follow-up
    asks for a still-open shortlist again. Two constructions of this shape
    would drift, and the half that drifted would be the one the engineer
    actually clicks Approve on.
    """
    policy = get_settings().policy
    candidates = state.get("candidate_scores") or []
    #  TWO WIDTHS, AND THEY MEAN DIFFERENT THINGS.
    #
    #  `page` is what the reviewer sees at once, and what this platform records
    #  as its recommendation - persist_recommendations uses the same bound.
    #  `deck` is what the panel is GIVEN, so "show the next 3" is a slice of a
    #  list already in the browser rather than a twenty-second re-run of the
    #  whole estate. Both come from one ranking; neither re-queries.
    page = candidates[: policy.top_clusters]
    deck = candidates[: policy.review_options]
    steps = refinement.next_steps(
        candidates,
        state.get("requirement") or {},
        shown=len(page),
    )
    dc_choice = (
        refinement.data_center_choice(
            state.get("candidate_scores") or [], state.get("exclude_data_centers")
        )
        if state.get("exclude_data_centers")
        else None
    )
    return {
        "investigation_id": state.get("investigation_id"),
        "investigation_type": state.get("investigation_type"),
        # A reviewer is choosing *one* placement, not rubber-stamping a list.
        # Each option carries the capacity figures the decision actually turns
        # on - how big the cluster/host is, how much is already committed, and
        # what is left - because "score 99.83" is a summary of those numbers,
        # not a substitute for seeing them.
        "options": [_review_option(c) for c in deck],
        # Retained for older callers; options above is the richer form.
        "top_candidates": [c.get("cluster_code") for c in page],
        "top_hosts_by_cluster": {
            c.get("cluster_code"): [n.get("host_name") for n in (c.get("top_nodes") or [])]
            for c in page
        },
        "cluster_eligibility": {
            c.get("cluster_code"): c.get("eligibility_status") for c in page
        },
        #: How many of `options` to show at once. The panel pages through the
        #: rest locally; sending it means the page size is a server policy
        #: rather than a number the browser invented.
        "page_size": policy.top_clusters,
        "confidence": state.get("confidence"),
        #  WHAT THE PLATFORM SUPPLIED ON THE ENGINEER'S BEHALF, in front of the
        #  person approving the placement.
        #
        #  coerced_fields was already computed and written into
        #  capacity_requirements - and capacity_requirements is referenced
        #  nowhere outside graph/state.py. Not persisted, not in this payload,
        #  not in the UI. So a data classification the model could not parse was
        #  replaced by the platform and the approver had no way to know, on a
        #  platform whose purpose is keeping classified workloads off
        #  under-certified infrastructure. The only trace was a log line on
        #  stdout, gone at the next deploy.
        "assumptions": {
            "coerced_fields": (state.get("capacity_requirements") or {}).get("coerced_fields") or [],
            "assumed_defaults": (state.get("capacity_requirements") or {}).get("assumed_defaults") or [],
            "data_classification": (state.get("requirement") or {}).get("data_classification"),
            "availability_tier": (state.get("requirement") or {}).get("availability_tier"),
        },
        "message": _review_message(steps, dc_choice),
        # What to offer when the shortlist is not good enough. This is a search:
        # when the results are usable the engineer picks one and leaves, and
        # next_steps reports sufficient=True so the UI stays quiet. When they
        # are not, the useful move is a choice - see more, or ask for less -
        # rather than an explanation of every rule that failed.
        "next_steps": steps,
        # Only present when this run actually excluded something - an
        # ordinary first ask has nothing to say about "what did we rule
        # out", and next_steps already stays quiet the same way when the
        # shortlist speaks for itself. When it IS present: on this estate a
        # Tier-1 workload typically has two DCs and three eligible clusters
        # total (verified against production, see 22492b3's deploy notes),
        # so after one exclusion this is usually a choice of one DC or
        # none - has_genuine_alternative:false is the common outcome here,
        # not an edge case, and a caller should say so plainly rather than
        # render an empty picker.
        "data_center_choice": dc_choice,
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
        # WHERE IT IS. A shortlist of three clusters is a choice about SITE as
        # much as capacity - the reviewer is deciding which data centre a Tier-1
        # workload lands in - and the payload carried the cluster code alone, so
        # that decision was being made from a name. atl-03 and den-03 are
        # different sites and the screen said nothing about it.
        #
        # Read from the candidate rather than looked up: placement sets it on
        # every CandidateScore, so this is the same value the exclusion logic
        # uses when the reviewer asks for a different DC.
        "data_center": candidate.get("data_center"),
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
    #  top_candidates IS THE RANKED SELECTION, whatever produced it.
    #
    #  It used to read candidate_scores unconditionally, which only the
    #  Hosting/Capacity path fills. A right-sizing investigation therefore
    #  handed the reporting model an empty list, and asked for "the best 3
    #  candidates" it answered that the evidence contained no precomputed
    #  top-candidates list - correctly, because this platform does not let a
    #  model rank anything. The refusal was the guard working; the empty list
    #  was the bug behind it.
    top_candidates = state.get("candidate_scores", [])[:5]
    right_sizing_summary = None
    if itype == InvestigationType.RIGHT_SIZING:
        all_results = (state.get("capacity_calculations") or {}).get("right_sizing") or []
        ranked = rightsizing.rank_right_sizing(all_results)
        actionable = [r for r in ranked if r.get("node_delta")]
        top_candidates = actionable[:5]
        #  THE DENOMINATORS, so a top-5 cannot pass for the whole picture.
        #
        #  Reductions outrank expansions in rank_right_sizing, so on an estate
        #  with plenty of savings a bare top 5 could be all reductions and an
        #  engineer would never learn that eleven clusters are heading for
        #  their ceiling. Counting them here is what stops the ranking from
        #  hiding the risk it deprioritises.
        #
        #  "Classified but not actionable" is its own number and not an
        #  oversight: an Overprovisioned cluster already floored by N-1
        #  tolerance has nothing to do, and saying so is a better answer than
        #  either listing it or silently dropping it.
        right_sizing_summary = {
            "clusters_analysed": len(all_results),
            "reductions_available": sum(1 for r in ranked if (r.get("node_delta") or 0) < 0),
            "expansions_needed": sum(1 for r in ranked if (r.get("node_delta") or 0) > 0),
            "flagged_but_no_change_possible": sum(
                1 for r in ranked
                if not r.get("node_delta") and r.get("classification") != "Healthy"
            ),
            "healthy": sum(1 for r in ranked if r.get("classification") == "Healthy"),
        }
    evidence = {
        "top_candidates": top_candidates,
        "right_sizing_summary": right_sizing_summary,
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
        _dropped("graph.generate_final_report_failed", state, exc)
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


#: Statuses that represent a decision the reviewer actually made. "Superseded"
#: is not here on purpose - see _record_decisions.
_DECIDED_STATUSES = ("Approved", "Rejected")


def _record_decisions(state, decided_ids: list[int]) -> None:
    """Write the audit row for each recommendation the reviewer decided.

    Skipped entirely when there is no reviewer id. DecidedBy is NOT NULL with a
    foreign key to sad.Employee, so there is no honest value to substitute - an
    audit trail that invents an approver is worse than one that is empty, because
    the empty one is visibly missing.
    """
    reviewer = state.get("reviewer_employee_id")
    decision = state.get("decision")
    if not reviewer or decision not in ("Approve", "Reject", "RequestMoreAnalysis"):
        return
    for recommendation_id in decided_ids:
        recommendation_repository.save_decision(
            recommendation_id=recommendation_id,
            decision=decision,
            decision_reason=state.get("comments"),
            decided_by=reviewer,
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
    decided_ids: list[int] = []
    for c in state.get("candidate_scores", [])[: settings.policy.top_clusters]:
        explanation = explanations_by_cluster.get(c["cluster_code"])
        sub = c.get("subscores") or {}
        cluster_status = status_for(c["cluster_code"], None)
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
            "Status": cluster_status,
        }
        cluster_rec_id = recommendation_repository.save(rec)
        saved_ids.append(cluster_rec_id)
        if cluster_status in _DECIDED_STATUSES:
            decided_ids.append(cluster_rec_id)

        for n in (c.get("top_nodes") or [])[: settings.policy.top_nodes_per_cluster]:
            nsub = n.get("subscores") or {}
            nprojected = n.get("projected") or {}
            node_status = status_for(c["cluster_code"], n["host_name"])
            node_rec_id = recommendation_repository.save(
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
                        "Status": node_status,
                    }
            )
            saved_ids.append(node_rec_id)
            if node_status in _DECIDED_STATUSES:
                decided_ids.append(node_rec_id)

    # After the rows exist: RecommendationDecision has a foreign key to
    # RecommendationId, so the audit row cannot be written until its subject has
    # been saved.
    _record_decisions(state, decided_ids)

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
