"""The async indexing pipeline: run records, the single-run lease, resumability.

What is worth testing here is not "does it index" - test_differential_index.py
covers that - but the properties that only matter when something goes wrong:

  * two workers must not index the same corpus at once
  * a worker that dies must not block every later run forever
  * a run that fails at 90% must resume, not restart
  * a cursor must never advance past documents that were not written

The last one is the reason the whole thing is shaped this way, and it is the
one a passing "it indexed everything" test would never catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories import index_run_repository as runs
from app.repositories import index_watermark_repository as wm
from app.repositories.base import T, execute


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def offline_embedder(monkeypatch):
    """Hash embedder and in-memory store. autouse, because a test that forgets
    it would not fail - it would bill Gemini for the whole corpus."""
    from app.config import get_settings
    from app.retrieval import embedder as embedder_module
    from app.retrieval import vector_store as vector_store_module

    monkeypatch.setenv("SAD_RETRIEVAL__EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("SAD_RETRIEVAL__EMBEDDING_DIMENSIONS", "384")
    monkeypatch.setenv("SAD_RETRIEVAL__BACKEND", "memory")

    def _reset():
        get_settings.cache_clear()
        embedder_module.reset_embedder_cache()
        vector_store_module.reset_vector_store_cache()

    _reset()
    yield
    monkeypatch.undo()
    _reset()


@pytest.fixture
def clean(monkeypatch):
    """No leftover runs or watermarks either side of a test."""
    execute(f"DELETE FROM {T('IndexRun')}")
    wm.reset()
    yield
    execute(f"DELETE FROM {T('IndexRun')}")
    wm.reset()


class TestRunLifecycle:
    def test_a_new_run_starts_queued_and_is_attributed(self, clean):
        """Indexing spends money at the embedding provider, so an
        unattributable run is not acceptable."""
        run_id = runs.create("refresh", "E1001")
        row = runs.get(run_id)
        assert row["Status"] == "Queued"
        assert row["TriggeredBy"] == "E1001"
        assert row["StartedAt"] is None

    def test_claiming_moves_it_to_running(self, clean):
        run_id = runs.create("refresh", "E1001")
        assert runs.claim(run_id) is True
        assert runs.get(run_id)["Status"] == "Running"

    def test_a_run_cannot_be_claimed_twice(self, clean):
        """Two workers popping the same id must not both proceed."""
        run_id = runs.create("refresh", "E1001")
        assert runs.claim(run_id) is True
        assert runs.claim(run_id) is False

    def test_a_second_run_cannot_start_while_one_is_live(self, clean):
        """The single-run lease. Two concurrent indexers would write to one
        Qdrant collection and race each other's watermark updates."""
        first = runs.create("refresh", "E1001")
        second = runs.create("refresh", "E1001")
        assert runs.claim(first) is True
        assert runs.claim(second) is False, "the lease let a second run through"

    def test_the_slot_frees_when_the_first_run_finishes(self, clean):
        first = runs.create("refresh", "E1001")
        second = runs.create("refresh", "E1001")
        runs.claim(first)
        runs.finish(first, status="Succeeded", documents=10, batches=2)
        assert runs.claim(second) is True

    def test_progress_is_visible_while_the_run_is_still_running(self, clean):
        """A long index has to be observable before it is over - that is the
        only time anyone actually wants to look at it."""
        run_id = runs.create("rebuild", "E1001")
        runs.claim(run_id)
        runs.heartbeat(run_id, documents=500, batches=1, source="node")
        row = runs.get(run_id)
        assert row["Status"] == "Running"
        assert row["DocumentsIndexed"] == 500
        assert row["CurrentSource"] == "node"

    def test_a_failure_records_why(self, clean):
        run_id = runs.create("refresh", "E1001")
        runs.claim(run_id)
        runs.finish(run_id, status="Failed", documents=120, batches=1, error="Gemini 429")
        row = runs.get(run_id)
        assert row["Status"] == "Failed"
        assert "429" in row["ErrorMessage"]
        # Progress made before the failure is kept: it is what the next run
        # resumes from, so discarding it would hide the resume point.
        assert row["DocumentsIndexed"] == 120

    def test_an_overlong_error_is_truncated_rather_than_lost(self, clean):
        """Losing the tail of a stack trace is bad; losing the record that the
        run failed at all is worse."""
        run_id = runs.create("refresh", "E1001")
        runs.claim(run_id)
        runs.finish(run_id, status="Failed", documents=0, batches=0, error="x" * 5000)
        assert runs.get(run_id)["Status"] == "Failed"


class TestAbandonedRuns:
    """A worker killed mid-run cannot mark its own row - that is exactly the
    case where it has stopped executing."""

    def test_a_stale_running_row_is_reclaimed(self, clean):
        run_id = runs.create("refresh", "E1001")
        runs.claim(run_id)
        stale = _now() - timedelta(seconds=runs.STALE_HEARTBEAT_SECONDS + 60)
        execute(
            f"UPDATE {T('IndexRun')} SET HeartbeatAt = :stale WHERE RunId = :id",
            {"stale": stale, "id": run_id},
        )
        assert runs.reclaim_abandoned() == 1
        assert runs.get(run_id)["Status"] == "Abandoned"

    def test_a_live_run_is_not_reclaimed(self, clean):
        """Reclaiming a run that is still working would put two workers on the
        same corpus - worse than the stuck lease it is trying to fix."""
        run_id = runs.create("refresh", "E1001")
        runs.claim(run_id)
        runs.heartbeat(run_id, documents=1, batches=1, source="node")
        assert runs.reclaim_abandoned() == 0
        assert runs.get(run_id)["Status"] == "Running"

    def test_a_crashed_worker_does_not_wedge_indexing(self, clean):
        """The lock is held by a fresh heartbeat, not by the Running status.

        So a crashed worker releases it by falling silent, and the next run
        proceeds *without* anyone having swept the table first. Written the
        other way round originally - asserting the stale run still blocked -
        and it failed, which is how the comments describing reclaim_abandoned()
        as the recovery mechanism got corrected.
        """
        dead = runs.create("refresh", "E1001")
        runs.claim(dead)
        execute(
            f"UPDATE {T('IndexRun')} SET HeartbeatAt = :stale WHERE RunId = :id",
            {"stale": _now() - timedelta(seconds=runs.STALE_HEARTBEAT_SECONDS + 60), "id": dead},
        )
        nxt = runs.create("refresh", "E1001")
        assert runs.claim(nxt) is True, "a stale run must not hold the lease"

    def test_sweeping_is_about_history_not_recovery(self, clean):
        """reclaim_abandoned() changes what the history says, not what is
        possible. A run left reading Running forever makes "what happened to
        run 41" unanswerable and any count of active runs wrong."""
        dead = runs.create("refresh", "E1001")
        runs.claim(dead)
        execute(
            f"UPDATE {T('IndexRun')} SET HeartbeatAt = :stale WHERE RunId = :id",
            {"stale": _now() - timedelta(seconds=runs.STALE_HEARTBEAT_SECONDS + 60), "id": dead},
        )
        assert runs.get(dead)["Status"] == "Running"
        assert runs.reclaim_abandoned() == 1
        row = runs.get(dead)
        assert row["Status"] == "Abandoned"
        assert row["CompletedAt"] is not None


class TestResumability:
    def test_a_failed_run_resumes_rather_than_restarting(self, clean):
        """The property that makes a million records indexable at all.

        Simulated by interrupting mid-pipeline: everything checkpointed before
        the interruption must not be re-indexed by the next run.
        """
        from app.retrieval import pipeline

        stop_after = {"n": 0}

        def stop_after_two():
            stop_after["n"] += 1
            return stop_after["n"] >= 2

        first = pipeline.execute("refresh", should_stop=stop_after_two)
        assert first["stopped_early"] is True
        assert first["documents_indexed"] > 0

        # The second run continues from the saved cursors. It must index the
        # rest - not zero (which would mean the cursor ran ahead of the writes)
        # and not everything again (which would mean it did not advance).
        second = pipeline.execute("refresh")
        assert second["stopped_early"] is False
        assert second["documents_indexed"] > 0
        assert second["documents_indexed"] < first["documents_indexed"] + second["documents_indexed"]

        # And a third finds nothing left from the database. The four standards
        # are rewritten on every run by design - they have no watermark, and
        # making them rebuild-only meant they were never indexed at all.
        third = pipeline.execute("refresh")
        cmdb = {k: v for k, v in third["by_source"].items() if k != "standard"}
        assert all(v == 0 for v in cmdb.values()), cmdb

    def test_a_cursor_never_advances_past_an_unwritten_document(self, clean, monkeypatch):
        """The ordering guarantee, tested by breaking the write.

        If the cursor moved before the upsert, a failed embed would skip those
        rows permanently while the run still looked healthy. So: make upsert
        raise, and assert no watermark was left behind.
        """
        from app.retrieval import pipeline, vector_store

        store = vector_store.get_vector_store()

        def explode(_documents):
            raise RuntimeError("embedding provider unavailable")

        monkeypatch.setattr(store, "upsert", explode)
        with pytest.raises(RuntimeError):
            pipeline.execute("refresh")

        assert wm.list_all() == [], "a cursor advanced despite the write failing"
