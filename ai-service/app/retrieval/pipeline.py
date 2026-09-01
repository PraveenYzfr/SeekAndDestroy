"""Streaming, resumable indexing.

The previous shape collected every changed document into one list, embedded it in
one call, and advanced the watermarks once at the end. Three things break at
volume, and all three are silent:

* **Memory.** A million changed rows means a million documents held at once
  before anything is written.
* **Resumability.** A run that died at 90% advanced no watermark, so the next one
  started from zero. A corpus too large to index in one uninterrupted pass could
  never be indexed at all.
* **Observability.** Progress existed only in the return value, which a failed
  run never produced.

Here each source is paged with a keyset cursor and yielded as a batch. The caller
writes the batch, saves the cursor, and only then asks for the next one - so an
interrupted run resumes from its last completed batch rather than its start.

WHY A GENERATOR AND NOT A CALLBACK
----------------------------------
The caller owns the writing, the checkpointing and the progress reporting. That
keeps the ordering guarantee - documents are durable *before* their cursor moves -
in one place, where it can be read, instead of split across a callback contract
where "the batch succeeded" and "the cursor advanced" can drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

import structlog

from app.models.retrieval import RetrievalDocument
from app.repositories import (
    application_repository,
    cluster_repository,
    dependency_repository,
    hosting_repository,
    incident_repository,
    index_watermark_repository,
    itsm_repository,
    node_repository,
)
from app.retrieval import documents
from app.services import capacity

logger = structlog.get_logger(__name__)

#: Rows fetched, and documents embedded, per batch. Sized to the embedding
#: provider rather than to the database: 500 documents is roughly two Gemini
#: batch calls at the configured batch size, so a failure costs seconds of work
#: rather than minutes, and the cursor moves often enough that resuming is cheap.
BATCH_SIZE = 500

#: How far back incident documents are indexed.
#:
#: Was 180 when the estate had 61 incidents, where the window was academic. With
#: 10,000 incidents spanning 540 days it silently excluded two thirds of them -
#: 6,700 tickets present in the database and absent from the index, so "has this
#: happened before on this cluster" answered from the last six months only and
#: gave no sign it was doing so.
#:
#: 540 matches the seed's history. Recency still matters for ranking, but that
#: belongs in scoring, not in whether a document exists at all: an index that
#: cannot see last year cannot tell you a problem is recurring.
INCIDENT_WINDOW_DAYS = 540

#: Every source this pipeline reads, in the order it reads them. Nodes precede
#: clusters deliberately - see the fan-out note in iter_batches().
SOURCES = (
    "application",
    "node",
    "cluster",
    "hosting",
    "incident_opened",
    "incident_closed",
    "change",
    "problem",
    "dependency",
)


@dataclass
class Batch:
    """One unit of work: documents to write, and the cursor they exhaust.

    ``cursor_at``/``cursor_id`` describe the *last row in this batch*, not the
    time the batch was produced. Saving a wall-clock timestamp instead would skip
    every row written while the batch was in flight.
    """

    source: str
    documents: list[RetrievalDocument]
    cursor_at: datetime | None = None
    cursor_id: int | None = None


@dataclass
class _Context:
    """Lookups shared by every batch, loaded once per run.

    Clusters and applications are read whole because documents refer to them by
    code and both sets are small - hundreds and dozens. Nodes, which are not
    small, are never loaded whole; they are paged like everything else.
    """

    clusters: dict = field(default_factory=dict)
    apps: dict = field(default_factory=dict)
    dirty_clusters: set = field(default_factory=set)


def _cursor(source: str) -> tuple:
    row = index_watermark_repository.get(source)
    if not row:
        return None, 0
    return row["LastSeenAt"], row["LastSeenId"] or 0


def _load_context() -> _Context:
    return _Context(
        clusters={c.ClusterId: c for c in cluster_repository.list_all(limit=5000)},
        apps={a.ApplicationId: a for a in application_repository.list_all(limit=5000)},
    )


def iter_batches(batch_size: int = BATCH_SIZE) -> Iterator[Batch]:
    """Yield one batch at a time, each with the cursor it exhausts.

    Sources are independent: each has its own watermark and each is paged to
    exhaustion before the next begins. A source that raises stops the run with
    every earlier source already durably checkpointed.
    """
    ctx = _load_context()

    yield from _applications(ctx, batch_size)
    # Nodes before clusters. cluster_document() embeds live utilisation computed
    # from a cluster's nodes, and InfrastructureCluster.UpdatedAt does not move
    # when a node changes - so a decommissioned host would otherwise leave its
    # cluster advertising capacity that no longer exists. Paging nodes first
    # collects the affected cluster ids; the cluster stage then re-indexes them
    # alongside the clusters that changed in their own right.
    yield from _nodes(ctx, batch_size)
    yield from _clusters(ctx, batch_size)
    yield from _hosting(ctx, batch_size)
    yield from _incidents_itsm(ctx, batch_size)
    yield from _incidents_closed(ctx, batch_size)
    yield from _changes(ctx, batch_size)
    yield from _problems(ctx, batch_size)
    yield from _dependencies(ctx, batch_size)


def _applications(ctx: _Context, size: int) -> Iterator[Batch]:
    at, ident = _cursor("application")
    while True:
        rows = application_repository.changed_since(at, ident, limit=size)
        if not rows:
            return
        yield Batch(
            source="application",
            documents=[documents.application_document(a) for a in rows],
            cursor_at=rows[-1].UpdatedAt,
            cursor_id=rows[-1].ApplicationId,
        )
        at, ident = rows[-1].UpdatedAt, rows[-1].ApplicationId
        if len(rows) < size:
            return


def _nodes(ctx: _Context, size: int) -> Iterator[Batch]:
    at, ident = _cursor("node")
    while True:
        rows = node_repository.changed_since(at, ident, limit=size)
        if not rows:
            return
        docs = []
        for node in rows:
            cluster = ctx.clusters.get(node.ClusterId)
            if cluster:
                docs.append(documents.node_document(node, cluster.ClusterCode))
                ctx.dirty_clusters.add(node.ClusterId)
        yield Batch(
            source="node",
            documents=docs,
            cursor_at=rows[-1].UpdatedAt,
            cursor_id=rows[-1].NodeId,
        )
        at, ident = rows[-1].UpdatedAt, rows[-1].NodeId
        if len(rows) < size:
            return


def _clusters(ctx: _Context, size: int) -> Iterator[Batch]:
    at, ident = _cursor("cluster")
    seen: set = set()
    while True:
        rows = cluster_repository.changed_since(at, ident, limit=size)
        if not rows:
            break
        seen.update(c.ClusterId for c in rows)
        yield Batch(
            source="cluster",
            documents=[_cluster_document(ctx, c.ClusterId) for c in rows if ctx.clusters.get(c.ClusterId)],
            cursor_at=rows[-1].UpdatedAt,
            cursor_id=rows[-1].ClusterId,
        )
        at, ident = rows[-1].UpdatedAt, rows[-1].ClusterId
        if len(rows) < size:
            break

    # Clusters made stale by a node change rather than by their own row. No
    # cursor is advanced for these: their watermark is owned by the cluster
    # source above, and moving it here would claim progress through a cursor
    # these documents were not read by.
    fanout = sorted(ctx.dirty_clusters - seen)
    for start in range(0, len(fanout), size):
        chunk = fanout[start : start + size]
        docs = [_cluster_document(ctx, cid) for cid in chunk if ctx.clusters.get(cid)]
        if docs:
            yield Batch(source="cluster_via_node_change", documents=docs)


def _cluster_document(ctx: _Context, cluster_id: int) -> RetrievalDocument:
    cluster = ctx.clusters[cluster_id]
    snapshot = capacity.compute_cluster_capacity(cluster)
    return documents.cluster_document(
        cluster,
        current_cpu_percent=float(snapshot.current_cpu_utilization_percent),
        current_memory_percent=float(snapshot.current_memory_utilization_percent),
    )


def _hosting(ctx: _Context, size: int) -> Iterator[Batch]:
    at, ident = _cursor("hosting")
    while True:
        rows = hosting_repository.changed_since(at, ident, limit=size)
        if not rows:
            return
        docs = []
        for hosting in rows:
            app = ctx.apps.get(hosting.ApplicationId)
            cluster = ctx.clusters.get(hosting.ClusterId)
            if app and cluster:
                docs.append(documents.hosting_document(hosting, app.ApplicationCode, cluster.ClusterCode))
        yield Batch(source="hosting", documents=docs, cursor_at=rows[-1].UpdatedAt, cursor_id=rows[-1].HostingId)
        at, ident = rows[-1].UpdatedAt, rows[-1].HostingId
        if len(rows) < size:
            return


def _incident_subject(ctx: _Context, incident) -> str | None:
    if incident.ApplicationId and incident.ApplicationId in ctx.apps:
        return "application " + ctx.apps[incident.ApplicationId].ApplicationCode
    if incident.ClusterId and incident.ClusterId in ctx.clusters:
        return "cluster " + ctx.clusters[incident.ClusterId].ClusterCode
    # index_all() only reaches incidents through an application or a cluster.
    # Indexing a node-only incident here would add a document that a rebuild
    # then removed, so the two paths would disagree about the corpus.
    return None


def _incident_batches(ctx: _Context, size: int, source: str, fetch, cursor_of) -> Iterator[Batch]:
    at, ident = _cursor(source)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=INCIDENT_WINDOW_DAYS)
    while True:
        rows = fetch(at, ident, limit=size)
        if not rows:
            return
        docs = []
        for incident in rows:
            if incident.OpenedAt < cutoff:
                continue
            subject = _incident_subject(ctx, incident)
            if subject:
                docs.append(documents.incident_document(incident, subject))
        # The cursor advances even when every row in the page was filtered out.
        # It records how far the *source* was read, not how many documents that
        # produced - otherwise a long run of out-of-window incidents would be
        # re-read on every refresh, forever.
        yield Batch(source=source, documents=docs, cursor_at=cursor_of(rows[-1]), cursor_id=rows[-1].IncidentId)
        at, ident = cursor_of(rows[-1]), rows[-1].IncidentId
        if len(rows) < size:
            return


def _incidents_opened(ctx: _Context, size: int) -> Iterator[Batch]:
    yield from _incident_batches(
        ctx, size, "incident_opened", incident_repository.changed_since, lambda i: i.OpenedAt
    )


def _incidents_closed(ctx: _Context, size: int) -> Iterator[Batch]:
    yield from _incident_batches(
        ctx, size, "incident_closed", incident_repository.closed_since, lambda i: i.ClosedAt
    )


def _dependencies(ctx: _Context, size: int) -> Iterator[Batch]:
    _, ident = _cursor("dependency")
    last_id = ident or None
    while True:
        rows = dependency_repository.created_after_id(last_id, limit=size)
        if not rows:
            return
        docs = []
        for dep in rows:
            source_app = ctx.apps.get(dep.SourceApplicationId)
            if not source_app:
                continue
            if dep.TargetApplicationId:
                target = ctx.apps.get(dep.TargetApplicationId)
                target_desc = "application " + target.ApplicationCode if target else "an application"
            elif dep.TargetClusterId:
                target_cluster = ctx.clusters.get(dep.TargetClusterId)
                target_desc = "cluster " + target_cluster.ClusterCode if target_cluster else "a cluster"
            else:
                target_desc = "an unspecified target"
            docs.append(documents.dependency_document(dep, source_app.ApplicationCode, target_desc))
        yield Batch(source="dependency", documents=docs, cursor_id=rows[-1].DependencyId)
        last_id = rows[-1].DependencyId
        if len(rows) < size:
            return


def standards_batch() -> Batch:
    """The static policy documents.

    Not a source and not watermarked: there are four of them, they live in
    Python rather than the database, and they change only when the code does.
    A rebuild writes them; a refresh does not need to.
    """
    from app.retrieval.indexer import STANDARDS

    return Batch(
        source="standard",
        documents=[documents.standard_document(doc_id, title, text) for doc_id, title, text in STANDARDS],
    )


def execute(mode: str, on_batch=None, *, should_stop=None) -> dict:
    """Run the whole pipeline, checkpointing after every batch.

    Shared by the worker and by the synchronous entry points in indexer.py, so
    there is exactly one implementation of the ordering rule that matters:

        write the documents -> save the cursor -> report progress

    Never the other way round. A cursor that moved before the write would let a
    failed embed skip those rows permanently while the run still looked healthy.

    ``on_batch(source, documents_written, batches)`` is called after each batch
    is durable. ``should_stop()`` is polled on batch boundaries so a worker can
    drain on SIGTERM at a point where everything written is also checkpointed.
    """
    from app.retrieval.vector_store import get_vector_store

    store = get_vector_store()
    written = 0
    batches = 0
    # Per-source tallies, seeded with every source at zero. Seeded rather than
    # accumulated from what ran, so a source that produced nothing is reported
    # as 0 instead of being absent - "indexed nothing" and "was never read" are
    # different answers and only one of them is fine.
    by_source: dict[str, int] = {s: 0 for s in SOURCES}

    if mode == "rebuild":
        # Clear, then forget the watermarks, so what follows behaves as a first
        # index. This order leaves no window in which the collection holds
        # documents that no watermark claims to have indexed.
        store.clear()
        index_watermark_repository.reset()

    # Standards on EVERY run, not only on a rebuild.
    #
    # They were rebuild-only, reasoning that they live in Python, change only
    # when the code changes, and have no watermark to advance. That was wrong in
    # the way that matters: a refresh is how the collection actually gets
    # filled, so "no watermark" turned into "never indexed", and the live
    # collection held 0 of the 4 policy documents. Every grounded question about
    # why a policy exists was answered from node documents instead.
    #
    # Re-writing four documents on every run costs four embeddings and is
    # idempotent - the document ids are stable, so this replaces rather than
    # duplicates. That is a much smaller price than a policy corpus that is
    # silently absent.
    standards = standards_batch()
    store.upsert(standards.documents)
    written += len(standards.documents)
    batches += 1
    by_source[standards.source] = by_source.get(standards.source, 0) + len(standards.documents)
    if on_batch:
        on_batch(standards.source, written, batches)

    for batch in iter_batches():
        if batch.documents:
            store.upsert(batch.documents)
            written += len(batch.documents)
        by_source[batch.source] = by_source.get(batch.source, 0) + len(batch.documents)

        # Only a batch carrying a real cursor advances a watermark. The cluster
        # fan-out batch carries none: its documents were reached through the
        # node cursor, and advancing the cluster cursor for them would skip
        # clusters that had never been read.
        if batch.cursor_at is not None or batch.cursor_id is not None:
            index_watermark_repository.save(
                batch.source,
                last_seen_at=batch.cursor_at,
                last_seen_id=batch.cursor_id,
                documents_indexed=len(batch.documents),
                run_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )

        batches += 1
        if on_batch:
            on_batch(batch.source, written, batches)

        if should_stop and should_stop():
            logger.info("pipeline.stopped_on_batch_boundary", documents=written, batches=batches)
            return {"documents_indexed": written, "batches": batches,
                    "by_source": by_source, "stopped_early": True}

    logger.info("pipeline.completed", mode=mode, documents=written, batches=batches, **by_source)
    return {"documents_indexed": written, "batches": batches,
            "by_source": by_source, "stopped_early": False}


def _incidents_itsm(ctx: _Context, size: int) -> Iterator[Batch]:
    """Incidents as chunks: header, each substantive note, resolution.

    Replaces the one-document-per-incident source. At 10,000 incidents carrying
    89,912 work notes, a single document per ticket averages its vector over
    everything in it - the one note that answers a question is diluted by the
    ten that do not.

    Comments are fetched once per PAGE, not once per incident. The per-incident
    shape is 10,000 round trips returning nine rows each; this is 20 round trips
    returning ~4,500 each, and it scales with what changed rather than with the
    size of the corpus.
    """
    at, ident = _cursor("incident_opened")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=INCIDENT_WINDOW_DAYS)
    while True:
        rows = incident_repository.changed_since(at, ident, limit=size)
        if not rows:
            return
        in_window = [i for i in rows if i.OpenedAt >= cutoff]
        comments = itsm_repository.incident_comments_for([i.IncidentId for i in in_window])
        docs = []
        for incident in in_window:
            cluster = ctx.clusters.get(incident.ClusterId)
            app = ctx.apps.get(incident.ApplicationId)
            subject = (f"application {app.ApplicationCode}" if app
                       else f"cluster {cluster.ClusterCode}" if cluster else "")
            if not subject:
                # index_all() only ever reached incidents through an application
                # or a cluster; indexing a node-only one here would add a
                # document a rebuild then removed.
                continue
            docs.extend(documents.incident_chunks(
                incident, comments.get(incident.IncidentId, []),
                cluster_code=cluster.ClusterCode if cluster else "",
                app_code=app.ApplicationCode if app else "",
                subject_description=subject,
            ))
        # The cursor advances over the whole page even when rows were filtered
        # out of the window - it records how far the source was READ, not how
        # many documents that produced. Otherwise a long run of out-of-window
        # incidents is re-read on every refresh, forever.
        yield Batch(source="incident_opened", documents=docs,
                    cursor_at=rows[-1].OpenedAt, cursor_id=rows[-1].IncidentId)
        at, ident = rows[-1].OpenedAt, rows[-1].IncidentId
        if len(rows) < size:
            return


def _changes(ctx: _Context, size: int) -> Iterator[Batch]:
    """Changes as chunks: header, plan, backout, risk, outcome.

    The plan and the backout are separate documents on purpose. "What were we
    going to do if this failed" is a different question from "what were we
    doing", and someone looking at a failed change is asking the second.
    """
    at, ident = _cursor("change")
    while True:
        rows = itsm_repository.changes_changed_since(at, ident, limit=size)
        if not rows:
            return
        comments = itsm_repository.change_comments_for([c.ChangeId for c in rows])
        docs = []
        for change in rows:
            cluster = ctx.clusters.get(change.ClusterId)
            app = ctx.apps.get(change.ApplicationId)
            docs.extend(documents.change_chunks(
                change, comments.get(change.ChangeId, []),
                cluster_code=cluster.ClusterCode if cluster else "",
                app_code=app.ApplicationCode if app else "",
            ))
        yield Batch(source="change", documents=docs,
                    cursor_at=rows[-1].UpdatedAt, cursor_id=rows[-1].ChangeId)
        at, ident = rows[-1].UpdatedAt, rows[-1].ChangeId
        if len(rows) < size:
            return


def _problems(ctx: _Context, size: int) -> Iterator[Batch]:
    """Problems as chunks: header, root cause, workaround, fix - separately.

    The highest-value text in the corpus. A problem record is written to answer
    "why did this keep happening", and the three sections answer different parts
    of it: what was wrong, what we did about it meanwhile, and what actually
    fixed it.
    """
    at, ident = _cursor("problem")
    while True:
        rows = itsm_repository.problems_changed_since(at, ident, limit=size)
        if not rows:
            return
        docs = []
        for problem in rows:
            cluster = ctx.clusters.get(problem.ClusterId)
            docs.extend(documents.problem_chunks(
                problem, cluster_code=cluster.ClusterCode if cluster else ""))
        yield Batch(source="problem", documents=docs,
                    cursor_at=rows[-1].UpdatedAt, cursor_id=rows[-1].ProblemId)
        at, ident = rows[-1].UpdatedAt, rows[-1].ProblemId
        if len(rows) < size:
            return
