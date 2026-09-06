"""The indexing worker: a separate process that consumes the queue.

Runs as its own container (``ai-indexer``) from the same image as ai-service,
with a different command. Separate because indexing is long, memory-hungry and
network-bound on an external provider, and none of that belongs in the process
serving interactive investigations - a full index used to hold an HTTP worker
thread for minutes.

    python -m app.retrieval.worker

THE ORDER THAT MATTERS
----------------------
For every batch: **write the documents, then save the cursor, then report
progress.** Never the other way round. If the cursor moved first, a failed embed
would skip those rows permanently while the run still looked healthy - the exact
failure mode where the index silently falls behind the database and nothing
anywhere says so.
"""

from __future__ import annotations

import signal
import sys
import time

import structlog

from app.observability.logging import configure_logging
from app.repositories import index_run_repository
from app.retrieval import index_queue, pipeline

logger = structlog.get_logger(__name__)

_shutdown = False


def _handle_signal(signum, _frame):
    """Finish the batch in flight, then stop.

    SIGTERM is what `docker stop` sends, and the default action is to die
    immediately - mid-upsert, before the cursor is saved. Draining instead means
    a redeploy costs at most one batch of repeated work rather than leaving a
    Running row to be reclaimed by heartbeat timeout minutes later.
    """
    global _shutdown
    _shutdown = True
    logger.info("index_worker.shutdown_requested", signal=signum)


def run_job(run_id: int, mode: str) -> None:
    """Execute one queued run to completion, checkpointing as it goes."""
    if not index_run_repository.claim(run_id):
        # Either another run holds the slot or this one was already claimed.
        # Leaving it Queued is correct: it stays visible, and a later trigger
        # finds the slot free.
        active = index_run_repository.active()
        logger.warning(
            "index_worker.claim_refused",
            run_id=run_id,
            blocked_by=active.get("RunId") if active else None,
        )
        return

    progress = {"documents": 0, "batches": 0}

    def on_batch(source: str, written: int, batches: int) -> None:
        progress["documents"], progress["batches"] = written, batches
        index_run_repository.heartbeat(run_id, documents=written, batches=batches, source=source)

    try:
        result = pipeline.execute(mode, on_batch=on_batch, should_stop=lambda: _shutdown)
        if result["stopped_early"]:
            # Stopped on a boundary where everything written is also
            # checkpointed. Marked Failed rather than Succeeded because it did
            # not finish - the next run resumes from here rather than restarting.
            index_run_repository.finish(
                run_id, status="Failed",
                documents=result["documents_indexed"], batches=result["batches"],
                error="worker shut down; stopped on a batch boundary and can be resumed",
            )
            logger.info("index_worker.drained", run_id=run_id, **result)
            return

        index_run_repository.finish(
            run_id, status="Succeeded",
            documents=result["documents_indexed"], batches=result["batches"],
        )
        logger.info("index_worker.completed", run_id=run_id, mode=mode, **result)

    except Exception as exc:
        # Everything already checkpointed stays checkpointed: the next run
        # resumes from the last completed batch rather than starting over.
        # That is the whole reason the cursor is saved per batch.
        index_run_repository.finish(
            run_id, status="Failed",
            documents=progress["documents"], batches=progress["batches"],
            error=f"{type(exc).__name__}: {exc}",
        )
        logger.error(
            "index_worker.failed", run_id=run_id, error=str(exc),
            documents_before_failure=progress["documents"],
            batches_before_failure=progress["batches"],
        )


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Tidy up any run a previous worker was killed in the middle of. Not
    # required for correctness - claim() ignores a stale heartbeat, so the lock
    # was never actually held - but it stops the history reading Running for a
    # run that stopped hours ago.
    reclaimed = index_run_repository.reclaim_abandoned()
    if reclaimed:
        logger.warning("index_worker.reclaimed_abandoned_runs", count=reclaimed)

    logger.info("index_worker.started", queue=index_queue.QUEUE_KEY)

    while not _shutdown:
        try:
            run_id = index_queue.dequeue()
        except index_queue.QueueUnavailable as exc:
            # Redis down. Back off and keep trying rather than exiting: the
            # container would otherwise restart-loop, and the log would fill
            # with startup noise instead of the actual cause.
            logger.error("index_worker.queue_unavailable", error=str(exc))
            time.sleep(10)
            continue

        if run_id is None:
            continue

        row = index_run_repository.get(run_id)
        if not row:
            logger.warning("index_worker.unknown_run", run_id=run_id)
            continue
        if row["Status"] != "Queued":
            # Already claimed, or finished. Normal after a requeue.
            logger.info("index_worker.skipping_non_queued", run_id=run_id, status=row["Status"])
            continue

        run_job(run_id, row["Mode"])

    logger.info("index_worker.stopped")
    return 0


if __name__ == "__main__":
    # Without this the worker's log events come out under structlog's
    # defaults - unfiltered and unrendered - while every other process in this
    # image uses the platform's configuration. Two shapes from one platform.
    configure_logging()
    sys.exit(main())
