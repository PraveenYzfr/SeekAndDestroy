"""Do the CMDB documents earn their place in the index?

    python scripts/eval_ablation.py --build-a     # index the full corpus (COSTS MONEY)
    python scripts/eval_ablation.py --build-b     # derive the ITSM-only arm (free)
    python scripts/eval_ablation.py --measure     # score both, report the difference

THE QUESTION
------------
The index holds two kinds of thing.

  ITSM prose      work notes, close notes, change plans, problem root cause.
                  No column holds this. If retrieval cannot find it, nothing can.

  CMDB documents  application, cluster, node, hosting and dependency records,
                  rendered from structured rows into English sentences.
                  Roughly 10,000 of them, and every fact in them is already a
                  SQL query away.

e7's claim is that the second kind is redundant with SQL and costs top-k slots
that should go to prose. That is plausible and it has never been tested, and
about 10,000 documents were going to be deleted on the strength of it.

WHY ARM B COSTS NOTHING
-----------------------
The naive design embeds the corpus twice. It does not need to: arm B's documents
are a strict SUBSET of arm A's, and the same text embeds to the same vector.

So arm A is built once - that is the spend - and arm B is derived from it by
scrolling arm A's points, dropping the CMDB entity types, and re-upserting the
survivors with their dense vectors copied verbatim. Zero additional API calls.

The sparse half is NOT copied. BM25 document encoding carries TF saturation and
length normalisation against the corpus average, so a document's sparse vector
genuinely differs between a corpus of 100,000 chunks and one of 90,000. Those are
recomputed from the payload text, which is local arithmetic and free. Copying the
sparse vectors would have been the subtle version of this mistake: correct-
looking, cheap, and quietly measuring arm A's statistics in arm B's collection.

WHAT A FAIR COMPARISON REQUIRES
-------------------------------
Both arms are scored with the SAME golden set, the same k, and the same query
set. The cases whose ground truth is a CMDB entity are reported separately from
the ITSM cases - removing cluster documents should obviously hurt a query about a
cluster, and averaging that in with incident retrieval would hide which is which.

The interesting number is not the headline mean. It is whether any ITSM query
class degrades when the CMDB documents are gone, because that is the only way the
CMDB documents could be earning their place - by being retrieved as CONTEXT for a
question that was not about them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ai-service"))

RESULT = REPO_ROOT / "ai-service" / "app" / "evaluation" / "ablation_result.json"

#: Entity kinds that are structured rows rendered as prose. These are what the
#: ablation removes. Everything else - incidents, changes, problems, standards -
#: is text that exists nowhere else.
CMDB_KINDS = ("application", "cluster", "node", "hosting", "dependency")

ARM_A_SUFFIX = "ablation_full"
ARM_B_SUFFIX = "ablation_itsm"


#: One embedded client for the whole process. Qdrant's on-disk mode takes an
#: exclusive lock on its directory, so a second client against the same path
#: fails - and both arms live in the same directory as separate collections.
_EMBEDDED: dict = {}


def _embedded_client(path: str):
    from qdrant_client import QdrantClient

    if "client" not in _EMBEDDED:
        Path(path).mkdir(parents=True, exist_ok=True)
        _EMBEDDED["client"] = QdrantClient(path=path)
        print(f"  embedded Qdrant at {path}")
    return _EMBEDDED["client"]


def _store_for(base_collection: str, qdrant_path: str | None = None):
    """A vector store bound to a named collection, bypassing the cached singleton.

    With `qdrant_path`, the store is repointed at an EMBEDDED Qdrant instead of a
    server.

    DO NOT USE THIS FOR THE PAID RUN. I wrote the opposite here first, on the
    assumption that "on-disk mode" meant vectors land on disk as they are written
    and an interrupted run keeps what it paid for. Measured, it does not:

        13 minutes of hash-embedding this corpus
        storage folder: 2 KB, containing meta.json and nothing else
        process memory: 9.1 GB and still climbing

    qdrant_client's local mode is a pure-Python implementation that holds the
    whole collection in RAM and serialises on clean shutdown. So it gives the
    exact opposite of the property I claimed for it - a crash, an OOM or a killed
    process loses every embedding, which is the only expensive thing here.

    It is fine for a smoke test on a small corpus. For 100,000 chunks at 3,072
    dimensions it is both the wrong memory profile and the wrong durability
    story, and the paid run needs a real Qdrant server.

    The client is swapped on the instance rather than added to the settings
    model, because app/retrieval belongs to another session tonight and a
    settings field for it should be theirs to add if it is worth keeping.
    """
    from app.config import get_settings
    from app.retrieval import vector_store

    settings = get_settings()
    original = settings.retrieval.collection
    try:
        settings.retrieval.collection = base_collection
        vector_store.reset_vector_store_cache()
        store = vector_store.get_vector_store()
        if qdrant_path:
            store._client = _embedded_client(qdrant_path)  # noqa: SLF001
        return store
    finally:
        settings.retrieval.collection = original
        vector_store.reset_vector_store_cache()


def build_a(batch_delay: float, qdrant_path: str | None) -> int:
    """Index the whole corpus. This is the arm that costs money."""
    from app.config import get_settings
    from app.retrieval import pipeline

    settings = get_settings()
    settings.retrieval.collection = f"{settings.retrieval.collection}__{ARM_A_SUFFIX}"
    if batch_delay:
        # Paced deliberately. The embedding provider enforces a per-minute pool
        # shared across every caller of the base model, so a burst that succeeds
        # in isolation can still 429 because something else is also embedding.
        settings.retrieval.embedding_batch_delay_seconds = batch_delay
        print(f"  pacing: {batch_delay}s between embedding batches")

    from app.retrieval import vector_store

    vector_store.reset_vector_store_cache()
    if qdrant_path:
        # Repoint the module-level singleton the pipeline will fetch for itself.
        store = vector_store.get_vector_store()
        store._client = _embedded_client(qdrant_path)  # noqa: SLF001

    # pipeline.execute, not indexer.index_all. index_all() takes no arguments and
    # indexes into whatever collection the cached store points at; execute() takes
    # the mode explicitly and checkpoints per batch, so an interrupted run resumes
    # from its last completed batch rather than paying for the whole corpus twice.
    def _progress(source: str, written: int, batches: int) -> None:
        print(f"    {source:<22} {written:>7,} documents  ({batches} batches)", flush=True)

    result = pipeline.execute("rebuild", on_batch=_progress)
    print(json.dumps(result, indent=2, default=str))
    return 0


def build_b(qdrant_path: str | None) -> int:
    """Derive the ITSM-only arm from arm A. No embedding provider is contacted."""
    from qdrant_client.models import PointStruct, SparseVector

    from app.config import get_settings
    from app.retrieval import sparse, vector_store

    settings = get_settings()
    base = settings.retrieval.collection
    src = _store_for(f"{base}__{ARM_A_SUFFIX}", qdrant_path)
    dst = _store_for(f"{base}__{ARM_B_SUFFIX}", qdrant_path)

    kept: list[tuple] = []
    dropped = 0
    offset = None
    while True:
        points, offset = src._client.scroll(  # noqa: SLF001 - deliberate, see module docstring
            collection_name=src._collection, limit=1000, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            payload = p.payload or {}
            if payload.get("bm25"):
                continue  # the stats point - rebuilt for arm B, never copied
            if payload.get("entity_type") in CMDB_KINDS:
                dropped += 1
                continue
            kept.append((p.id, payload, p.vector))
        if offset is None:
            break

    if not kept:
        print("arm A is empty - run --build-a first", file=sys.stderr)
        return 2

    print(f"  kept {len(kept):,} documents, dropped {dropped:,} CMDB documents")

    # BM25 is refitted over the SURVIVING corpus. Copying arm A's sparse vectors
    # would carry arm A's document frequencies and average length into arm B and
    # quietly measure the wrong corpus.
    texts = [p[1].get("text", "") for p in kept]
    stats = sparse.fit(texts)
    print(f"  refitted BM25 over {stats.document_count:,} documents")

    dst._ensure_collection()  # noqa: SLF001
    dst._save_stats(stats)  # noqa: SLF001
    written = 0
    for i in range(0, len(kept), 256):
        chunk = kept[i : i + 256]
        points = []
        for (pid, payload, vector), text in zip(chunk, texts[i : i + 256]):
            dense = vector["dense"] if isinstance(vector, dict) else vector
            indices, values = sparse.encode_document(text, stats)
            points.append(
                PointStruct(
                    id=pid,
                    vector={"dense": dense, "sparse": SparseVector(indices=indices, values=values)},
                    payload=payload,
                )
            )
        dst._client.upsert(collection_name=dst._collection, points=points)  # noqa: SLF001
        written += len(points)
    print(f"  wrote {written:,} points to the ITSM-only arm")
    return 0


def measure(k: int, top_k: int, cases_per_kind: int, qdrant_path: str | None) -> int:
    from app.config import get_settings
    from app.evaluation import retrieval_golden, retrieval_metrics

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from eval_retrieval import dedupe_to_entities  # noqa: PLC0415

    cases = retrieval_golden.build_cases(limit_per_kind=cases_per_kind)
    if not cases:
        print("no cases derived - is the ITSM corpus loaded?", file=sys.stderr)
        return 2

    base = get_settings().retrieval.collection
    arms = {"with_cmdb": f"{base}__{ARM_A_SUFFIX}", "itsm_only": f"{base}__{ARM_B_SUFFIX}"}

    scores: dict[str, list] = {}
    for arm, collection in arms.items():
        store = _store_for(collection, qdrant_path)
        out = []
        for case in cases:
            try:
                hits = store.search(case.query, top_k=top_k)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {arm} {case.id}: {exc}", file=sys.stderr)
                continue
            retrieved = dedupe_to_entities([h.document.id for h in hits])
            out.append(retrieval_metrics.score(case.query, arm, retrieved, case.relevant, k=k))
        scores[arm] = out

    control = {c.query for c in cases if c.kind == "control_prefix_match"}
    kinds = {c.query: c.kind for c in cases}

    def mean(arm: str, metric: str, queries=None):
        rows = [s for s in scores[arm] if s.query not in control]
        if queries is not None:
            rows = [s for s in rows if s.query in queries]
        return retrieval_metrics.mean(rows, metric) if rows else None

    print()
    print(f"  {len(cases)} cases, k={k}, top_k={top_k}")
    print()
    print(f"  {'arm':<12}{'recall@k':>10}{'MRR':>8}{'NDCG@k':>9}")
    for arm in arms:
        print(f"  {arm:<12}{mean(arm,'recall_at_k'):>10.3f}{mean(arm,'mrr'):>8.3f}{mean(arm,'ndcg_at_k'):>9.3f}")

    print()
    print("  by query class - this is where the answer is:")
    for kind in sorted(set(kinds.values())):
        if kind == "control_prefix_match":
            continue
        qs = {q for q, kk in kinds.items() if kk == kind}
        a, b = mean("with_cmdb", "mrr", qs), mean("itsm_only", "mrr", qs)
        if a is None or b is None:
            continue
        delta = b - a
        verdict = "no cost" if abs(delta) < 0.01 else ("WORSE without" if delta < 0 else "better without")
        print(f"    {kind:<22} with {a:.3f}   without {b:.3f}   {delta:+.3f}  {verdict}")

    payload = {
        "k": k, "top_k": top_k,
        "cases": [{"id": c.id, "kind": c.kind, "query": c.query} for c in cases],
        "summary": {
            arm: {m: mean(arm, m) for m in ("recall_at_k", "mrr", "ndcg_at_k")} for arm in arms
        },
        "per_case": {
            arm: [{"query": s.query, "mrr": s.mrr, "recall_at_k": s.recall_at_k} for s in rows]
            for arm, rows in scores.items()
        },
    }
    RESULT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  written to {RESULT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-a", action="store_true", help="index the full corpus (costs money)")
    ap.add_argument("--build-b", action="store_true", help="derive the ITSM-only arm (free)")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--batch-delay", type=float, default=1.0, help="seconds between embedding batches")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=30)
    ap.add_argument("--cases-per-kind", type=int, default=3)
    ap.add_argument(
        "--qdrant-path", default=None,
        help="use an embedded on-disk Qdrant at this directory instead of a server",
    )
    args = ap.parse_args()

    if args.build_a:
        return build_a(args.batch_delay, args.qdrant_path)
    if args.build_b:
        return build_b(args.qdrant_path)
    if args.measure:
        return measure(args.k, args.top_k, args.cases_per_kind, args.qdrant_path)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
