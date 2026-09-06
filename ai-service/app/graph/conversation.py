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

#: "GIVE ME FROM A DIFFERENT DC" - THE SAME REQUEST WITH ONE CONSTRAINT CHANGED.
#:
#: This is what an engineer says after rejecting a shortlist, and until now
#: nothing here matched it. It names no application, quotes no cluster code,
#: contains no referential pronoun, does not open with "and"/"what about", and
#: is not a question - so looks_like_follow_up returned None, the message became
#: a brand-new investigation, and "different data centre" went to the vector
#: index as a search phrase.
#:
#: What came back was three incidents that happened to mention a dependency in
#: another data centre, narrated as a report. Every fact in it was true and the
#: whole answer was worthless: the engineer asked for somewhere else to put a
#: workload and got an incident history for upstream timeouts.
#:
#: Asking for an ALTERNATIVE is the signal, and it only means anything against a
#: previous result - "another" and "different" are comparatives with nothing to
#: compare to on a first message, which is why this is a follow-up pattern and
#: not a classifier keyword.
_RESCOPE_RE = re.compile(
    r"\b(?:different|another|other|alternative|elsewhere|somewhere\s+else|else)\b",
    re.IGNORECASE,
)

#: Which dimension is being re-scoped, when it is a location. Only used to
#: decide what to EXCLUDE from the re-run - a re-scope that names no dimension
#: still re-runs the placement, it just excludes nothing.
#: dcs?\d* rather than dcs? - real data centre names are Denver-DC1, Atlanta-DC1,
#: and \bdc\b does NOT match inside "DC1" because there is no word boundary
#: between C and 1. So "Show other options, but not in Denver-DC1" - which is
#: what the rejection button in Chat.tsx generates, and the PRIMARY way this
#: feature is reached - contained no word this gate recognised, and the click
#: meant to trigger the whole thing would have excluded nothing, silently.
#:
#: c2 found it by reading what the UI actually sends rather than what the
#: feature is described as doing. The UI wording is now explicit too, but the
#: gate must not depend on a sentence some other file happens to build - a
#: human typing "not in Denver-DC1" deserves the same answer as the button.
#:
#: Plurals folded in for the same reason a9 stemmed the scope vocabulary: a list
#: with dc but not dcs, site but not sites, looks complete and is not.
_LOCATION_WORD_RE = re.compile(
    r"\b(?:dcs?\d*|data\s*cent(?:er|re)s?|sites?|regions?|locations?|zones?|campus)\b",
    re.IGNORECASE,
)

#: A bare prepositional fragment - "in staging?", "with 128 GB RAM". Only a
#: continuation when it is the *whole* message: "in production, which clusters
#: are underutilized?" is a complete question that happens to start with a
#: preposition, and carrying a subject into it would turn a right-sizing
#: question into a placement request for an application nobody mentioned.
_FRAGMENT_RE = re.compile(r"^\s*(?:in|with|without|for|on|at|under|using)\b", re.IGNORECASE)
_FRAGMENT_MAX_WORDS = 5

#: "what other DCs?" is a re-scope; "which clusters are underutilized in
#: another region?" is a question that happens to contain "another". Six
#: words is the line, for the same reason _FRAGMENT_MAX_WORDS exists: a
#: follow-up that leans on context is short, because the context is doing
#: the work the words would otherwise have to do.
_RESCOPE_QUESTION_MAX_WORDS = 6


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
    #: Data centres the PREVIOUS turn had already ruled out, carried forward so
    #: a second "give me another one" does not offer back the site the engineer
    #: rejected first. Read from the same checkpoint state as everything else
    #: here; empty for an ordinary first ask.
    exclude_data_centers: list[str] = field(default_factory=list)

    @property
    def cluster_subject(self) -> Optional[str]:
        """The cluster this conversation is about, if it is about one.

        THE FAILURE THIS EXISTS FOR. An engineer asked "explain me more about
        msp-p194", then followed up with "is it stable enough for a production
        hosting?" and "what are the incidents talk about?". Neither follow-up
        contains the cluster code, and carry_subject had nothing to carry: it
        knows about application codes and capacity requirements, and a cluster
        is neither.

        So retrieval ran on the bare pronoun text. Hybrid search is not at
        fault - given "msp-p194 incidents" it returns msp-p194 documents at the
        top, BM25 is on and the code tokenises to three sparse terms. It was
        never given the code. Dense similarity then matched "stability" and
        "incidents" generally, and returned INC1009430, INC1004913 and
        INC1002631 - all on msp-p204 - plus INC1003924 on dal-p044.

        msp-p194 has ZERO incidents. The platform reported four, said "two",
        then "three", then insisted "the correct count is three, not two". None
        of those numbers was right and the count changed because every turn
        re-retrieved a different top-k for a different query.

        Read from the query that started the thread rather than from the
        candidate list: a right-sizing run names dozens of clusters and none of
        them is the subject, whereas the code the engineer typed is.
        """
        match = _CLUSTER_CODE_RE.search(self.user_query or "")
        return match.group(0) if match else None

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
            exclude_data_centers=list(state.get("exclude_data_centers") or []),
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
    #: Data centres the previous shortlist came from, when this turn asked for a
    #: different one. Empty for every other turn, and empty means no exclusion.
    exclude_data_centers: list[str] = field(default_factory=list)


def is_question(query: str) -> bool:
    """Does this turn ASK something, by its opening word.

    Exported because the scope gate needs it and the distinction is already made
    here. A referential QUESTION - "why was that rejected?" - is a real follow-up
    and must reach the graph. A referential STATEMENT - "its waste talking to
    you" - reaches the same ABOUT_PREVIOUS classification through the bare
    catch-all at the bottom of looks_like_follow_up, and is not a question at
    all.
    """
    return bool(_QUESTION_START_RE.search(query or ""))


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

        # ASKING FOR AN ALTERNATIVE, IN EITHER GRAMMAR.
        #
        #     "give me from a different DC"   an instruction
        #     "what other options?"           a question
        #     "what other DCs?"               a question
        #
        # All three mean the same thing and all three used to fall through to a
        # fresh investigation. The instruction form became a vector search and
        # answered with incident history; the question forms did not even get
        # that far - they reached the scope guard and were told "I answer
        # infrastructure questions only", which is the worst of the three,
        # because asking for other data centres IS an infrastructure question
        # and the platform had the answer sitting in the previous turn.
        #
        # The question form needs a length bound that the instruction form does
        # not. "which clusters are underutilized in another region?" contains
        # "another" and is nonetheless a complete, self-contained query about
        # the estate; carrying a subject into it would turn a right-sizing
        # question into a placement re-run for an application nobody named.
        # A re-scope question is short and has no clause of its own, so the
        # same word bound _FRAGMENT_RE already relies on separates them.
        if _RESCOPE_RE.search(text):
            if not _QUESTION_START_RE.search(text):
                return INHERIT_SUBJECT
            if len(text.split()) <= _RESCOPE_QUESTION_MAX_WORDS:
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
        # THE PLATFORM ASKED A QUESTION AND THEN COULD NOT ACCEPT THE ANSWER.
        #
        #     user  get me a best server
        #     SAD   I need a bit more to work with. Either name the application
        #           or give me the resources it needs.
        #     user  for java app
        #     SAD   I don't have an earlier result in this conversation to refer
        #           back to.
        #     user  its a brand new java app
        #     SAD   I don't have an earlier result in this conversation to refer
        #           back to.
        #
        # A closed loop with no way out except abandoning the thread and typing
        # one fully-formed sentence. The engineer answered correctly, twice, and
        # was told the platform had no memory of the question it had just asked.
        #
        # The cause: that clarifying reply comes from quick_reply, which
        # deliberately creates NO Investigation row - "hi" should not produce
        # one. So prior is None, "for java app" resolves as INHERIT_SUBJECT with
        # nothing to inherit, and _NO_REFERENT fires. "No prior INVESTIGATION"
        # and "no prior CONVERSATION" are different facts and this conflated
        # them.
        #
        # RECALL KEEPS THE REFUSAL, because there it is true and useful: "show me
        # those options again" as a first message has no options to show, and
        # saying so is the whole point of _NO_REFERENT.
        #
        # Everything else falls through as an ordinary query. That is not a
        # guess about intent - it is declining to assert a referent that does
        # not exist. quick_reply and capability_reply then answer it properly:
        # "for java app" gets the ask for an application or a size, and "its a
        # brand new java app" reaches the capability refusal that explains this
        # CMDB records a hosting platform and not a language. Both are the
        # honest answer to what was typed.
        if kind == RECALL:
            return Resolution(kind=RECALL, resolved_query=query, prior=None, reply=_NO_REFERENT)
        return Resolution(kind=None, resolved_query=query, prior=None)

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
        return Resolution(
            kind=INHERIT_SUBJECT, resolved_query=carried, prior=prior,
            exclude_data_centers=excluded_data_centers(query, prior),
        )

    # ABOUT_PREVIOUS GROUNDS ON THE PREVIOUS RUN'S EVIDENCE - AND A QUESTION HAS
    # NONE.
    #
    # The design is sound where it applies: "why was that rejected?" is answered
    # from the candidate list of the run the engineer is looking at, and a vector
    # search has no way of knowing which run that was. But when the previous turn
    # was itself a Question, there is no candidate list, prior_context_docs comes
    # back empty, and retrieval falls through to a similarity search over the bare
    # follow-up text.
    #
    # That is how a conversation about msp-p194 - a cluster with ZERO incidents -
    # came to be told about four incidents belonging to msp-p204 and dal-p044.
    # "is it stable enough for production hosting?" contains no cluster code, so
    # the search matched on "stability" and "production" and returned whatever
    # incident prose was nearest.
    #
    # So the subject is carried here too. It costs nothing when the previous turn
    # DID produce evidence - the query simply names the thing it was already
    # about - and it gives retrieval the one token that discriminates msp-p194
    # from msp-p204 when it did not. Hybrid search resolves the code correctly
    # once it is given it; BM25 tokenises msp-p194 to three sparse terms and
    # ranks its documents top. It was never the retriever that failed.
    carried = carry_subject(query, prior)
    if carried is None:
        # NOT via carry_subject, deliberately. That helper is shared with
        # INHERIT_SUBJECT, and giving it a cluster changed CLASSIFICATION as
        # well as wording: "what about staging?" stopped falling through to a
        # question about the previous turn and became a fresh placement-shaped
        # run scoped to a cluster that already exists. That is the same drift
        # this fix exists to stop, arriving through the fix itself, and
        # test_a_continuation_with_no_subject_to_inherit_becomes_a_question_
        # about_the_previous_turn caught it.
        #
        # Appended only here, where the kind is already decided, so the cluster
        # can reach retrieval without moving any turn between branches.
        cluster = prior.cluster_subject
        if cluster:
            carried = f"{query} (about cluster {cluster})"
    return Resolution(
        kind=ABOUT_PREVIOUS, resolved_query=carried or query, prior=prior
    )


def excluded_data_centers(query: str, prior: PriorInvestigation) -> list[str]:
    """Which data centres this turn is asking to move away from.

    Gated on _LOCATION_WORD_RE rather than on _RESCOPE_RE, and the difference
    matters. "what other options?" is a re-scope with no dimension named - the
    engineer wants a different ANSWER, not necessarily a different site - and
    quietly excluding a data centre nobody mentioned would drop candidates for a
    reason never stated and never shown. "what other DCs?" names the dimension,
    so the exclusion is what was asked for.

    TWO CASES, IN PRIORITY ORDER - AND THE SECOND ONE USED TO BE WRONG.

    1. This turn's own text NAMES a data centre this run actually recorded
       (either eligible or rejected). Exact substring match against the real
       recorded value, never a guessed shape ("City-DCn") - the rejection-flow
       button in Chat.tsx always produces text naming the one site the
       engineer actually objected to ("...not in the Atlanta-DC1 data
       center."), and that is the PRIMARY way this feature is reached. When a
       specific site is named, only that site is excluded.

    2. No data centre is named ("give me from a different DC" - Praveen's own
       original phrasing). The fallback is the site of the RECOMMENDATION
       that was actually made - the top-ranked eligible candidate - and not
       every site that held an eligible one. See _offered_data_center; the
       broader version emptied the shortlist on the headline query.
       Never the rejected pool either. ``prior.candidate_scores`` is eligible + REJECTED, from
       app.services.placement.discover_candidate_clusters's full scan of
       every cluster matching the requirement's environment and platform -
       for a common combination that can be most of the estate, most of it
       rejected for reasons (wrong tier, wrong classification, no capacity)
       that have nothing to do with location. A FIRST VERSION OF THIS
       FUNCTION USED THE WHOLE POOL, LIVE-VERIFIED BROKEN: excluding
       "Atlanta-DC1" on a 3-eligible/2-DC shortlist for APP-CRM excluded all
       eight data centres in the estate and returned zero candidates, every
       time, for any request - because some rejected cluster from nearly
       every data centre always turned up in that pool.

    Returns [] when neither case applies - the previous turn had no eligible
    candidates at all, so there is nothing honest to move away from. []
    means no exclusion the whole way down to the SQL, where an empty NOT IN
    would exclude everything.
    """
    if not _LOCATION_WORD_RE.search(query):
        return []
    text = query.lower()
    known = {str(c["data_center"]) for c in prior.candidate_scores if c.get("data_center")}
    named = {dc for dc in known if dc.lower() in text}
    this_turn = sorted(named) if named else _offered_data_center(prior)

    #  EXCLUSIONS ACCUMULATE ACROSS THE CONVERSATION.
    #
    #  Each turn used to be derived from its immediate predecessor alone, and
    #  asking twice therefore walked in a circle. Live-verified on production,
    #  one conversation, four turns:
    #
    #      1. find hosting, critical, 32 cores   -> cmh-p225  (Columbus-DC1)
    #      2. "what other options?"              -> cmh-p225  (Columbus-DC1)
    #      3. "give me from a different DC"      -> excludes Columbus, offers
    #                                               phx-p167 (Phoenix-DC1)
    #      4. "what other DCs?"                  -> excludes PHOENIX ONLY, and
    #                                               offers cmh-p225 back
    #
    #  Turn 4 handed the engineer the exact site they had ruled out one turn
    #  earlier, and listed Columbus-DC1 under "available_data_centers" as
    #  though it were a fresh choice. A re-scope that returns you to what you
    #  already rejected is worse than refusing: it reads as a genuine second
    #  answer.
    #
    #  So the prior turn's own exclusions are unioned in. The risk this runs
    #  is the one that has already broken this feature twice - excluding so
    #  much that the shortlist empties - and it is handled rather than
    #  avoided: the set only ever grows by the ONE site actually offered per
    #  turn, and when it finally covers everything with capacity,
    #  refinement.data_center_choice reports has_genuine_alternative:false and
    #  the caller says so plainly. An honest "you have ruled out every site
    #  that fits" is the correct end of this conversation. Silently starting
    #  it over is not.
    carried = [dc for dc in prior.exclude_data_centers if dc]
    return sorted(set(this_turn) | set(carried))


def _offered_data_center(prior: PriorInvestigation) -> list[str]:
    """The data centre of the recommendation that was actually made.

    NOT every data centre that happened to hold an eligible candidate. That was
    the second version of this and it is still too broad - live-verified on
    APP-CRM, whose eligible shortlist is:

        rank 1  atl-03    Atlanta-DC1  91.38   <- the recommendation
        rank 2  den-03    Denver-DC1   85.30
        rank 3  den-p096  Denver-DC1   81.84

    Excluding every eligible data centre removes Atlanta AND Denver, which is
    the whole shortlist, so "give me from a different DC" returned zero
    candidates - Praveen's own phrasing, and the headline case for the feature.

    The engineer rejected ONE recommendation. They were shown atl-03; ranks 2
    and 3 were never offered to them and there is nothing to move away from in
    Denver. Excluding it discards the genuine next set - which is precisely what
    Praveen asked for: "should have given me the genuine next set".

    So the exclusion is the top-ranked eligible candidate's site, and the answer
    to "a different DC" becomes den-03 and den-p096 rather than nothing.

    Ranked by ``rank`` where the engine set one, falling back to score, because
    a candidate list that arrives unranked must not silently pick an arbitrary
    row as "the recommendation" - the ordering IS the claim about which one was
    offered.
    """
    eligible = [
        c for c in prior.candidate_scores
        if c.get("data_center") and c.get("eligibility_status") == "Eligible"
    ]
    if not eligible:
        return []

    def _order(candidate: dict) -> tuple[int, float]:
        rank = candidate.get("rank")
        score = candidate.get("overall_score")
        return (
            int(rank) if rank is not None else 1_000_000,
            -float(score) if score is not None else 0.0,
        )

    return [str(min(eligible, key=_order)["data_center"])]


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
        f"It shortlisted {eligible} recommended and {rejected} not-recommended clusters.",
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
        f"These are the same {len(eligible)} recommended options from investigation "
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
