"""BM25 sparse retrieval - the half dense embeddings are bad at.

These run without Qdrant and without a database. The store integration lives in
test_qdrant_store.py, which needs a live server; this file covers the scoring
itself, which is arithmetic and has no excuse for being untested.
"""

from __future__ import annotations

from app.retrieval import sparse


def _score(document: str, query: str, stats: sparse.BM25Stats) -> float:
    """What Qdrant computes: a dot product over shared sparse dimensions.

    Reproducing it here is the point. The BM25 formula is deliberately split
    across encode_document (term saturation) and encode_query (IDF), and that
    split is only correct if the dot product of the two halves reconstitutes a
    BM25 score. A test that called some internal score() would not check that.
    """
    d_idx, d_val = sparse.encode_document(document, stats)
    q_idx, q_val = sparse.encode_query(query, stats)
    document_weights = dict(zip(d_idx, d_val))
    return sum(w * document_weights.get(i, 0.0) for i, w in zip(q_idx, q_val))


class TestTokenize:
    def test_identifiers_survive_whole_and_split(self):
        """The single reason this module exists. Splitting cmh-p212 on the
        hyphen leaves `cmh`, which matches every host in the data centre."""
        tokens = sparse.tokenize("Incident on cmh-p212")
        assert "cmh-p212" in tokens
        assert "cmh" in tokens and "p212" in tokens

    def test_itsm_record_numbers_are_one_token(self):
        tokens = sparse.tokenize("Caused by INC1005432 and PRB0040118")
        assert "inc1005432" in tokens
        assert "prb0040118" in tokens

    def test_stopwords_are_dropped_but_negation_is_not(self):
        """`not` and `failed` change the meaning of an incident record, so the
        stoplist deliberately stops short of a standard English one."""
        tokens = sparse.tokenize("the node was not failed")
        assert "the" not in tokens
        assert "not" in tokens and "failed" in tokens

    def test_identifier_terms_are_not_counted_twice(self):
        """Scanning the whole string for words *and* for identifiers would emit
        cmh and p212 twice, inflating term frequency and skewing the length
        normalisation against documents that mention fewer hosts."""
        assert sparse.tokenize("cmh-p212").count("cmh") == 1

    def test_case_is_normalised(self):
        assert sparse.tokenize("INC1005432") == sparse.tokenize("inc1005432")


class TestStats:
    def test_fit_counts_documents_and_mean_length(self):
        stats = sparse.fit(["alpha beta", "gamma"])
        assert stats.document_count == 2
        assert stats.average_length == 1.5

    def test_rare_terms_outweigh_common_ones(self):
        """IDF is the whole reason statistics are persisted at all."""
        corpus = [f"cluster node memory report {i}" for i in range(50)]
        corpus.append("cluster node memory cmh-p212")
        stats = sparse.fit(corpus)
        rare = stats.idf(sparse._index_of("cmh-p212"))
        common = stats.idf(sparse._index_of("cluster"))
        assert rare > common

    def test_merge_accumulates_rather_than_replacing(self):
        """An incremental reindex must not re-fit from one document - that
        would discard the corpus and leave every IDF meaningless."""
        base = sparse.fit(["alpha beta", "alpha gamma"])
        merged = sparse.merge(base, ["delta"])
        assert merged.document_count == 3
        assert merged.document_frequency[sparse._index_of("alpha")] == 2
        assert merged.document_frequency[sparse._index_of("delta")] == 1

    def test_merge_of_nothing_changes_nothing(self):
        base = sparse.fit(["alpha beta"])
        assert sparse.merge(base, []).to_dict() == base.to_dict()

    def test_stats_survive_a_round_trip(self):
        """They are persisted as JSON in a Qdrant payload, where integer keys
        become strings. from_dict has to put them back."""
        original = sparse.fit(["cmh-p212 memory exhaustion", "alpha beta"])
        restored = sparse.BM25Stats.from_dict(original.to_dict())
        assert restored.document_frequency == original.document_frequency
        assert restored.document_count == original.document_count
        assert restored.average_length == original.average_length


class TestScoring:
    def test_the_document_holding_the_identifier_wins(self):
        """The motivating case from the module docstring: dense embeddings put
        every hostname in roughly the same region, so 'incidents on cmh-p212'
        returns documents that merely read alike. Sparse must not."""
        target = "Memory exhaustion on cmh-p212 after failover"
        others = [
            "Memory exhaustion on cmh-p999 after failover",
            "Memory exhaustion on lhr-p104 after failover",
        ]
        stats = sparse.fit([target, *others])
        best = _score(target, "incidents on cmh-p212", stats)
        assert all(best > _score(o, "incidents on cmh-p212", stats) for o in others)

    def test_a_query_sharing_no_terms_scores_zero(self):
        stats = sparse.fit(["alpha beta gamma"])
        assert _score("alpha beta gamma", "entirely unrelated wording", stats) == 0.0

    def test_empty_text_encodes_to_an_empty_vector(self):
        """Qdrant is handed this verbatim, so it must be a well-formed empty
        pair rather than None."""
        stats = sparse.fit(["alpha"])
        assert sparse.encode_document("", stats) == ([], [])
        assert sparse.encode_query("   ", stats) == ([], [])

    def test_encoding_is_stable_across_processes(self):
        """Token indices are blake2b, not hash(): Python randomises string
        hashing per process, so indexing and querying in different workers
        would map the same token to different slots and the sparse half would
        silently return nothing."""
        assert sparse._index_of("cmh-p212") == 306781

    def test_a_longer_document_is_not_rewarded_for_length(self):
        """BM25's length normalisation. Without it, padding a document with
        repetitions of a term would rank it above a concise exact match."""
        stats = sparse.fit(["cmh-p212 failed", "cmh-p212 " + "filler " * 60])
        concise = _score("cmh-p212 failed", "cmh-p212", stats)
        padded = _score("cmh-p212 " + "filler " * 60, "cmh-p212", stats)
        assert concise > padded
