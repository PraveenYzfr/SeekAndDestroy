"""BM25 sparse vectors, for the half of retrieval dense embeddings are bad at.

A dense embedding of "cmh-p212" is not meaningfully closer to the document
containing that host than to any other host's document - the model has no
reason to treat an opaque identifier as anything but noise, and the whole
corpus of hostnames occupies roughly the same region of the space. Ask for
"incidents on cmh-p212" and dense retrieval returns incidents that *read*
similar, which is not the same question.

Exact-token matching is what BM25 is for, and this estate is full of exact
tokens: INC1005432, PRB0040118, CHG0030291, cmh-p212, APP-PAYMENTS,
KB5041234. Those are precisely the terms an engineer types.

Written here rather than pulled from fastembed for the same reason
HttpChatModel is raw httpx: fastembed downloads model weights at runtime,
which means a container that cannot start without network access and an image
that carries hundreds of megabytes for a job that is arithmetic over token
counts.

TOKENISATION IS THE DESIGN
--------------------------
The default "split on non-alphanumeric" would destroy every identifier that
makes this worth doing - `cmh-p212` becomes `cmh` + `p212`, and `cmh` then
matches every host in the estate. So identifier-shaped tokens are preserved
whole and *also* emitted in their split form, which lets both "cmh-p212" and
"p212" find the document while keeping the exact match ranked far higher.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

#: ITSM record numbers and infrastructure identifiers, kept whole.
#: ServiceNow numbering is continuous - INC1005432, not INC-1005432 - which is
#: what makes this work: the number survives as a single token instead of
#: splitting into a useless `INC` plus a bare integer.
_IDENTIFIER = re.compile(
    r"\b(?:"
    r"(?:INC|PRB|CHG|CTASK|REQ|RITM)\d{6,9}"      # ITSM records
    r"|KB\d{6,8}"                                   # vendor knowledge articles
    r"|[a-z]{2,4}-?p?\d{2,4}(?:-[A-Za-z]+-\d{1,3})?"  # cmh-p212, cmh-p212-NODE-05
    r"|APP-[A-Z0-9]+"                               # APP-PAYMENTS
    r")\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z0-9]+")

#: Terms carrying no discriminating signal in an ITSM corpus. Kept short on
#: purpose - an aggressive stoplist removes words like "not" and "failed" that
#: genuinely change meaning here.
_STOPWORDS = frozenset(
    """a an the and or of to in on for with is are was were be been being at by
    from this that these those it its as we i you they he she""".split()
)

#: Hashed vocabulary size. Sparse vectors are indexed by integer, so tokens are
#: hashed into this space rather than kept in a growable dictionary that would
#: need to stay in lockstep with the index. 2**20 keeps collisions negligible
#: for a corpus of this size while costing nothing - only non-zero entries are
#: ever stored or transmitted.
VOCAB_SIZE = 1 << 20

# BM25 constants. k1 controls term-frequency saturation, b controls length
# normalisation. These are the standard defaults and there is no reason to tune
# them before there are retrieval metrics to tune against.
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercased terms, with identifiers preserved whole.

    An identifier is emitted twice: once intact and once split. `cmh-p212`
    yields `cmh-p212`, `cmh`, `p212`. The intact form is rare, so BM25 gives it
    a high IDF and an exact query ranks the right document first; the split
    forms still allow a partial query to find it at all.
    """
    lowered = text.lower()
    tokens: list[str] = []
    consumed: list[tuple[int, int]] = []

    for match in _IDENTIFIER.finditer(lowered):
        whole = match.group(0)
        tokens.append(whole)
        tokens.extend(w for w in _WORD.findall(whole) if w != whole)
        consumed.append(match.span())

    # Only the span *outside* every identifier. Scanning the whole string here
    # would re-emit `cmh` and `p212` a second time, doubling their term
    # frequency and inflating average_length for identifier-heavy documents -
    # which then skews BM25's length normalisation against documents that
    # happen to mention fewer hostnames.
    for match in _WORD.finditer(lowered):
        start, end = match.span()
        if any(lo <= start and end <= hi for lo, hi in consumed):
            continue
        word = match.group(0)
        if word not in _STOPWORDS and len(word) > 1:
            tokens.append(word)

    return tokens


def _index_of(token: str) -> int:
    # blake2b rather than hash(): Python's string hash is randomised per
    # process (PYTHONHASHSEED), so indexing and querying in different processes
    # would map the same token to different slots and the sparse half would
    # silently return nothing.
    import hashlib

    return int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16) % VOCAB_SIZE


@dataclass
class BM25Stats:
    """Corpus statistics needed to weight a query. Computed at index time and
    persisted, because IDF is a property of the corpus and a query encoded
    without it is just term frequency - which ranks common words highest."""

    document_frequency: dict[int, int] = field(default_factory=dict)
    document_count: int = 0
    average_length: float = 0.0

    def to_dict(self) -> dict:
        return {
            "df": {str(k): v for k, v in self.document_frequency.items()},
            "n": self.document_count,
            "avgdl": self.average_length,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "BM25Stats":
        return cls(
            document_frequency={int(k): v for k, v in (raw.get("df") or {}).items()},
            document_count=raw.get("n", 0),
            average_length=raw.get("avgdl", 0.0),
        )

    def idf(self, index: int) -> float:
        # BM25's probabilistic IDF, with the +0.5 smoothing that keeps a term
        # appearing in more than half the corpus from going negative.
        df = self.document_frequency.get(index, 0)
        return math.log(1 + (self.document_count - df + 0.5) / (df + 0.5))


def fit(texts: list[str]) -> BM25Stats:
    """Compute corpus statistics over the documents about to be indexed."""
    df: Counter[int] = Counter()
    total_length = 0
    for text in texts:
        tokens = tokenize(text)
        total_length += len(tokens)
        df.update({_index_of(t) for t in tokens})
    count = len(texts) or 1
    return BM25Stats(
        document_frequency=dict(df),
        document_count=len(texts),
        average_length=total_length / count,
    )


def encode_document(text: str, stats: BM25Stats) -> tuple[list[int], list[float]]:
    """Sparse vector for a stored document: BM25 term weights."""
    tokens = tokenize(text)
    if not tokens:
        return [], []
    counts = Counter(_index_of(t) for t in tokens)
    length = len(tokens)
    avgdl = stats.average_length or length

    indices: list[int] = []
    values: list[float] = []
    for index, tf in counts.items():
        # Standard BM25 term saturation and length normalisation. The IDF half
        # is applied at query time rather than here, which is what lets the
        # dot product Qdrant computes reproduce a BM25 score.
        weight = (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * length / avgdl))
        indices.append(index)
        values.append(weight)
    return indices, values


def encode_query(text: str, stats: BM25Stats) -> tuple[list[int], list[float]]:
    """Sparse vector for a query: IDF only.

    Splitting the BM25 formula across document and query encoding is
    deliberate. Qdrant scores sparse vectors with a dot product, so
    ``document_weight * query_idf`` summed over shared terms *is* the BM25
    score. Putting IDF on the query side also means a corpus statistics change
    does not invalidate stored vectors.
    """
    tokens = tokenize(text)
    if not tokens:
        return [], []
    counts = Counter(_index_of(t) for t in tokens)
    indices: list[int] = []
    values: list[float] = []
    for index, tf in counts.items():
        idf = stats.idf(index)
        if idf <= 0:
            continue
        indices.append(index)
        values.append(idf * tf)
    return indices, values


def merge(existing: BM25Stats, texts: list[str]) -> BM25Stats:
    """Fold newly indexed documents into statistics that already exist.

    ``index_all`` clears the collection and then indexes the whole corpus in one
    call, so it takes the exact ``fit`` path. The incremental entry points -
    ``reindex_application``, ``reindex_cluster`` - upsert one document at a time
    into a corpus whose statistics are already established, and re-fitting from a
    single document would throw away the corpus and leave every IDF meaningless.

    KNOWN DRIFT, stated rather than hidden: re-indexing a document that is
    already present counts it a second time in both ``document_count`` and the
    document frequency of its terms. The point id is derived from the document
    id, so the *point* is replaced correctly - it is only the statistics that
    over-count. On a corpus of a few thousand documents a handful of single-
    document reindexes moves IDF by a fraction of a percent, and it cannot
    compound indefinitely because ``index_all`` clears and re-fits exactly.
    Correcting it properly means reading the old text back to subtract its
    contribution, which is a round-trip per document for an error smaller than
    the one already accepted by hashing terms into a fixed vocabulary.
    """
    df: Counter[int] = Counter(existing.document_frequency)
    total_length = existing.average_length * existing.document_count
    for text in texts:
        tokens = tokenize(text)
        total_length += len(tokens)
        df.update({_index_of(t) for t in tokens})
    count = existing.document_count + len(texts)
    return BM25Stats(
        document_frequency=dict(df),
        document_count=count,
        average_length=total_length / (count or 1),
    )
