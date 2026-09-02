"""Deterministic intent routing for free-text CMDB Insighter questions.

Consistent with the rest of this platform (see app.graph.nodes on why
investigation-type routing is keyword/regex matching rather than an LLM
call): deciding WHICH kind of question this is costs nothing and must be
reliable, so it is decided here in plain Python. Only two things ever touch
a model: mapping a question onto a constrained query spec
(app.insights.spec_parser) and narrating an already-computed result
(app.insights.narrator) - never deciding what to compute, and never
producing a number.

FOUR INTENTS, EACH BACKED BY WORK ALREADY BUILT AND TESTED
-------------------------------------------------------------
  health    app.insights.cmdb_health - completeness, staleness, orphans,
            duplicates, coverage. Answered with a Python-composed narrative,
            not a model call: the numbers are a fixed, known shape every
            time, and templating them costs nothing and cannot drift.
  impact    app.insights.impact_analysis - blast radius from a named CI.
            Same reasoning: the answer is a count, a depth and a
            true/false ceiling flag, not something that benefits from an
            LLM's phrasing.
  business_service_leader
            app.insights.business_service_impact.volume_vs_severity_leader -
            "which service has the most incidents overall, and is that the
            one hit hardest by SevNs" is a fixed two-number comparison, not
            an open-ended aggregation - routed here instead of the generic
            path below for the same reason health/impact are: a comparison
            this well-defined does not need an LLM to decide what to
            compute, and this question was observed live to trigger
            deepseek-v4-flash's reasoning-overflow failure on the
            two-LLM-call aggregate path (16-17k reasoning chars, twice in a
            row) - a well-defined question should not depend on a
            reasoning model choosing to stop reasoning.
  aggregate anything else - the general NL-to-query-spec-to-SQL-to-narrated
            pipeline (severity by root cause, changes by close code, and so
            on). This is the one path that calls an LLM twice, because the
            question's shape is not fixed in advance.
"""

from __future__ import annotations

import re

from langchain_core.language_models.chat_models import BaseChatModel

from app.insights import business_service_impact, cmdb_health, impact_analysis
from app.insights.narrator import narrate
from app.insights.query_builder import run_query
from app.insights.spec_parser import parse_query_spec
from app.insights.whitelist import SEVERITY_SYNONYMS

# No trailing \b: "healthy", "duplicates", "orphaned" must all match their
# root word. A trailing boundary here is exactly the kind of regex-vs-domain
# mismatch the mission brief warned about (two prior regexes in this
# codebase silently returned nothing for weeks because they described an
# imagined vocabulary rather than the real one) - caught here by testing
# against a real question ("How healthy is our CMDB?") rather than trusting
# the pattern by inspection.
_HEALTH_WORDS = re.compile(
    r"\b(health|completeness|complete|stale|orphan|duplicate|coverage|"
    r"unowned|unclassified|unhosted|ownership)",
    re.IGNORECASE,
)
# Same reasoning as _HEALTH_WORDS: no trailing \b, so "failing", "dying",
# "affected" all match their root rather than only the one inflected form
# spelled out here.
_IMPACT_WORDS = re.compile(
    r"\b(blast radius|what breaks|what happens if|impact of|impact analysis|"
    r"fail|die|goes down|affected)",
    re.IGNORECASE,
)

# Fires only on the compound comparison shape ("most incidents overall, and
# is that the same one hit hardest by Sev1s") - a plain "incidents by
# business service" question has no comparison word and correctly falls
# through to the generic aggregate path instead, since that IS an open
# group-by with no fixed second half to compute.
_BUSINESS_SERVICE_WORDS = re.compile(r"business service", re.IGNORECASE)
_LEADER_COMPARISON_WORDS = re.compile(
    r"\b(most incidents|hit hardest|hardest hit|hit the hardest|highest|leader|"
    r"top service|same (one|service))\b",
    re.IGNORECASE,
)

#: Words common enough in a real question that trying them as a CI name
#: would waste a lookup on every single call. Not a correctness issue if
#: omitted - resolve_ci_id would just miss - but skipping them keeps the
#: common case fast and the failure mode (no CI found) less noisy to read
#: through if this is ever logged.
_STOPWORDS = frozenset(
    "what breaks happens fails dies goes down affected impact analysis blast radius "
    "the if of this that when where how many will would could should".split()
)


class NoCiNamedError(ValueError):
    """Raised when an impact question names no CI this layer can resolve -
    refuses rather than guessing a CI or silently falling through to a
    different intent, which would answer a question nobody asked."""


def _extract_ci_name(question: str) -> str | None:
    """The same "does this look like an identifier" approach
    app.graph.scope uses to decide whether a question is on-topic at all,
    applied here to find WHICH CI an impact question is actually about.
    Tries each candidate token against the real ConfigurationItem table
    rather than guessing from shape alone - a token that looks like a code
    but names nothing real must not be treated as a match.
    """
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9\-_.]{2,}", question)
    for token in candidates:
        if token.lower() in _STOPWORDS:
            continue
        if impact_analysis.resolve_ci_id(token) is not None:
            return token
    return None


def _health_narrative(report: dict) -> dict:
    """A fixed-shape Python narrative over cmdb_health.health_report() -
    never a model call, because the shape of this answer never varies and
    templating it cannot invent a number the SQL did not already produce.

    Ordering matters here the same way it does everywhere else in this
    package: the finding with real signal (unhosted applications, orphans,
    duplicates) is stated plainly; the fact that every other class's
    completeness gap is a generator artefact rather than an estate finding
    is stated too, not smoothed over one way or the other.
    """
    total_cis = sum(r["TotalCis"] for r in report["completeness_by_class"])
    unhosted = report["unhosted_application_breakdown"]
    total_orphans = sum(r["OrphanCis"] for r in report["orphans_by_class"])
    total_duplicates = len(report["duplicates_by_class"])

    headline = f"{total_cis:,} configuration items across {len(report['classes'])} classes."

    lines = [headline]
    if unhosted["total_unhosted"]:
        lines.append(
            f"{unhosted['unhosted_and_unconnected']} applications have no hosting record and no "
            f"relationship of any kind - genuinely unmapped infrastructure. "
            f"{unhosted['unhosted_but_dependency_linked']} more are unhosted but still linked to "
            f"something they depend on."
        )
    if total_orphans:
        lines.append(f"{total_orphans} configuration items have no relationship to anything else in the estate.")
    if total_duplicates:
        lines.append(f"{total_duplicates} names are shared by more than one configuration item of the same class.")

    caveats = [
        "Ownership and support-group completeness reflect what the discovery process has recorded, "
        "not a judgement about whether the estate is well-run.",
    ]

    return {
        "intent": "health",
        "headline": headline,
        "narrative": " ".join(lines),
        "caveats": caveats,
        "table": {
            "title": "Completeness by class",
            "columns": ["ClassName", "TotalCis", "MissingOwnedById", "MissingManagedById", "MissingDataClassification"],
            "rows": [
                [r["ClassName"], r["TotalCis"], r["MissingOwnedById"], r["MissingManagedById"], r["MissingDataClassification"]]
                for r in report["completeness_by_class"]
            ],
        },
        # Findings a reader should be able to get to, but that should not
        # compete with the headline for attention (same disclosure pattern
        # as CandidateTable's "show other options considered").
        "details": {
            "orphans_by_class": report["orphans_by_class"],
            "duplicates_by_class": report["duplicates_by_class"],
            "unhosted_application_breakdown": unhosted,
        },
        "row_count": len(report["completeness_by_class"]),
    }


def _extract_severity(question: str) -> str:
    """Which severity a business-service-leader comparison means, found the
    same way app.insights.whitelist.normalize_severity maps a filter value -
    reusing SEVERITY_SYNONYMS rather than a second hand-written mapping, so
    the two never drift apart. Searched as a substring (longest key first,
    so "sev1" cannot shadow "severity1" or vice versa) rather than matched as
    an exact filter value, since this is free text, not a validated filter.

    Defaults to Sev1 when no severity is named - every version of this
    question seen so far (and the approved example on the Insights screen)
    names one explicitly, but a reader asking "which service is hit
    hardest" with no severity at all should get the platform's own default
    top severity rather than an error demanding they repeat themselves.
    """
    text = question.lower()
    for key in sorted(SEVERITY_SYNONYMS, key=len, reverse=True):
        if key in text:
            return SEVERITY_SYNONYMS[key]
    return "Sev1"


def _business_service_leader_payload(result: dict) -> dict:
    """Python-composed narrative over
    business_service_impact.volume_vs_severity_leader() - same reasoning as
    _health_narrative and _impact_payload: the shape of this answer (two
    named leaders and whether they match) is fixed in advance, so templating
    it costs nothing and cannot invent a number the SQL did not produce.
    """
    top_severity = result["top_severity"]
    overall = result["overall_leader"]
    overall_count = result["overall_leader_count"]
    filtered = result["filtered_leader"]
    filtered_count = result["filtered_leader_count"]

    if overall is None:
        return {
            "intent": "business_service_leader",
            "headline": "No incidents resolve to a business service yet.",
            "narrative": "",
            "caveats": [
                "Business service is only known for an incident whose application has a service "
                "relationship recorded - none currently do."
            ],
            "table": None,
            "details": result,
            "row_count": 0,
        }

    headline = f"{overall} has the most incidents overall, with {overall_count:,}."
    insight = ""

    if filtered is None:
        narrative = (
            f"No {top_severity} incidents resolve to a business service, so no {top_severity} "
            f"leader can be named."
        )
    elif result["leader_changes"]:
        narrative = (
            f"{filtered} is hit hardest by {top_severity}s, with {filtered_count:,} - a different "
            f"service from the one with the most incidents overall."
        )
        insight = (
            f"{overall} dominates the overall incident count, but {filtered} is where {top_severity} "
            f"severity actually concentrates - the busiest service by volume is not the one at "
            f"greatest {top_severity} risk."
        )
    else:
        narrative = (
            f"{filtered} is also hit hardest by {top_severity}s, with {filtered_count:,} - the same "
            f"service that leads overall."
        )

    return {
        "intent": "business_service_leader",
        "headline": headline,
        "narrative": narrative,
        "insight": insight,
        "caveats": [
            "Business service is only known for incidents whose application has a service "
            "relationship recorded - incidents with no such relationship are excluded from this "
            "comparison, not counted as zero."
        ],
        "table": {
            "title": f"Overall vs. {top_severity} leader",
            "columns": ["scope", "business_service", "count"],
            "rows": [
                ["overall", overall, overall_count],
                [top_severity, filtered, filtered_count],
            ],
        },
        "details": result,
        "row_count": 2,
    }


def _impact_payload(ci_name: str, result: dict) -> dict:
    if result["hit_depth_ceiling"]:
        headline = f"At least {result['affected_cis']} configuration items would be affected if {ci_name} fails."
        caveats = [
            "This is a LOWER BOUND, not an exact count - the traversal reached its depth limit before "
            "exhausting every path, so the true blast radius may be larger.",
        ]
    else:
        headline = f"{result['affected_cis']} configuration items would be affected if {ci_name} fails."
        caveats = []

    return {
        "intent": "impact",
        "headline": headline,
        "narrative": (
            f"Walking the relationship graph from {ci_name} outward reaches "
            f"{result['affected_cis']} other configuration item{'s' if result['affected_cis'] != 1 else ''}, "
            f"at a maximum depth of {result['max_depth']} hop{'s' if result['max_depth'] != 1 else ''}."
        ),
        "caveats": caveats,
        "table": None,
        "details": result,
        "row_count": result["affected_cis"],
    }


def answer_free_text(spec_llm: BaseChatModel, narrator_llm: BaseChatModel, question: str) -> dict:
    """A free-text question in, one composed, screen-ready answer out.

    Every branch's numbers already come from a tested module
    (cmdb_health / impact_analysis / query_builder) - this function only
    decides which one to call and shapes the result, never computes
    anything itself.
    """
    if _IMPACT_WORDS.search(question):
        name = _extract_ci_name(question)
        if name is None:
            raise NoCiNamedError(
                "Name the specific configuration item (a cluster code, server hostname, application "
                "code, or similar) you want the blast radius for."
            )
        result = impact_analysis.blast_radius_for_name(name)
        return _impact_payload(name, result)

    if _HEALTH_WORDS.search(question):
        return _health_narrative(cmdb_health.health_report())

    if _BUSINESS_SERVICE_WORDS.search(question) and _LEADER_COMPARISON_WORDS.search(question):
        severity = _extract_severity(question)
        result = business_service_impact.volume_vs_severity_leader(severity)
        return _business_service_leader_payload(result)

    spec = parse_query_spec(spec_llm, question)
    result = run_query(spec)
    narrative = narrate(narrator_llm, question, result)
    return {
        "intent": "aggregate",
        "headline": narrative.headline,
        "narrative": narrative.narrative,
        "insight": narrative.insight,
        "caveats": narrative.caveats,
        "table": {
            "title": None,
            "columns": [*result["group_by"], "count"],
            "rows": [[*(row[c] for c in _bare_group_columns(result)), row["IncidentCount"]] for row in result["rows"]],
        },
        "filters_applied": result["filters"],
        "row_count": result["distinct_groups"],
        "total_count": narrative.total_count,
    }


def _bare_group_columns(result: dict) -> list[str]:
    """Row dict keys for the group_by columns are bare column names (see
    app.insights.query_builder._bare_name), not the whitelist dimension
    keys - e.g. group_by=["data_center"] produces rows keyed "DataCenter".
    Reconstructed here from whatever the first row actually has, rather than
    re-deriving it from the whitelist, so this stays correct even if a
    future dimension's bare name does not match a simple guess.
    """
    if not result["rows"]:
        return []
    return [k for k in result["rows"][0] if k != "IncidentCount"]
