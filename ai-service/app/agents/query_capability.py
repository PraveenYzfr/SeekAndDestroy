"""What this platform can and cannot be asked, decided before anything is spent.

THE FAILURE THIS EXISTS FOR
---------------------------
A real query: "give me best dc for java apps". It ran a full investigation and
returned, in part:

    The evidence does not include any record of which clusters host Java
    applications... It mentions hosting locations for APP-DOCSIGN on dal-03,
    APP-KYC-SYNC0062 on den-p119 ... but none of these applications is tagged
    as Java.

Everything in that is true and all of it is useless, in three separate ways.

1. It reads as a RETRIEVAL MISS - "the evidence does not include" - when the
   real fact is structural: this CMDB has no column recording what language an
   application is written in, and never will have one by searching harder. The
   reader cannot tell "we did not find it" from "it does not exist", and those
   have completely different next steps.

2. It listed five applications and their clusters. None of them relate to the
   question. That is the retriever's top-k pasted into a refusal, and it reads
   as partial evidence when it is noise.

3. It never engaged with the answerable half. "Best DC" is exactly what this
   platform does - it ranks candidates across eight data centres. It was the
   "java" qualifier that could not be honoured, not the request.

WHY THE GUARD THAT SHOULD HAVE CAUGHT IT DID NOT
------------------------------------------------
quick_reply already had the right check: infrastructure-shaped, no application
code, no resource quantity - ask for one rather than investigating nothing. It
did not fire because of its final clause, ``len(text.split()) <= 6``.

    "give me best dc for apps"        6 words -> intercepted correctly
    "give me best dc for java apps"   7 words -> full investigation, useless report

One word. The length test is a proxy for vagueness, and vagueness is not length:
"find hosting for APP-CRM" is four words and perfectly actionable, while "give me
the best data centre for our java applications" is nine and actionable by
nobody. The proxy has to go, but it cannot simply be deleted - it is also what
stops this heuristic swallowing legitimate long questions like "why was the
incident on the payments cluster caused by a failed change?", which mentions a
cluster, carries no app code or quantity, and is perfectly answerable.

So the length test is replaced by the distinction it was standing in for:
PLACEMENT intent ("where should this go") needs something to place and is
refused without it at any length, while QUESTION intent ("why did this happen")
does not.

WHY THIS IS DETERMINISTIC AND NOT A MODEL CALL
-----------------------------------------------
Whether a column exists is a fact about the schema. Asking a model to decide it
invites exactly the confident invention the rest of this platform is built to
prevent - and it would cost a call to answer a question that a dictionary
answers for free. This file is that dictionary; app.insights.whitelist is the
same idea for the query layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class UnmodelledAttribute:
    """Something people ask about that this CMDB does not record.

    ``instead`` names the nearest thing it DOES record. A refusal that only says
    "no" leaves the reader guessing whether to rephrase or give up; one that
    names the adjacent attribute turns a dead end into the next query.
    """

    name: str
    terms: tuple[str, ...]
    instead: str


#: Verified against INFORMATION_SCHEMA, not assumed. Every entry here was
#: checked to be genuinely absent - probes for licence, patch level, compliance
#: scope, power, latency and cost all found real columns and are deliberately
#: NOT listed, because claiming the platform cannot answer something it can is
#: the same class of error as claiming it can answer something it cannot.
UNMODELLED_ATTRIBUTES: tuple[UnmodelledAttribute, ...] = (
    UnmodelledAttribute(
        name="runtime language",
        # Word-boundary matched below. "java" must not fire on "javascript" as
        # a different language, but both are equally unrecorded, so both are
        # listed rather than relying on one to catch the other.
        terms=(
            "java", "javascript", "python", "dotnet", ".net", "c#", "csharp",
            "node", "nodejs", "node.js", "golang", "ruby", "php", "scala",
            "kotlin", "spring", "spring boot", "jvm", "cobol", "rust",
        ),
        instead=(
            "TechnologyPlatform, which records the HOSTING platform an "
            "application runs on rather than the language it is written in"
        ),
    ),
)

#: "Where should this go" - the question the placement engine answers.
#:
#: Deliberately narrow. Every phrase here is a request to CHOOSE somewhere,
#: which is meaningless without knowing what is being placed. "Which cluster is
#: the payments app on today" is a lookup, not a placement, and must not match.
_PLACEMENT_INTENT_RE = re.compile(
    r"\b("
    r"best\s+(?:dc|data\s?cent(?:re|er)s?|site|region|cluster|host|place|location)"
    r"|(?:which|what)\s+(?:dc|data\s?cent(?:re|er)s?|site|region|cluster|host)\s+"
    r"(?:should|would|to|for|is\s+best)"
    r"|where\s+(?:should|can|do|would)\s+(?:i|we|you)\s+"
    r"(?:put|host|place|deploy|run|migrate)"
    r"|where\s+to\s+(?:put|host|place|deploy|run)"
    r"|(?:find|recommend|suggest|pick|choose)\s+(?:me\s+)?"
    r"(?:a\s+|an\s+|the\s+)?(?:best\s+)?"
    r"(?:dc|data\s?cent(?:re|er)s?|site|region|cluster|host|home|placement)"
    r")\b",
    re.IGNORECASE,
)

#: Data-centre words, absent from _INFRA_INTENT_WORDS in nodes.py. "dc" is how
#: people actually write it and matched nothing at all - the example query only
#: registered as infrastructure-shaped because "apps" contains "app".
DATACENTRE_WORDS: tuple[str, ...] = (
    "dc", "datacenter", "datacentre", "data center", "data centre", "site", "region",
)


#: Inflections a noun in the list is allowed to carry. Deliberately a closed set
#: rather than a wildcard.
#:
#: The first fix used ``\bapp\w*``, which correctly stopped "happened" matching
#: "app" - and then matched "apply" and "apparently", which DO begin with those
#: three letters. "apply the recommendation" is a sentence this platform sees
#: often, and treating it as infrastructure-shaped is the same false positive
#: moved one step along.
#:
#: A wildcard suffix cannot tell an inflection from a different word that
#: happens to share a prefix. An explicit set can.
_INFLECTIONS = ("", "s", "es", "ing")


def mentions_any(query: str, words: tuple[str, ...]) -> bool:
    """Whether the query uses any of ``words`` AS WORDS, not as substrings.

    ``"app" in "what happened in INC1009985?"`` is True, because "happened"
    contains a-p-p. That single substring match sent every incident lookup in
    the golden set to "I need a bit more to work with" - four cases, an entire
    query class, refused for containing an ordinary English word.

    "host" is inside "ghost", "place" is inside "replace", "app" is inside
    "apply", "apparent" and "happy". Short infrastructure nouns are exactly the
    strings most likely to occur inside unrelated words, which is what makes
    substring matching wrong here rather than merely loose.

    Bounded at BOTH ends, with a closed set of inflections between - so "app"
    matches "app", "apps" and "hosting" matches "host", while "apply",
    "apparently" and "happened" match nothing.
    """
    forms = {
        f"{w}{suffix}"
        for w in words
        for suffix in _INFLECTIONS
        # Multi-word entries like "data centre" take no suffix.
        if " " not in w or suffix == ""
    }
    alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return bool(re.search(rf"\b(?:{alternatives})\b", query, re.IGNORECASE))


def unmodelled_attribute(query: str) -> UnmodelledAttribute | None:
    """The attribute this query filters on that the CMDB does not record."""
    lowered = query.lower()
    for attribute in UNMODELLED_ATTRIBUTES:
        for term in attribute.terms:
            # Word boundaries: "java" must not match inside "javadoc", and
            # ".net" must not match inside "subnet.network".
            if re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", lowered):
                return attribute
    return None


def has_placement_intent(query: str) -> bool:
    return bool(_PLACEMENT_INTENT_RE.search(query))


@lru_cache(maxsize=1)
def _estate_shape() -> tuple[int | None, tuple[str, ...]]:
    """How many data centres exist, and what platforms are recorded.

    Read from the database rather than written here as a list. A hard-coded
    "eight data centres" is correct until somebody adds one, and then it is a
    figure this platform states confidently and wrongly - which is the exact
    failure the rest of the codebase is built to prevent. Cached, because the
    shape of an estate does not change between requests.

    Returns (None, ()) when the database cannot be read, and every caller is
    written to say less rather than to guess.
    """
    try:
        from app.repositories.base import fetch_all

        centres = fetch_all(
            "SELECT COUNT(DISTINCT DataCenter) AS n FROM sad.InfrastructureCluster"
        )
        platforms = fetch_all(
            "SELECT DISTINCT TechnologyPlatform AS p FROM sad.CmdbApplication "
            "WHERE TechnologyPlatform IS NOT NULL"
        )
        count = int(centres[0]["n"]) if centres and centres[0].get("n") else None
        names = tuple(sorted(str(r["p"]) for r in platforms if r.get("p")))
        return count, names
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_capability.estate_shape_unavailable", error=str(exc)[:200])
        return None, ()


def reset_estate_cache() -> None:
    """For tests, and for a process that outlives a schema change."""
    _estate_shape.cache_clear()


def capability_reply(query: str, *, has_app_code: bool, has_quantity: bool) -> str | None:
    """An honest answer for a query this platform cannot take as written.

    Returns None when the query should proceed to the graph - this only ever
    intercepts, never redirects.

    The two conditions are independent and both can hold at once, which is
    precisely the case that produced the bad report: "java" is unrecordable AND
    no application or size was given. Answering only one of them would send the
    reader to supply an app code and hit the same wall from the other side.
    """
    attribute = unmodelled_attribute(query)
    placement = has_placement_intent(query)

    if attribute is None and not placement:
        return None
    if attribute is None and (has_app_code or has_quantity):
        # A placement request that names what to place. That is a real
        # investigation and must not be intercepted.
        return None

    parts: list[str] = []

    if attribute is not None:
        parts.append(
            f"I cannot filter by {attribute.name}: this CMDB does not record it. "
            f"There is no column for it on any application, so it is not a matter "
            f"of searching harder - the data was never captured. "
            f"What each application does record is {attribute.instead}."
        )
        _, platforms = _estate_shape()
        if platforms:
            parts.append("The platforms actually recorded are " + ", ".join(platforms) + ".")

    if placement and not has_app_code and not has_quantity:
        centres, _ = _estate_shape()
        where = f"across the {centres} data centres" if centres else "across the estate"
        parts.append(
            f"I can rank candidates {where}, but I need to know what is being placed. "
            f"Either name the application (\"best data centre for APP-CRM\") or give me "
            f"its size (\"32 cores, 128 GB RAM and 2 TB storage in production\")."
        )
    elif attribute is not None and placement:
        parts.append(
            "Drop that qualifier and I will rank the candidates for what you named."
        )

    return " ".join(parts) if parts else None
