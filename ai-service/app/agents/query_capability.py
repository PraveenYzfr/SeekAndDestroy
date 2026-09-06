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

WHAT A REFUSAL MAY SAY
----------------------
A refusal is still an answer, and it is the answer least likely to be reviewed.
The first version of this file wrote one that was accurate, helpful, and a
disclosure: it named the backing store, named the column that exists instead,
listed every platform recorded across the estate, and stated how many data
centres there are. Two of those figures were read live from production on the
refusal path, so the leak stayed current as the estate grew.

None of it was secret in isolation. The shape is the problem - an engineer who
asks one malformed question learns the platform inventory and the size of an
estate they may have no business knowing, and the next attribute added to
UNMODELLED_ATTRIBUTES inherits that behaviour by default.

So the rule here is: a refusal says what the READER must do differently, never
what the system looks like inside. Name no table, no column, no enum value and
no count. The structural distinction - "not tracked" is not "not found" - is
the part worth keeping, and it survives in plain language.

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
        #: A whole sentence, and deliberately in the user's vocabulary rather
        #: than the schema's. An earlier version named the column and then
        #: listed every value in it, which told an engineer asking about Java
        #: the platform inventory of the entire bank - see the note on
        #: disclosure in the module docstring.
        instead=(
            # Same voice as the sentence it follows. "I don't record X. What it
            # does record is Y" changes person mid-thought and reads as two
            # systems talking.
            "I do have the hosting platform an application runs on, if that "
            "helps."
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


#: Estate identifiers, blanked before any attribute term is looked for.
#:
#: EVERY NODE IN THIS ESTATE IS NAMED "<cluster>-NODE-nn", AND "node" IS A
#: RUNTIME LANGUAGE. So "cmh-p234-NODE-01 is not an ideal choice?" - a question
#: about one host - came back "I cannot filter by runtime language", because the
#: hyphens either side of NODE are word boundaries and the boundary-aware match
#: fired on the hostname.
#:
#: Word boundaries were the right fix for "java" inside "javadoc". They cannot
#: help here: the token IS a whole word, it is simply part of an identifier
#: rather than a request. The only reliable separator is what the string
#: denotes, so identifiers are removed before the question is read.
#:
#: THIRD TIME THIS CLASS HAS BITTEN: "app" matched "apply", "report" matched
#: "reporting service", now "node" matches every host we own.
_IDENTIFIER_RE = re.compile(
    r"\b[A-Z]{2,}-[A-Z0-9-]+\b"          # APP-CRM, APP-RISK-WORKER1135
    r"|\b[a-z]{3}-p?\d+(?:-node-\d+)?\b"  # cmh-p234, cmh-p234-NODE-01, atl-03
    r"|\b(?:INC|CHG|PRB)\d+\b",
    re.IGNORECASE,
)


def unmodelled_attribute(query: str) -> UnmodelledAttribute | None:
    """The attribute this query filters on that the CMDB does not record.

    IDENTIFIERS ARE STRIPPED FIRST. A cluster code, a node name or an incident
    number is a thing being asked ABOUT, never a filter being asked FOR, so an
    attribute term inside one is never a request for that attribute.
    """
    lowered = _IDENTIFIER_RE.sub(" ", query).lower()
    for attribute in UNMODELLED_ATTRIBUTES:
        for term in attribute.terms:
            # Word boundaries: "java" must not match inside "javadoc", and
            # ".net" must not match inside "subnet.network".
            if re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", lowered):
                return attribute
    return None


def has_placement_intent(query: str) -> bool:
    return bool(_PLACEMENT_INTENT_RE.search(query))


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
            # PLAIN, AND NOTHING ABOUT HOW THIS WORKS INSIDE. The previous
            # wording explained our own machinery to the reader - "a limit of
            # what is recorded, not a search that came back empty" describes the
            # difference between a schema gap and an empty result set, which is
            # our concern and not theirs.
            #
            # "rewording will not help" stays, in plain words: it saves somebody
            # trying the same question five ways, which is the one genuinely
            # useful thing this reply can offer.
            f"I don't record {attribute.name}, so I can't filter by it and "
            f"rewording won't help. {attribute.instead}"
        )

    if placement and not has_app_code and not has_quantity:
        parts.append(
            "I can rank candidates across the estate, but I need to know what is being "
            "placed. Either name the application (\"best data centre for APP-CRM\") or "
            "give me its size (\"32 cores, 128 GB RAM and 2 TB storage in production\")."
        )
    elif attribute is not None and placement:
        parts.append(
            "Drop that qualifier and I will rank the candidates for what you named."
        )

    return " ".join(parts) if parts else None
