"""What a follow-up refers to.

Every chat message used to be an independent investigation. "give me the
options again" carried no reference to anything, so it classified as a general
question, retrieved nothing, and answered that it had no grounded information -
a correct answer to a question nobody asked.

This module resolves a follow-up against the conversation it belongs to. It
does that with plain pattern matching over the query and the previous
investigation's own results, never by asking the LLM what the user meant. That
is the same trust boundary enforced everywhere else in this platform: routing
and resolution are deterministic, the LLM only narrates. A model that decided
"the options" meant a different cluster than the one actually shortlisted would
produce an answer about infrastructure the engineer never saw.

Three kinds of follow-up, in the order they are tested:

``RECALL``
    "give me the options again" - restate the previous shortlist. No new
    investigation: re-running the pipeline would produce a second Investigation
    row and, because utilization moves, possibly a *different* answer to
    "again".

``ABOUT_PREVIOUS``
    "why was that rejected?" - a real question, answered as a Question
    investigation whose grounding is the previous investigation's evidence
    rather than a vector search that has no idea which results are meant.

``INHERIT_SUBJECT``
    "what about in staging?" - a new investigation about the same subject. The
    subject (an application code, or a capacity size) is carried forward into
    the query the pipeline classifies and extracts from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

RECALL = "Recall"
ABOUT_PREVIOUS = "AboutPrevious"
INHERIT_SUBJECT = "InheritSubject"

#: Multi-segment, not single. The first version stopped at the first hyphen, so
#: APP-AML-API0044 read as APP-AML - a code that does not exist. 1,160 of the
#: 1,200 applications carry two or more segments, so it was wrong for 97% of the
#: estate: a valid request answered "APP-AML not found in CMDB", naming a code
#: the user never typed.
_APP_CODE_RE = re.compile(r"\bAPP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")

#: Real cluster codes are atl-03 and cmh-p212. The previous pattern matched
#: CL-PROD-01, which fits none of the 256 clusters in the CMDB and had therefore
#: never once fired - so names_a_cluster() was always False and a follow-up that
#: named a cluster was invisible to the recall logic.
#:
#: Both regexes on these lines described a corpus that was imagined rather than
#: the one that exists, and both were found by the data changing rather than by
#: a test, because the tests used hand-written examples in the same wrong shape.
_CLUSTER_CODE_RE = re.compile(r"\b[a-z]{3}-p?\d{2,3}\b", re.IGNORECASE)

#: Words that point back at something already said. Deliberately does not
#: include bare "the clusters" or "hosts" - those appear in perfectly
#: self-contained questions ("which clusters are underutilized?") and treating
#: them as references would hijack queries that need no context at all.
_REFERENTIAL_RE = re.compile(
    r"\b("
    r"that|those|these|them|they|it|its|their|"
    r"the (?:first|second|third|top|last|other|same) one|"
    r"the (?:options?|candidates?|shortlist|recommendations?|results?|choices?)|"
    r"previous(?:ly)?|earlier|above|before that|"
    r"you (?:picked|chose|recommended|suggested|said|found|ranked)"
    r")\b",
    re.IGNORECASE,
)

#: "Say that again", in the phrasings people actually use. The noun-phrase form
#: requires a determiner ("the options", "those candidates") on purpose: "show
#: clusters" is a question about the estate, not a request to repeat anything.
_RECALL_RE = re.compile(
    r"\b(?:again|repeat that|repeat the|one more time|as before)\b"
    r"|\b(?:show|list|give|send|display)\s+(?:me\s+)?(?:the|those|that|these)\s+"
    r"(?:options?|candidates?|clusters?|hosts?|shortlist|recommendations?|results?|list)\b"
    r"|\bwhat\s+(?:were|was)\s+(?:the|those|that)\s+"
    r"(?:options?|candidates?|clusters?|hosts?|shortlist|recommendations?|results?)\b",
    re.IGNORECASE,
)

#: A question, grammatically. Used together with a referential phrase - one
#: without the other is not a follow-up question about previous results.
_QUESTION_START_RE = re.compile(
    r"^\s*(?:why|what|what's|whats|how|which|who|where|when|explain|compare|tell me|is|are|was|were|did|does|do|can)\b",
    re.IGNORECASE,
)

#: Openers that continue the previous request rather than starting a new one.
_CONTINUATION_RE = re.compile(
    r"^\s*(?:and|but|also|or|plus|then)\b"
    r"|^\s*(?:what|how)\s+about\b"
    r"|\b(?:instead|as well|the same|try)\b",
    re.IGNORECASE,
)

#: A bare prepositional fragment - "in staging?", "with 128 GB RAM". Only a
#: continuation when it is the *whole* message: "in production, which clusters
#: are underutilized?" is a complete question that happens to start with a
#: preposition, and carrying a subject into it would turn a right-sizing
#: question into a placement request for an application nobody mentioned.
_FRAGMENT_RE = re.compile(r"^\s*(?:in|with|without|for|on|at|under|using)\b", re.IGNORECASE)
_FRAGMENT_MAX_WORDS = 5


@dataclass(frozen=True)
class PriorInvestigation:
    """The previous investigation in this conversation, as far as a follow-up
    needs to know about it.

    Built from the LangGraph checkpoint (:func:`from_state`), which is where
    the full candidate evidence lives - the InfrastructureRecommendation rows
    hold the same shortlist but only the top N of it, and rejected candidates
    with the reasons they were rejected are exactly what "why not that one?"
    needs.
    """

    investigation_id: int
    investigation_type: str
    user_query: str
    status: str = "Completed"
    confidence: str = "Medium"
    application_code: Optional[str] = None
    requirement: Optional[dict] = None
    candidate_scores: list[dict] = field(default_factory=list)
    final_report: Optional[dict] = None

    @property
    def has_options(self) -> bool:
        return bool(self.candidate_scores)

    @property
    def awaiting_review(self) -> bool:
        return self.status == "AwaitingReview"

    @classmethod
    def from_state(cls, investigation_id: int, state: dict, status: str = "Completed") -> "PriorInvestigation":
        return cls(
            investigation_id=investigation_id,
            investigation_type=str(state.get("investigation_type") or "Question"),
            user_query=str(state.get("user_query") or ""),
            status=status,
            confidence=str(state.get("confidence") or "Medium"),
            application_code=(state.get("application_requirements") or {}).get("application_code"),
            requirement=state.get("requirement"),
            candidate_scores=list(state.get("candidate_scores") or []),
            final_report=state.get("final_report"),
        )


@dataclass(frozen=True)
class Resolution:
    """What to do with this turn.

    ``kind`` is None for an ordinary query that refers to nothing. ``reply``
    short-circuits the whole pipeline with a direct answer - used only when the
    query clearly refers back but there is nothing to refer to.
    """

    kind: Optional[str] = None
    resolved_query: str = ""
    prior: Optional[PriorInvestigation] = None
    reply: Optional[str] = None


def looks_like_follow_up(query: str) -> Optional[str]:
    """Which kind of follow-up this query is, from its wording alone.

    Independent of whether a prior investigation exists, so the caller can tell
    "this refers to something" apart from "and here is the something" - a
    reference with no referent deserves a better answer than an empty report.
    """
    text = query.strip()
    if not text:
        return None

    upper = text.upper()
    # A query that names the thing it is about is not a follow-up, whatever
    # else it says. "find hosting for APP-CRM in the same data center as it"
    # contains "it" and "the same", and resolving it against a previous
    # investigation would answer about an application the engineer just
    # replaced. Naming a subject is how you start a new request.
    has_own_subject = bool(_APP_CODE_RE.search(upper))
    names_a_cluster = bool(_CLUSTER_CODE_RE.search(upper))
    if has_own_subject:
        return None

    referential = bool(_REFERENTIAL_RE.search(text))

    # "give me the options again". Not a recall if it names a cluster:
    # "forecast CL-NYC-03 again" is a request to run something, and the thing
    # it names is what it should run against.
    if not names_a_cluster and _RECALL_RE.search(text):
        return RECALL

    # "why was that rejected?" - a question, pointing back. A cluster code is
    # allowed here: "why was CL-NYC-03 rejected then?" is still a question
    # about the results the engineer is looking at, and grounding it in those
    # results beats a vector search that has no idea which run is meant.
    if referential and _QUESTION_START_RE.search(text):
        return ABOUT_PREVIOUS

    # "what about in staging?" - continues the last request rather than
    # starting one. A bare prepositional opener only counts when it is the
    # whole message: "in production, which clusters are underutilized?" is a
    # complete question, and carrying a subject into it would turn a
    # right-sizing question into a placement request for an application nobody
    # mentioned.
    if not names_a_cluster:
        fragment = bool(_FRAGMENT_RE.match(text)) and len(text.split()) <= _FRAGMENT_MAX_WORDS
        if fragment or _CONTINUATION_RE.search(text):
            return INHERIT_SUBJECT

    if referential:
        return ABOUT_PREVIOUS

    return None


#: Said when a follow-up has nothing to follow. Better than running the query
#: as a fresh investigation and reporting that the context was empty, which is
#: what used to happen.
_NO_REFERENT = (
    "I don't have an earlier result in this conversation to refer back to. "
    "Ask me to find hosting for an application (for example \"find hosting for APP-CRM\") "
    "or give me the resources you need (\"64 cores, 512 GB RAM and 4 TB storage\"), "
    "and then \"show me those options again\" will have something to show."
)

_NO_OPTIONS = (
    "The previous answer in this conversation was not a shortlist, so there are no options to repeat. "
    "Ask me to find hosting for an application or a capacity size and I will rank the clusters and hosts that fit."
)


def resolve(query: str, prior: Optional[PriorInvestigation]) -> Resolution:
    """Decide what this turn means in the context of its conversation."""
    kind = looks_like_follow_up(query)
    if kind is None:
        return Resolution(kind=None, resolved_query=query, prior=None)

    if prior is None:
        return Resolution(kind=kind, resolved_query=query, prior=None, reply=_NO_REFERENT)

    if kind == RECALL:
        if not prior.has_options:
            return Resolution(kind=RECALL, resolved_query=query, prior=prior, reply=_NO_OPTIONS)
        return Resolution(kind=RECALL, resolved_query=query, prior=prior)

    if kind == INHERIT_SUBJECT:
        carried = carry_subject(query, prior)
        if carried is None:
            # Nothing to inherit (the previous turn was a question, not a
            # placement) - treat it as a question about that previous turn
            # rather than pretending a subject exists.
            return Resolution(kind=ABOUT_PREVIOUS, resolved_query=query, prior=prior)
        return Resolution(kind=INHERIT_SUBJECT, resolved_query=carried, prior=prior)

    return Resolution(kind=ABOUT_PREVIOUS, resolved_query=query, prior=prior)


def carry_subject(query: str, prior: PriorInvestigation) -> Optional[str]:
    """Append the previous subject to the query, or None if there isn't one.

    The user's words come first and the carried subject second, because
    extraction takes the *first* match for each dimension
    (app.graph.nodes._capacity_requirement_from_regex). "and with 128 GB RAM?"
    must resolve to 128 GB with the previous CPU and storage, not to the
    previous memory figure with the new one ignored.

    The wording is not decoration: it has to classify. "find" and "hosting"
    are the tokens app.graph.nodes.classify_investigation_type looks for, so a
    carried subject produces the same investigation type the original request
    did instead of degrading to a general question.
    """
    if prior.application_code:
        return f"{query} (continuing the request to find hosting for {prior.application_code})"

    requirement = prior.requirement or {}
    cpu = requirement.get("cpu_cores")
    memory = requirement.get("memory_gb")
    storage = requirement.get("storage_gb")
    if cpu is None and memory is None and storage is None:
        return None

    parts = []
    if cpu is not None:
        parts.append(f"{_plain(cpu)} cores")
    if memory is not None:
        parts.append(f"{_plain(memory)} GB RAM")
    if storage is not None:
        parts.append(f"{_plain(storage)} GB storage")
    return f"{query} (continuing the request to find {' and '.join(parts)})"


def _plain(value) -> str:
    """8.0 -> "8". A carried figure is re-parsed by the same regexes that read
    the original request, and "8.0 cores" is a needless difference from what
    the engineer typed.
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def grounding_documents(prior: PriorInvestigation, limit: int = 8) -> list[dict]:
    """The previous investigation, rendered as retrieval context.

    Same shape as app.retrieval.vector_store results ({text, score, entity_type})
    so app.graph.nodes.retrieve_related_context can put these in front of the
    vector hits without the Q&A chain knowing the difference. Scores are 1.0
    because this is not a similarity match - it is the thing the question is
    literally about.

    Every number here is copied from the stored candidate, never recomputed:
    a follow-up must answer with the figures the engineer was shown, not with
    a fresh reading of a cluster that has moved since.
    """
    from app.config import get_settings

    # Which candidates the engineer actually saw. "that one" and "the second
    # one" refer to the shortlist in front of them, not to the 85th entry of a
    # ranked list nobody displayed - so the shown options are labelled with
    # their position, and that is what makes a positional reference
    # answerable.
    shown = get_settings().policy.top_clusters

    docs: list[dict] = [
        {
            "text": _headline(prior, shown),
            "score": 1.0,
            "entity_type": "PriorInvestigation",
        }
    ]
    for index, candidate in enumerate(prior.candidate_scores[: limit - 1]):
        position = index + 1 if index < shown else None
        docs.append(
            {"text": _candidate_document(candidate, position), "score": 1.0, "entity_type": "PriorCandidate"}
        )
    return docs


def _headline(prior: PriorInvestigation, shown: int = 3) -> str:
    report = prior.final_report or {}
    eligible = sum(1 for c in prior.candidate_scores if c.get("eligibility_status") == "Eligible")
    rejected = len(prior.candidate_scores) - eligible
    presented = [
        str(c.get("cluster_code")) for c in prior.candidate_scores[:shown] if c.get("cluster_code")
    ]
    lines = [
        f"Previous request in this conversation (investigation #{prior.investigation_id}, "
        f"type {prior.investigation_type}, status {prior.status}): \"{prior.user_query}\".",
        f"It shortlisted {eligible} eligible and {rejected} rejected clusters.",
    ]
    if presented:
        lines.append(
            "The options presented to the engineer, in order, were: "
            + ", ".join(f"{i + 1}. {code}" for i, code in enumerate(presented))
            + ". A reference to \"that one\" or \"the first/second one\" means one of these."
        )
    if prior.application_code:
        lines.append(f"The application under consideration was {prior.application_code}.")
    if report.get("executive_summary"):
        lines.append(f"Its summary was: {report['executive_summary']}")
    if report.get("top_recommendation"):
        lines.append(f"Its top recommendation was {report['top_recommendation']}.")
    return " ".join(lines)


def _candidate_document(candidate: dict, position: int | None = None) -> str:
    projected = candidate.get("projected") or {}
    failed = [
        f"{r.get('name')}: {r.get('reason')}"
        for r in (candidate.get("rule_results") or [])
        if r.get("passed") is False
    ]
    hosts = [n.get("host_name") for n in (candidate.get("top_nodes") or [])]

    shown_as = (
        f"It was shown to the engineer as option {position}. "
        if position is not None
        else "It was ranked but not shown in the shortlist. "
    )
    parts = [
        shown_as
        + f"Cluster {candidate.get('cluster_code')} was {candidate.get('eligibility_status')} "
        f"in the previous investigation at rank {candidate.get('rank')} "
        f"with overall score {candidate.get('overall_score')}."
    ]
    if projected:
        parts.append(
            "After placing the workload its projected utilization was "
            f"CPU {projected.get('projected_cpu_utilization_percent')}%, "
            f"memory {projected.get('projected_memory_utilization_percent')}%, "
            f"storage {projected.get('projected_storage_utilization_percent')}%, "
            f"leaving {projected.get('projected_headroom_percent')}% headroom."
        )
    if failed:
        parts.append("It failed these rules: " + "; ".join(failed) + ".")
    if hosts:
        parts.append("Its best hosts were " + ", ".join(h for h in hosts if h) + ".")
    return " ".join(parts)


def recall_summary(prior: PriorInvestigation) -> str:
    """One line restating what is being shown again, and whether it is still
    open for a decision."""
    eligible = [c for c in prior.candidate_scores if c.get("eligibility_status") == "Eligible"]
    subject = prior.application_code or f"\"{prior.user_query}\""
    head = (
        f"These are the same {len(eligible)} eligible options from investigation "
        f"#{prior.investigation_id} for {subject}."
    )
    if prior.awaiting_review:
        return head + " It is still awaiting your decision, so you can choose one below."
    return head + " That investigation is already closed; ask for a new one to get fresh numbers."


def turn_summary(result: dict) -> str:
    """A one-line assistant turn for the conversation history.

    History exists to resolve references, not to re-read reports - the report
    itself stays on the Investigation row.
    """
    report = result.get("final_report") or {}
    if result.get("status") == "AwaitingReview":
        payload = result.get("review_payload") or {}
        options = payload.get("options") or []
        codes = ", ".join(str(o.get("cluster_code")) for o in options if o.get("cluster_code"))
        return f"Shortlisted {len(options)} options awaiting review" + (f": {codes}." if codes else ".")
    summary = report.get("executive_summary") or "Answered."
    return summary[:1000]
