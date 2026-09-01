"""How far the differential indexer has already read, per source.

One row per source rather than one global watermark: the sources do not advance
together, and a source that fails must be able to stay behind without rewinding
the ones that succeeded. See database/migration_004_index_watermark.sql.
"""

from __future__ import annotations

from datetime import datetime

from app.repositories.base import T, execute, fetch_all, fetch_one


def get(source: str) -> dict | None:
    """Watermark row for one source, or None if it has never been indexed."""
    return fetch_one(
        f"SELECT Source, LastSeenAt, LastSeenId, LastRunAt, DocumentsIndexed "
        f"FROM {T('IndexWatermark')} WHERE Source = :source",
        {"source": source},
    )


def list_all() -> list[dict]:
    return fetch_all(f"SELECT * FROM {T('IndexWatermark')} ORDER BY Source")


def save(
    source: str,
    *,
    last_seen_at: datetime | None,
    last_seen_id: int | None,
    documents_indexed: int,
    run_at: datetime,
) -> None:
    """Record how far this source got.

    UPDATE-then-INSERT rather than MERGE: MERGE on SQL Server has enough
    documented correctness issues under concurrency that it is not worth using
    for two columns, and the refresh job is single-writer by design anyway.

    The watermark is only ever moved forward. A refresh that finds nothing still
    updates LastRunAt and writes DocumentsIndexed = 0, so "the job ran and there
    was nothing to do" stays distinguishable from "the job has not run" - which
    is the distinction that makes a silent no-op detectable.
    """
    updated = execute(
        f"UPDATE {T('IndexWatermark')} SET "
        f"LastSeenAt = CASE WHEN :last_seen_at IS NULL THEN LastSeenAt "
        f"                  WHEN LastSeenAt IS NULL OR :last_seen_at > LastSeenAt THEN :last_seen_at "
        f"                  ELSE LastSeenAt END, "
        f"LastSeenId = CASE WHEN :last_seen_id IS NULL THEN LastSeenId "
        f"                  WHEN LastSeenId IS NULL OR :last_seen_id > LastSeenId THEN :last_seen_id "
        f"                  ELSE LastSeenId END, "
        f"LastRunAt = :run_at, DocumentsIndexed = :documents "
        f"WHERE Source = :source",
        {
            "source": source,
            "last_seen_at": last_seen_at,
            "last_seen_id": last_seen_id,
            "run_at": run_at,
            "documents": documents_indexed,
        },
    )
    if updated == 0:
        execute(
            f"INSERT INTO {T('IndexWatermark')} "
            f"(Source, LastSeenAt, LastSeenId, LastRunAt, DocumentsIndexed) "
            f"VALUES (:source, :last_seen_at, :last_seen_id, :run_at, :documents)",
            {
                "source": source,
                "last_seen_at": last_seen_at,
                "last_seen_id": last_seen_id,
                "run_at": run_at,
                "documents": documents_indexed,
            },
        )


def reset() -> None:
    """Forget every watermark, so the next refresh behaves like a first run.

    Used by index_all(): a full rebuild has just re-indexed everything, so the
    watermarks it leaves behind must describe the corpus it produced rather than
    whatever partial state preceded it.
    """
    execute(f"DELETE FROM {T('IndexWatermark')}")
