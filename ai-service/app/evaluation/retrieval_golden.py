"""Retrieval ground truth, derived from the database rather than hand-written.

WHY DERIVED
-----------
The corpus is 95,911 chunks. Hand-labelling is not on the table, and a
hand-written set would describe the corpus it was written against - it would
start lying the first time the seed changed, silently, in the direction of
whatever it was written from.

So relevance is defined by membership in an entity, which is a fact the database
already holds: the chunks relevant to "INC1000015" are exactly the chunks of
incident 1000015. That is exact, it costs one query, and it stays true.

WHERE IT IS APPROXIMATE, SAID PLAINLY
-------------------------------------
Two case families are not exact and are labelled `approximate` in the output:

  semantic      "memory exhaustion after failover" has no entity to belong to.
                Ground truth is a LIKE over ShortDescription, which will miss
                incidents phrased differently and catch some that are not really
                about the same thing. It is reproducible and it is honest about
                being a proxy.

  major_event   identified by phrase plus date window rather than an event id.
                An explicit event column in the seed would make these exact;
                until then the window does most of the work and the phrase
                narrows it.

An approximate ground truth still compares modes fairly - dense, sparse and
hybrid all face the same labels - so the dense-vs-sparse-vs-hybrid question is
answerable even where the absolute recall number is soft.

THE CONTROL CASE
----------------
One case, kind `control_prefix_match`, is deliberately unfalsifiable. It is the
same shape as an `exact_identifier` case with one difference: the ticket it names
is cited by nothing, so the only chunks containing the query term are its own
prefixes and exact matching cannot lose.

The real identifier cases are chosen the opposite way - the most-cited tickets in
the corpus - so competing chunks genuinely carry the query term. The gap between
the two is the part of a sparse score that came from retrieval rather than from
the id being printed into the text. It is a reference line, not a result, and it
is excluded from the headline means.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from dataclasses import dataclass, field

from app.repositories.base import T, fetch_all

#: INC1234567. Matched against note text to find cross-references between
#: tickets. The seed writes them in this form and nothing else in the corpus
#: looks like it.
_INC_REF_RE = re.compile(r"\bINC\d{7}\b")


@dataclass
class RetrievalCase:
    id: str
    query: str
    kind: str
    #: Chunk ids that count as correct. Derived, never typed by hand.
    relevant: set[str] = field(default_factory=set)
    #: False when relevance is a proxy rather than entity membership.
    exact: bool = True
    notes: str = ""


@functools.lru_cache(maxsize=1)
def _citation_counts() -> tuple[dict[str, int], dict[str, int]]:
    """How many OTHER incidents cite each ticket number in their notes.

    Returns (cited_by_others, own_number_leaks).

    Done in Python over one bulk read rather than in SQL. The natural query -
    joining Incident to IncidentComment on `Text LIKE '%' + Number + '%'` - is a
    non-sargable predicate evaluated across 89,831 comments per candidate row,
    and it ran past ten minutes without returning. One pass with a regex takes
    seconds. Cached because the callers below both need it.

    The leak count is kept because it is the corpus invariant this whole
    evaluation rests on: if notes start repeating their own ticket number again,
    every identifier case silently becomes unfalsifiable, and the number that
    detects it is this one.
    """
    numbers = {
        r["IncidentId"]: r["Number"]
        for r in fetch_all(
            f"SELECT IncidentId, Number FROM {T('Incident')} WHERE Number IS NOT NULL",
            max_rows=500_000,
        )
    }
    cited: Counter[str] = Counter()
    leaks: Counter[str] = Counter()
    for row in fetch_all(
        f"SELECT IncidentId, Text FROM {T('IncidentComment')}", max_rows=500_000
    ):
        own = numbers.get(row["IncidentId"])
        # set(): two mentions in one note is one citing ticket, not two.
        for ref in set(_INC_REF_RE.findall(row["Text"] or "")):
            if ref == own:
                leaks[ref] += 1
            else:
                cited[ref] += 1
    return dict(cited), dict(leaks)


def _incident_chunk_ids(incident_ids: list[int]) -> set[str]:
    """Every chunk belonging to these incidents.

    Matches on the id prefix rather than listing chunk kinds, because the
    chunker owns how many chunks an incident produces - a header, one per
    substantive note, a resolution - and this must not have to change when it
    grows another.
    """
    return {f"incident:{i}:" for i in incident_ids}


def _prefix_match(retrieved_id: str, prefixes: set[str]) -> bool:
    return any(retrieved_id.startswith(p) for p in prefixes)


def build_cases(limit_per_kind: int = 3) -> list[RetrievalCase]:
    """The evaluation set, derived from whatever is in the database now."""
    cases: list[RetrievalCase] = []

    # --- exact identifier ---------------------------------------------------
    # BM25's case. Chosen by COMPETITION, not by id order.
    #
    # This used to take the three lowest IncidentIds, and all three turned out to
    # be among the 2,117 incidents no other ticket references. Nothing competed
    # with them, so retrieval could not rank anything above the right answer and
    # the case could not fail. It was measuring that we print the ticket number
    # into every chunk's prefix.
    #
    # A query for INC1008825 is a real test because nine OTHER incidents cite that
    # number in their notes. Those chunks contain the query term too, so the
    # retriever has to put the ticket itself above the tickets discussing it.
    # That is the thing that can go wrong, and now it can show up.
    cited, leaks = _citation_counts()
    if leaks:
        # Loud on purpose. Notes repeating their own number is exactly the
        # condition that made these cases unfalsifiable before.
        print(
            f"  WARNING: {len(leaks)} ticket(s) cite their own number in their own "
            f"notes - identifier cases for those cannot fail."
        )
    by_number = {
        r["Number"]: r["IncidentId"]
        for r in fetch_all(
            f"SELECT IncidentId, Number FROM {T('Incident')} WHERE Number IS NOT NULL",
            max_rows=500_000,
        )
    }
    contested = sorted(
        ((n, c) for n, c in cited.items() if c >= 5 and n in by_number),
        key=lambda pair: (-pair[1], pair[0]),
    )[:limit_per_kind]
    for number, citations in contested:
        cases.append(
            RetrievalCase(
                id=f"exact-{number}",
                query=str(number),
                kind="exact_identifier",
                relevant=_incident_chunk_ids([by_number[number]]),
                exact=True,
                notes=(
                    f"{citations} other incidents cite this number in their notes, so "
                    "those chunks carry the query term and compete. Falsifiable: the "
                    "retriever can rank a citing ticket above the ticket itself."
                ),
            )
        )

    # --- host / cluster by name --------------------------------------------
    # Separates the two halves: a cluster code is an opaque token to an embedder
    # but an exact term to BM25.
    rows = fetch_all(
        f"SELECT TOP (:n) c.ClusterId, c.ClusterCode, COUNT(i.IncidentId) AS Cnt "
        f"FROM {T('InfrastructureCluster')} c "
        f"JOIN {T('Incident')} i ON i.ClusterId = c.ClusterId "
        f"GROUP BY c.ClusterId, c.ClusterCode HAVING COUNT(i.IncidentId) BETWEEN 5 AND 40 "
        f"ORDER BY COUNT(i.IncidentId) DESC",
        {"n": limit_per_kind},
        max_rows=limit_per_kind,
    )
    for row in rows:
        incidents = fetch_all(
            f"SELECT IncidentId FROM {T('Incident')} WHERE ClusterId = :cid",
            {"cid": row["ClusterId"]},
            max_rows=500,
        )
        cases.append(
            RetrievalCase(
                id=f"host-{row['ClusterCode']}",
                query=f"what happened on {row['ClusterCode']}",
                kind="host_by_name",
                relevant=_incident_chunk_ids([r["IncidentId"] for r in incidents]),
                exact=True,
                notes=f"{row['Cnt']} incidents on this cluster.",
            )
        )

    # --- semantic, no identifier -------------------------------------------
    # APPROXIMATE. There is no entity to belong to, so the label is a LIKE and
    # will both miss and over-collect. Stated rather than hidden.
    for phrase, query in (
        ("%memory%", "memory exhaustion after failover"),
        ("%CPU saturation%", "sustained high processor load on a cluster"),
    ):
        rows = fetch_all(
            f"SELECT TOP 200 IncidentId FROM {T('Incident')} WHERE ShortDescription LIKE :p",
            {"p": phrase},
            max_rows=200,
        )
        if rows:
            cases.append(
                RetrievalCase(
                    id=f"semantic-{phrase.strip('%').replace(' ', '-').lower()}",
                    query=query,
                    kind="semantic",
                    relevant=_incident_chunk_ids([r["IncidentId"] for r in rows]),
                    exact=False,
                    notes="Ground truth is a phrase match, not entity membership - a proxy.",
                )
            )

    # --- major events -------------------------------------------------------
    # APPROXIMATE, by phrase plus date window. These span clusters regardless of
    # load and are findable only by time and language, which makes them the
    # sharpest test of whether retrieval works at all.
    for label, phrase in (
        ("ddos", "Volumetric attack traffic%"),
        ("hypervisor", "Workload disruption during emergency%"),
    ):
        rows = fetch_all(
            f"SELECT TOP 300 IncidentId FROM {T('Incident')} WHERE ShortDescription LIKE :p",
            {"p": phrase},
            max_rows=300,
        )
        if len(rows) >= 20:
            cases.append(
                RetrievalCase(
                    id=f"event-{label}",
                    query={
                        "ddos": "volumetric attack saturating ingress across the estate",
                        "hypervisor": "emergency hypervisor patching disrupted workloads",
                    }[label],
                    kind="major_event",
                    relevant=_incident_chunk_ids([r["IncidentId"] for r in rows]),
                    exact=False,
                    notes=f"{len(rows)} incidents, spanning clusters regardless of load.",
                )
            )

    # --- CONTROL: deliberately unfalsifiable ---------------------------------
    # Same query shape as exact_identifier, one difference: this ticket is cited
    # by NOTHING. No other chunk in the corpus contains its number, so the only
    # documents carrying the query term are its own - via the prefix we print on
    # every chunk. Exact matching wins by construction and cannot not win.
    #
    # It is a reference line, not a result. The gap between it and the real
    # identifier cases is the part of the score that came from retrieval rather
    # than from string matching. On its own, a sparse score of 0.95 reads as
    # evidence; beside a control that also scores 0.95, it reads correctly.
    #
    # Kept rather than deleted, on seekanddestroy-e7's suggestion. Deleting the
    # rigged case would have removed the evidence that the problem existed.
    uncited = sorted(n for n in by_number if n not in cited)
    if uncited:
        number = uncited[0]
        cases.append(
            RetrievalCase(
                id=f"control-uncited-{number}",
                query=str(number),
                kind="control_prefix_match",
                relevant=_incident_chunk_ids([by_number[number]]),
                exact=True,
                notes=(
                    f"CONTROL - cannot fail. None of the {len(by_number):,} tickets cite "
                    "this one, so the only chunks carrying the query term are its own "
                    "prefixes. Measures string matching, not retrieval. The ceiling."
                ),
            )
        )

    # --- recurrence: a problem and its incidents ----------------------------
    # EXACT, via sad.Incident.ProblemId. Labelled from the foreign key rather
    # than by phrase, because the link is a fact and a LIKE is a guess.
    #
    # Skipped entirely when nothing is linked. The column existed with 0 of
    # 10,000 rows populated for a while, and a case built on that would have
    # scored every mode at zero and read as a retrieval failure rather than as
    # missing ground truth.
    rows = fetch_all(
        f"SELECT TOP (:n) p.ProblemId, p.Number, p.ShortDescription, COUNT(i.IncidentId) AS Cnt "
        f"FROM {T('Problem')} p JOIN {T('Incident')} i ON i.ProblemId = p.ProblemId "
        f"GROUP BY p.ProblemId, p.Number, p.ShortDescription "
        f"HAVING COUNT(i.IncidentId) >= 5 ORDER BY COUNT(i.IncidentId) DESC",
        {"n": limit_per_kind},
        max_rows=limit_per_kind,
    )
    for row in rows:
        incidents = fetch_all(
            f"SELECT IncidentId FROM {T('Incident')} WHERE ProblemId = :pid",
            {"pid": row["ProblemId"]},
            max_rows=1000,
        )
        relevant = _incident_chunk_ids([r["IncidentId"] for r in incidents])
        # The problem record itself is part of the answer: "has this happened
        # before" is answered by the problem AND its incidents co-retrieving.
        relevant.add(f"problem:{row['ProblemId']}:")
        cases.append(
            RetrievalCase(
                id=f"recurrence-{row['Number']}",
                query=str(row["ShortDescription"] or row["Number"]),
                kind="recurrence",
                relevant=relevant,
                exact=True,
                notes=f"{row['Cnt']} incidents share this problem record.",
            )
        )

    return cases


def matches(retrieved_id: str, case: RetrievalCase) -> bool:
    """Whether a retrieved chunk id counts as relevant for this case."""
    return _prefix_match(retrieved_id, case.relevant)
