"""Is this question about the estate at all?

"Who is the best actor in India?" produced an Investigation row, a real model
call, a retrieval against the document index and a full report explaining, at
High confidence, that dal-p056-NODE-01 and three of its neighbours contain no
information about actors. Every step worked correctly. The question should
never have reached any of them.

quick_reply() already stops greetings and vague infrastructure asks before the
graph runs, but its test is inverted for this case::

    looks_infra = any(w in lower for w in _INFRA_INTENT_WORDS)
    if looks_infra and not has_app_code and not has_quantity and short:
        return "I need a bit more to work with..."
    return None

It catches input that *does* look like infrastructure but is too vague, and
lets through input with no infrastructure signal whatsoever - which is exactly
the case that produces the worst output.

WHY THIS IS A SIGNAL CHECK AND NOT A CLASSIFIER
-----------------------------------------------
The obvious alternative is to ask a model whether a question is on-topic. That
would cost a call to decide whether to make a call, it would be
non-deterministic on the demo path, and it would fail exactly when the provider
is rate-limiting - which is when the platform is already under strain.

The cheaper, checkable rule: a question about this estate contains at least one
recognisable thing from this estate. An identifier, a piece of infrastructure
vocabulary, a resource quantity, an environment. Absence of *all* of them is a
strong signal, and it is a signal we can read without asking anyone.

FALSE POSITIVES ARE THE EXPENSIVE ERROR
---------------------------------------
Refusing a real question is much worse than answering an odd one: the engineer
concludes the tool is broken and stops using it, and unlike a bad answer there
is nothing on screen to argue with. So the vocabulary below is deliberately
generous - it includes words that are only sometimes about infrastructure
("memory", "storage", "region", "tier") - and the check fires only when *not one*
signal is present. A question that mentions anything from the domain gets the
benefit of the doubt and goes to the graph.
"""

from __future__ import annotations

import re

#: Identifiers that only exist in this estate. Any of them is conclusive.
_IDENTIFIERS = (
    # Multi-segment: APP-AML-API0044, not just APP-AML. This gate happened to
    # survive the shorter pattern - it only needs any match to decide a query is
    # on-topic - but a pattern that is right for the wrong reason is one
    # refactor away from being wrong.
    re.compile(r"\bAPP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.IGNORECASE),
    # No CL-PROD-01 pattern here on purpose. nodes.py and conversation.py
    # both carry _CLUSTER_CODE_RE for that shape and it has never matched
    # anything: 0 of 256 clusters in the CMDB are named CL-%. The real
    # shapes are atl-03 and cmh-p212, covered by the pattern below and
    # verified against every cluster, application and node code in the estate.
    re.compile(r"\b[a-z]{2,4}-?p?\d{2,4}(?:-NODE-\d{1,3})?\b", re.IGNORECASE),  # cmh-p212, cmh-p212-NODE-05
    re.compile(r"\b(?:INC|PRB|CHG|CTASK|REQ|RITM)\d{6,9}\b", re.IGNORECASE),    # ITSM records
    re.compile(r"\bKB\d{6,8}\b", re.IGNORECASE),
)

#: A number attached to a resource unit - "32 cores", "512 GB RAM", "4 TB".
#: Someone asking for capacity is on topic even with no other vocabulary.
_QUANTITY = re.compile(
    r"\b\d+\s*(?:x\s*)?(?:v?cpus?|cores?|gb|tb|mb|gib|tib|ghz|nodes?|hosts?|servers?|replicas?)\b",
    re.IGNORECASE,
)

#: Domain vocabulary. Wide on purpose - see the false-positive note above. A
#: question containing any single one of these goes to the graph.
#:
#: Inflections are listed explicitly rather than stemmed. Stemming would
#: match more words than intended for a gate whose only failure mode that
#: matters is refusing a real question - "consolidated" was missed by the
#: first version of this list, and adding the form is cheaper and more
#: predictable than adding a stemmer that also matches things nobody checked.
_VOCABULARY = frozenset(
    """
    host hosting hosted place placed placement provision provisioned deploy deployed
    deployment migrate migrated migration move moved relocate relocated
    cluster clusters node nodes server servers host hosts vm vms hypervisor bare-metal baremetal
    zone platform resilience
    capacity headroom utilization utilisation utilized utilised overprovisioned underutilized
    underutilised right-size right-sized rightsize rightsizing rightsized
    consolidate consolidated consolidating consolidation reclaim reclaimed
    scale scaled scaling resize resized
    cpu vcpu core cores memory ram storage disk ssd iops throughput bandwidth network latency
    forecast forecasts forecasting forecasted trend trends growth
    projection projections projected predict predicted
    incident incidents outage severity sev1 sev2 problem change rca root-cause
    application applications app apps workload workloads service services dependency dependencies
    environment production staging test development dr prod nonprod non-production
    tier tier1 tier2 resiliency availability failover redundancy
    datacenter datacentre data-centre dc region location site rack
    estate infrastructure infra cmdb inventory
    kubernetes k8s openshift vmware container containers
    compliance classification restricted confidential internal
    recommendation recommend candidate candidates shortlist eligible rejected score scoring
    """.split()
)

#: Split on anything that is not a word character or a hyphen, so "right-size"
#: and "bare-metal" survive as single tokens and can be matched whole.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")

#: TWO EXAMPLES, NO INVENTORY, AND NO "THIS ESTATE".
#:
#: Three versions of this string have been rejected and the reasons compound
#: rather than replace each other:
#:
#:   The first listed every investigation type with a description and an example.
#:   That was clumsy - a manual delivered at the moment somebody wanted a redirect.
#:
#:   A later one came back as six bullets covering hosting, capacity, right-sizing,
#:   consolidation, forecasts and trade-offs. The objection to that one was
#:   different and sharper: enumerating everything the platform can do is
#:   INFORMATION LEAKAGE. Whoever just asked an off-topic question is the last
#:   person who should be handed a capability inventory, and an internal tool has
#:   no reason to advertise its surface to whoever happens to be typing.
#:
#:   "for this estate" and "about this infrastructure estate" both went the same
#:   way. It is filler - the reader knows what system they are using - and it
#:   reads as a system describing itself rather than answering.
#:
#: What survives: one sentence naming the domain, and two copyable examples. Two
#: rather than three, because the examples teach the SHAPE of a valid question,
#: and two shapes - a placement carrying requirements, and an estate-wide query -
#: do that. A third example is one more line of inventory.
OUT_OF_SCOPE_REPLY = (
    "I answer infrastructure questions only - where to place a workload, what "
    "capacity exists, and why a decision was made.\n\n"
    "Try:\n"
    '  "Where can I host a Tier-1 production Java app needing 32 cores and 128 GB?"\n'
    '  "Which clusters are underutilized?"'
)


def _is_domain_word(token: str) -> bool:
    """Whole-token membership, tolerant of a plural.

    The vocabulary listed some plurals and not others - `cluster clusters`,
    `node nodes`, but `dc` with no `dcs`, `site` with no `sites`, `region` with
    no `regions`. That inconsistency is worse than a short list, because the list
    LOOKS complete: nobody reads it and thinks "the plurals are missing".

    What it cost: "what other DCs?" contained no recognised domain word, so the
    scope guard refused it as a question about something other than this estate.
    Asking which data centres are available is the most infrastructure question
    there is, and it was answered with "I answer infrastructure questions only".

    Stripping a trailing s (and es) fixes the class rather than the instances,
    so the next word added to the vocabulary does not need its plural remembered.
    Safe because this only ever tests membership in a curated set - an
    over-eager stem cannot admit a word that is not in it. "as" stems to "a",
    which is in no vocabulary; "hosts" stems to "host", which is the point.
    """
    word = token.lower()
    if word in _VOCABULARY:
        return True
    if word.endswith("es") and word[:-2] in _VOCABULARY:
        return True
    return word.endswith("s") and word[:-1] in _VOCABULARY


def has_estate_signal(query: str) -> bool:
    """True when the query mentions anything this platform knows about.

    Deliberately permissive: one identifier, one quantity or one domain word is
    enough. The question being *answerable* is the graph's problem; this only
    asks whether it is about the estate at all.
    """
    text = (query or "").strip()
    if not text:
        return False

    if any(pattern.search(text) for pattern in _IDENTIFIERS):
        return True
    if _QUANTITY.search(text):
        return True

    # Whole tokens, not substrings. "actor" contains no domain word, but a
    # substring check would find "app" inside "happening" and "node" inside
    # "anode" - quietly disabling the whole gate in a way no test would notice.
    return any(_is_domain_word(token) for token in _TOKEN.findall(text))


#: Words that say the person is unhappy with the platform rather than asking it
#: something. A CLOSED list, deliberately: this decides whether to answer
#: differently, and a generous matcher would patronise an engineer who typed
#: "this is the wrong cluster" - a precise, ordinary infrastructure statement.
#: Better to miss a frustrated turn and give the plain refusal than to tell a
#: working engineer they seem upset.
_FRUSTRATION_RE = re.compile(
    r"\b("
    r"waste|wasting|useless|pointless|rubbish|garbage|nonsense|"
    r"idiot|stupid|dumb|"
    r"not helpful|unhelpful|no help|"
    r"terrible|awful|horrible|"
    r"give up|giving up|forget it|"
    r"fuck|shit|crap|damn"
    r")\b",
    re.IGNORECASE,
)


def frustration_reply(query: str, prior_query: str | None = None) -> str | None:
    """A reply for someone who is fed up, or None when they are not.

    WHY THIS EXISTS. A real transcript: the engineer typed "you are an idiot !!"
    and got the capability refusal, then "Its waste talking to you" - and the
    platform RAN A FULL INVESTIGATION on it, produced "Investigation Report for:
    Its waste talking to you", invented a finding, and advised them to "obtain
    relevant data or clarify the question". It answered a complaint about being
    useless by being useless again, and filed the insult as a governance record.

    What this does NOT do: guess at emotion with a model. The check is a closed
    word list, and the response is a fixed template. A model asked "is the user
    upset?" finds upset users everywhere, and the failure mode is patronising an
    engineer who is working perfectly happily.

    It also does not apologise repeatedly. One acknowledgement, then the two
    questions that can actually move this forward - what was wrong with the last
    answer, and what to do about it. The previous question is named when there
    is one, because "which answer" is otherwise the first thing they have to
    explain.
    """
    if not _FRUSTRATION_RE.search(query or ""):
        return None

    about = f' about "{prior_query.strip()[:120]}"' if prior_query else ""
    return (
        "That is fair, and I would rather fix it than leave it.\n\n"
        f"The last answer I gave{about} clearly did not land. So that I correct "
        "the right thing rather than guess:\n\n"
        "  - What was wrong with it - a figure that looked incorrect, a cluster "
        "that should not have been offered, or an answer that missed the "
        "question entirely?\n"
        "  - Should I re-run that request with different constraints, or is this "
        "a different question I have been answering badly?\n\n"
        "If it is easier, restate what you need in your own words and I will "
        "start from there - naming an application code or the resources you "
        "need is enough."
    )


def out_of_scope_reply(query: str) -> str | None:
    """The reply for a question this platform has no business answering, or
    None when the question should proceed to the graph."""
    return None if has_estate_signal(query) else OUT_OF_SCOPE_REPLY
