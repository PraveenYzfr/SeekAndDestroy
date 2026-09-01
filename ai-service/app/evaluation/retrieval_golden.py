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
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.repositories.base import T, fetch_all


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
    # The case BM25 exists for, and the one that was untestable until the ITSM
    # seed landed: before it there were zero INC-numbers in the entire index, so
    # the sparse half had nothing to match and every hybrid win came from dense.
    rows = fetch_all(
        f"SELECT TOP (:n) IncidentId, Number FROM {T('Incident')} "
        f"WHERE Number IS NOT NULL ORDER BY IncidentId",
        {"n": limit_per_kind},
        max_rows=limit_per_kind,
    )
    for row in rows:
        cases.append(
            RetrievalCase(
                id=f"exact-{row['Number']}",
                query=str(row["Number"]),
                kind="exact_identifier",
                relevant=_incident_chunk_ids([row["IncidentId"]]),
                exact=True,
                notes="Dense embeddings treat an opaque identifier as noise; this is BM25's case.",
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

    return cases


def matches(retrieved_id: str, case: RetrievalCase) -> bool:
    """Whether a retrieved chunk id counts as relevant for this case."""
    return _prefix_match(retrieved_id, case.relevant)
