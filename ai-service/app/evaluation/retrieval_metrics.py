"""recall@k, MRR and NDCG@k over a ranked list of retrieved ids.

Pure functions over (retrieved_ids, relevant_ids). No database, no embedder, no
model - so the arithmetic can be tested exactly and a change in the numbers can
only mean retrieval changed, never that the ruler did.

WHY THREE METRICS AND NOT ONE
-----------------------------
They disagree in ways that are informative, and reporting one hides that:

    recall@k   did the right documents come back at all, anywhere in the top k?
               Blind to order. A system that returns the answer at rank 10
               scores the same as one returning it at rank 1.

    MRR        how far down was the FIRST right answer? Only sees one document,
               so it is the right metric for "find me INC1000015" and the wrong
               one for "what happened during the outage", where a hundred
               tickets are relevant and finding one of them is not success.

    NDCG@k     order-sensitive across ALL the relevant documents, with a
               logarithmic discount so rank 1 counts more than rank 10. The
               closest to "was this a good result page".

An identifier lookup that scores MRR 1.0 and NDCG 0.2 has found the exact ticket
and buried its context. That is a real and common failure, and one number would
have called it a success.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalScore:
    query: str
    mode: str
    retrieved: int
    relevant: int
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    k: int
    #: Rank of the first relevant hit, 1-based. None when nothing relevant came
    #: back. Reported alongside MRR because "0.05" is arithmetic and "the first
    #: correct result was 20th" is a diagnosis.
    first_relevant_rank: int | None


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant documents appearing in the top k.

    Denominator is min(len(relevant), k), not len(relevant). With 109 relevant
    chunks and k=10, dividing by 109 caps the achievable score at 0.09 and every
    mode scores near zero - which measures the size of the event, not the
    quality of retrieval. Capping asks the answerable question: of the ten slots
    available, how many hold something relevant.
    """
    if not relevant:
        return 0.0
    top = retrieved[:k]
    hits = sum(1 for doc_id in top if doc_id in relevant)
    return hits / min(len(relevant), k)


def mrr(retrieved: list[str], relevant: set[str]) -> float:
    """Reciprocal rank of the first relevant document. 1.0 means rank 1."""
    for index, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / index
    return 0.0


def first_relevant_rank(retrieved: list[str], relevant: set[str]) -> int | None:
    for index, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return index
    return None


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Normalised discounted cumulative gain over the top k.

    Binary relevance: a chunk either belongs to the answer or does not. Graded
    relevance would need someone to decide that one chunk of an incident is
    twice as relevant as another, and there is no principled basis for that
    here - the ground truth is derived from entity membership, which is a fact,
    not a judgement.

    The ideal DCG is computed over min(len(relevant), k) documents, matching
    recall_at_k's denominator, so a perfect result page scores 1.0 even when
    more relevant documents exist than there are slots to hold them.
    """
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, doc_id in enumerate(retrieved[:k], start=1)
        if doc_id in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def score(query: str, mode: str, retrieved: list[str], relevant: set[str], k: int = 10) -> RetrievalScore:
    return RetrievalScore(
        query=query,
        mode=mode,
        retrieved=len(retrieved),
        relevant=len(relevant),
        recall_at_k=round(recall_at_k(retrieved, relevant, k), 4),
        mrr=round(mrr(retrieved, relevant), 4),
        ndcg_at_k=round(ndcg_at_k(retrieved, relevant, k), 4),
        k=k,
        first_relevant_rank=first_relevant_rank(retrieved, relevant),
    )


def mean(scores: list[RetrievalScore], attribute: str) -> float:
    """Mean of one metric across cases. Unweighted on purpose: every query
    counts once regardless of how many documents are relevant to it, so a single
    hundred-incident event cannot dominate the headline number."""
    if not scores:
        return 0.0
    return round(sum(getattr(s, attribute) for s in scores) / len(scores), 4)
