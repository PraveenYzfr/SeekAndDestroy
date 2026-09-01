"""Index runs: the durable record of what indexed, when, and how it ended.

See database/migration_005_index_runs.sql for why this lives in SQL Server while
the queue lives in Redis.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.repositories.base import T, execute, execute_insert, fetch_all, fetch_one

#: A Running row whose heartbeat is older than this is treated as abandoned.
#: Generous on purpose: a worker mid-batch against a slow embedding provider can
#: legitimately go quiet for a while, and reclaiming a run that is still
#: executing would put two workers on the same corpus.
STALE_HEARTBEAT_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create(mode: str, triggered_by: str | None) -> int:
    """Record a queued run and return its id. Written before the job is
    published, so a run always has a record even if the queue write fails."""
    return execute_insert(
        T("IndexRun"),
        "RunId",
        {"Mode": mode, "Status": "Queued", "TriggeredBy": triggered_by, "QueuedAt": _now()},
    )


def claim(run_id: int) -> bool:
    """Move a run from Queued to Running, if nothing else is already running.

    One statement, because the check and the write have to be atomic. Two
    workers polling the same queue will both see "nothing is running" if the
    check is a separate SELECT, and both will proceed - producing two writers
    against one Qdrant collection and two sets of watermark updates racing each
    other.

    Returns True if this call claimed it. False means either another run holds
    the slot or this run was already claimed, and the caller must not proceed.
    """
    stale_cutoff = _now() - timedelta(seconds=STALE_HEARTBEAT_SECONDS)
    affected = execute(
        f"UPDATE {T('IndexRun')} SET Status = 'Running', StartedAt = :now, HeartbeatAt = :now "
        f"WHERE RunId = :run_id AND Status = 'Queued' "
        f"  AND NOT EXISTS ("
        f"      SELECT 1 FROM {T('IndexRun')} AS other WITH (UPDLOCK, HOLDLOCK) "
        f"      WHERE other.Status = 'Running' AND other.HeartbeatAt > :stale_cutoff)",
        {"run_id": run_id, "now": _now(), "stale_cutoff": stale_cutoff},
    )
    return affected == 1


def heartbeat(run_id: int, *, documents: int, batches: int, source: str | None) -> None:
    """Report progress. Called after every batch, which is also what proves the
    worker is alive - there is no separate liveness signal to fall out of sync
    with the work."""
    execute(
        f"UPDATE {T('IndexRun')} SET HeartbeatAt = :now, DocumentsIndexed = :documents, "
        f"BatchesCompleted = :batches, CurrentSource = :source WHERE RunId = :run_id",
        {"run_id": run_id, "now": _now(), "documents": documents, "batches": batches, "source": source},
    )


def finish(run_id: int, *, status: str, documents: int, batches: int, error: str | None = None) -> None:
    """Close a run out. ``error`` is truncated to fit the column rather than
    failing the update - losing the tail of a stack trace is bad, losing the
    record that the run failed at all is worse."""
    execute(
        f"UPDATE {T('IndexRun')} SET Status = :status, CompletedAt = :now, "
        f"DocumentsIndexed = :documents, BatchesCompleted = :batches, "
        f"ErrorMessage = :error, CurrentSource = NULL WHERE RunId = :run_id",
        {
            "run_id": run_id,
            "status": status,
            "now": _now(),
            "documents": documents,
            "batches": batches,
            "error": (error or "")[:2000] or None,
        },
    )


def reclaim_abandoned() -> int:
    """Mark Running rows with stale heartbeats as Abandoned.

    Housekeeping, not recovery. claim() already ignores a Running row whose
    heartbeat has gone stale, so a crashed worker releases the lock by falling
    silent and indexing continues without this ever being called.

    What it fixes is the *history*: without it, a crashed run sits in the table
    reading Running forever, so "what happened to run 41" has no answer and any
    dashboard counting active runs is wrong. Called at worker startup, which is
    when a previous crash is most likely to be sitting there.
    """
    stale_cutoff = _now() - timedelta(seconds=STALE_HEARTBEAT_SECONDS)
    return execute(
        f"UPDATE {T('IndexRun')} SET Status = 'Abandoned', CompletedAt = :now, "
        f"ErrorMessage = 'worker heartbeat went stale; run was reclaimed' "
        f"WHERE Status = 'Running' AND (HeartbeatAt IS NULL OR HeartbeatAt <= :stale_cutoff)",
        {"now": _now(), "stale_cutoff": stale_cutoff},
    )


def get(run_id: int) -> dict | None:
    return fetch_one(f"SELECT * FROM {T('IndexRun')} WHERE RunId = :run_id", {"run_id": run_id})


def list_recent(limit: int = 20) -> list[dict]:
    return fetch_all(
        f"SELECT TOP (:limit) * FROM {T('IndexRun')} ORDER BY QueuedAt DESC",
        {"limit": limit},
        max_rows=limit,
    )


def active() -> dict | None:
    """The run currently holding the slot, if any. Used to answer "why is my
    trigger queued" without making the caller read the whole history."""
    rows = fetch_all(
        f"SELECT TOP (1) * FROM {T('IndexRun')} WHERE Status IN ('Queued','Running') ORDER BY QueuedAt",
        max_rows=1,
    )
    return rows[0] if rows else None
