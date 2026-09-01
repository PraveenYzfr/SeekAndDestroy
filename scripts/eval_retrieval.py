"""Measure retrieval: dense vs sparse vs hybrid, on one index.

    python scripts/eval_retrieval.py                    # all three modes
    python scripts/eval_retrieval.py --mode hybrid
    python scripts/eval_retrieval.py --json
    python scripts/eval_retrieval.py --baseline         # write the committed baseline

Retrieval was completely unmeasured: graders.py scores number_fidelity and
entity_fidelity, both about generation, and nothing said whether the retriever
found the right documents. Hybrid dense+BM25 with RRF shipped on an argument.
This is the number that says whether it earns its place.

ONE INDEX, THREE MODES
----------------------
SAD_RETRIEVAL__SEARCH_MODE is a query-time setting, so all three run against the
same collection with no reindex between them. That was a deliberate decision
when hybrid was built - baking the mode into the index would have meant
re-embedding the whole corpus per mode, which is how a comparison ends up never
being made.

SCORED PER ENTITY, NOT PER CHUNK
--------------------------------
A retrieved chunk id like incident:1000015:note:3:0 is collapsed to its entity,
incident:1000015:, keeping first-occurrence order. Two reasons:

  * The question an engineer asks is "did it find the right incident", not "did
    it find the fourth work note of the right incident".
  * Without it, a mode that returns eight chunks of one correct incident would
    outscore one that returns eight different correct incidents. That is
    backwards for every case here, and it would flatter whichever mode happens
    to chunk-cluster more tightly rather than the one that retrieves better.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ai-service"))

BASELINE = REPO_ROOT / "ai-service" / "app" / "evaluation" / "retrieval_baseline.json"

MODES = ("dense", "sparse", "hybrid")


def entity_of(chunk_id: str) -> str:
    """incident:1000015:note:3:0 -> incident:1000015:

    Everything up to and including the second colon. The chunker owns what comes
    after; this must not need changing when it grows another chunk kind.
    """
    parts = chunk_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}:"
    return chunk_id


def dedupe_to_entities(chunk_ids: list[str]) -> list[str]:
    """Collapse to entities, keeping the rank of each entity's first chunk."""
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk_id in chunk_ids:
        entity = entity_of(chunk_id)
        if entity not in seen:
            seen.add(entity)
            ordered.append(entity)
    return ordered


def run_mode(mode: str, cases, k: int, top_k: int):
    from app.config import get_settings
    from app.evaluation import retrieval_metrics
    from app.retrieval import vector_store

    os.environ["SAD_RETRIEVAL__SEARCH_MODE"] = mode
    get_settings.cache_clear()
    vector_store.reset_vector_store_cache()
    store = vector_store.get_vector_store()

    scores = []
    for case in cases:
        try:
            hits = store.search(case.query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {case.id} [{mode}] search failed: {exc}", file=sys.stderr)
            continue
        retrieved = dedupe_to_entities([h.document.id for h in hits])
        scores.append(retrieval_metrics.score(case.query, mode, retrieved, case.relevant, k=k))
    return scores


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, help="run a single mode")
    parser.add_argument("--k", type=int, default=10, help="cutoff for recall@k and NDCG@k")
    parser.add_argument("--top-k", type=int, default=30, help="how many chunks to retrieve per query")
    parser.add_argument("--cases-per-kind", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="write the committed baseline file")
    args = parser.parse_args()

    from app.evaluation import retrieval_golden, retrieval_metrics

    cases = retrieval_golden.build_cases(limit_per_kind=args.cases_per_kind)
    if not cases:
        print("no cases could be derived - is the ITSM corpus loaded?", file=sys.stderr)
        return 2

    modes = [args.mode] if args.mode else list(MODES)
    results = {m: run_mode(m, cases, args.k, args.top_k) for m in modes}

    summary = {
        m: {
            "recall_at_k": retrieval_metrics.mean(s, "recall_at_k"),
            "mrr": retrieval_metrics.mean(s, "mrr"),
            "ndcg_at_k": retrieval_metrics.mean(s, "ndcg_at_k"),
            "cases": len(s),
        }
        for m, s in results.items()
    }

    if args.json or args.baseline:
        payload = {
            "k": args.k,
            "top_k": args.top_k,
            "cases": [
                {"id": c.id, "kind": c.kind, "query": c.query, "relevant": len(c.relevant), "exact": c.exact}
                for c in cases
            ],
            "summary": summary,
            "per_case": {
                m: [
                    {
                        "query": s.query, "recall_at_k": s.recall_at_k, "mrr": s.mrr,
                        "ndcg_at_k": s.ndcg_at_k, "first_relevant_rank": s.first_relevant_rank,
                    }
                    for s in s_list
                ]
                for m, s_list in results.items()
            },
        }
        if args.baseline:
            BASELINE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"baseline written to {BASELINE}")
        else:
            print(json.dumps(payload, indent=2))
        return 0

    print()
    print(f"  {len(cases)} cases, k={args.k}, retrieving {args.top_k} chunks per query")
    print()
    print(f"  {'mode':<10}{'recall@k':>10}{'MRR':>8}{'NDCG@k':>9}")
    for m in modes:
        s = summary[m]
        print(f"  {m:<10}{s['recall_at_k']:>10.3f}{s['mrr']:>8.3f}{s['ndcg_at_k']:>9.3f}")

    print()
    print("  per case, by kind:")
    by_kind: dict[str, list] = {}
    for case, *_ in zip(cases):
        by_kind.setdefault(case.kind, []).append(case)
    for kind, kind_cases in by_kind.items():
        ids = {c.query for c in kind_cases}
        print(f"    {kind}")
        for m in modes:
            relevant_scores = [s for s in results[m] if s.query in ids]
            if relevant_scores:
                print(
                    f"      {m:<8} recall {retrieval_metrics.mean(relevant_scores, 'recall_at_k'):.3f}"
                    f"   MRR {retrieval_metrics.mean(relevant_scores, 'mrr'):.3f}"
                    f"   NDCG {retrieval_metrics.mean(relevant_scores, 'ndcg_at_k'):.3f}"
                )
    approximate = [c.id for c in cases if not c.exact]
    if approximate:
        print()
        print(f"  approximate ground truth ({len(approximate)}): {', '.join(approximate)}")
        print("    labelled by phrase match rather than entity membership - see")
        print("    app/evaluation/retrieval_golden.py. Fair between modes, soft in absolute terms.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
