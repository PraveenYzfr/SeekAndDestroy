"""The ruler, tested exactly.

These are pure functions over two lists, so every expected value here is
hand-computable. That is the point: when the retrieval numbers move, it has to
be because retrieval moved, and never because the measurement did.
"""

from __future__ import annotations

import math

import pytest

from app.evaluation import retrieval_metrics as m


class TestRecallAtK:
    def test_everything_relevant_in_the_top_k(self):
        assert m.recall_at_k(["a", "b", "c"], {"a", "b"}, k=10) == 1.0

    def test_nothing_relevant(self):
        assert m.recall_at_k(["x", "y"], {"a"}, k=10) == 0.0

    def test_beyond_k_does_not_count(self):
        """The whole meaning of @k: a correct answer on page four is not a
        correct answer."""
        assert m.recall_at_k(["x", "x", "x", "a"], {"a"}, k=3) == 0.0

    def test_the_denominator_is_capped_at_k(self):
        """With 109 relevant chunks and k=10, dividing by 109 caps the best
        possible score at 0.09 and every mode scores near zero - which measures
        the size of the event, not the quality of retrieval. Capping asks the
        answerable question: of the ten slots, how many are relevant."""
        retrieved = [f"r{i}" for i in range(10)]
        relevant = {f"r{i}" for i in range(100)}
        assert m.recall_at_k(retrieved, relevant, k=10) == 1.0

    def test_no_relevant_documents_scores_zero_not_one(self):
        """A case with no ground truth is a broken case, not a perfect score."""
        assert m.recall_at_k(["a"], set(), k=10) == 0.0


class TestMRR:
    def test_first_position(self):
        assert m.mrr(["a", "b"], {"a"}) == 1.0

    def test_third_position(self):
        assert m.mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_nothing_found(self):
        assert m.mrr(["x"], {"a"}) == 0.0

    def test_it_only_sees_the_first_hit(self):
        """Which is why it is the right metric for "find me INC1000015" and the
        wrong one for "what happened during the outage" - finding one of a
        hundred relevant tickets is not success there."""
        assert m.mrr(["a", "b", "c"], {"a"}) == m.mrr(["a", "b", "c"], {"a", "b", "c"})


class TestNDCG:
    def test_perfect_order_scores_one(self):
        assert m.ndcg_at_k(["a", "b"], {"a", "b"}, k=10) == pytest.approx(1.0)

    def test_reversed_order_scores_less_than_perfect(self):
        """The property recall lacks. Both return the same documents; only one
        put them where a reader would see them."""
        good = m.ndcg_at_k(["a", "x", "y"], {"a"}, k=10)
        bad = m.ndcg_at_k(["x", "y", "a"], {"a"}, k=10)
        assert good > bad

    def test_the_discount_is_logarithmic(self):
        """Hand-computed: one relevant document at rank 3 gives
        DCG = 1/log2(4) = 0.5, IDCG = 1/log2(2) = 1.0, so NDCG = 0.5."""
        assert m.ndcg_at_k(["x", "y", "a"], {"a"}, k=10) == pytest.approx(1.0 / math.log2(4))

    def test_more_relevant_than_slots_can_still_score_one(self):
        retrieved = [f"r{i}" for i in range(5)]
        relevant = {f"r{i}" for i in range(50)}
        assert m.ndcg_at_k(retrieved, relevant, k=5) == pytest.approx(1.0)


class TestScoreAndMean:
    def test_first_relevant_rank_is_reported(self):
        """"MRR 0.05" is arithmetic; "the first correct result was 20th" is a
        diagnosis."""
        s = m.score("q", "hybrid", ["x", "y", "a"], {"a"})
        assert s.first_relevant_rank == 3

    def test_first_relevant_rank_is_none_when_nothing_matched(self):
        assert m.score("q", "hybrid", ["x"], {"a"}).first_relevant_rank is None

    def test_the_mean_is_unweighted(self):
        """Every query counts once regardless of how many documents are relevant
        to it, so one hundred-incident event cannot dominate the headline."""
        scores = [
            m.score("q1", "hybrid", ["a"], {"a"}),
            m.score("q2", "hybrid", ["x"], {"b"}),
        ]
        assert m.mean(scores, "mrr") == 0.5

    def test_mean_of_nothing_is_zero_not_an_error(self):
        assert m.mean([], "mrr") == 0.0


class TestEntityCollapsing:
    """Scoring is per entity, not per chunk - "did it find the right incident",
    not "did it find the fourth work note of the right incident"."""

    def test_chunks_collapse_to_their_entity(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "eval_retrieval",
            __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "eval_retrieval.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.entity_of("incident:1000015:note:3:0") == "incident:1000015:"
        assert module.entity_of("change:42:backout:0") == "change:42:"
        assert module.entity_of("problem:7:rootcause:0") == "problem:7:"

    def test_dedupe_keeps_first_occurrence_rank(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "eval_retrieval",
            __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "eval_retrieval.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Eight chunks of one incident must not outrank three different ones:
        # without collapsing, a mode that chunk-clusters tightly would score
        # higher than one that actually retrieves more of the right entities.
        collapsed = module.dedupe_to_entities(
            ["incident:1:h:0", "incident:1:note:1:0", "incident:2:h:0", "incident:1:note:2:0"]
        )
        assert collapsed == ["incident:1:", "incident:2:"]
