"""Differential indexing: refresh only what changed, not the whole corpus.

These talk to the real database, like the rest of the repository tests, but they
must never talk to a real embedding provider. ai-service/.env configures Gemini
at 3072 dimensions, so an unguarded index_all() here bills ~2,400 documents to
the live API - which is exactly what happened the first time these were written,
until Gemini started returning 429. The `offline_embedder` fixture below is not
an optimisation; without it this file costs money every run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories import index_watermark_repository as wm

SOURCES = ("application", "node", "cluster", "hosting", "incident", "dependency")


@pytest.fixture(autouse=True)
def offline_embedder(monkeypatch):
    """Force the deterministic hash embedder and the in-memory store.

    autouse, deliberately: a test added later that forgets this fixture would
    not fail, it would quietly bill the embedding provider. The safe default has
    to be the one you get by doing nothing.

    Every cache in the chain has to be cleared - settings, embedder, and the
    vector store - because each is lru_cached and any one left warm would hand
    back the Gemini-backed object the env vars were meant to replace.
    """
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
def clean_watermarks():
    """Every test starts with no watermarks and leaves none behind.

    The table is shared state in a real database, so a test that left a
    watermark would make the next one see a partial corpus and pass or fail for
    reasons unrelated to what it asserts.
    """
    wm.reset()
    yield
    wm.reset()


class TestWatermarkStore:
    def test_an_unknown_source_reads_as_never_run(self, clean_watermarks):
        """None and "indexed up to the epoch" must stay distinguishable - one
        means index everything, the other means index nothing."""
        assert wm.get("application") is None

    def test_save_then_get_round_trips(self, clean_watermarks):
        at = datetime(2026, 9, 1, 10, 30, 0)
        wm.save("application", last_seen_at=at, last_seen_id=None, documents_indexed=7, run_at=at)
        row = wm.get("application")
        assert row["LastSeenAt"] == at
        assert row["DocumentsIndexed"] == 7

    def test_saving_twice_updates_rather_than_duplicating(self, clean_watermarks):
        """Source is the primary key; a second save is an update. If it inserted,
        get() would start returning an arbitrary one of two rows."""
        t1 = datetime(2026, 9, 1, 10, 0, 0)
        t2 = datetime(2026, 9, 1, 11, 0, 0)
        wm.save("node", last_seen_at=t1, last_seen_id=None, documents_indexed=1, run_at=t1)
        wm.save("node", last_seen_at=t2, last_seen_id=None, documents_indexed=2, run_at=t2)
        assert len([r for r in wm.list_all() if r["Source"] == "node"]) == 1
        assert wm.get("node")["LastSeenAt"] == t2

    def test_the_watermark_never_moves_backwards(self, clean_watermarks):
        """A source that returns an older row must not rewind the watermark and
        cause everything after it to be re-indexed."""
        newer = datetime(2026, 9, 1, 12, 0, 0)
        older = datetime(2026, 8, 1, 12, 0, 0)
        wm.save("cluster", last_seen_at=newer, last_seen_id=None, documents_indexed=3, run_at=newer)
        wm.save("cluster", last_seen_at=older, last_seen_id=None, documents_indexed=0, run_at=older)
        assert wm.get("cluster")["LastSeenAt"] == newer

    def test_a_run_that_finds_nothing_still_records_that_it_ran(self, clean_watermarks):
        """Zero documents and "never ran" are different states. Only one of them
        means something is wrong."""
        at = datetime(2026, 9, 1, 9, 0, 0)
        wm.save("incident", last_seen_at=at, last_seen_id=None, documents_indexed=0, run_at=at)
        row = wm.get("incident")
        assert row is not None and row["DocumentsIndexed"] == 0

    def test_id_watermarks_are_kept_separately_from_timestamps(self, clean_watermarks):
        """ApplicationDependency has no timestamp and is followed by IDENTITY."""
        at = datetime(2026, 9, 1, 9, 0, 0)
        wm.save("dependency", last_seen_at=None, last_seen_id=41, documents_indexed=2, run_at=at)
        row = wm.get("dependency")
        assert row["LastSeenId"] == 41
        assert row["LastSeenAt"] is None

    def test_a_null_does_not_erase_an_existing_mark(self, clean_watermarks):
        """Each source sets only the column it uses; the other stays untouched
        rather than being nulled out by the save."""
        at = datetime(2026, 9, 1, 9, 0, 0)
        wm.save("dependency", last_seen_at=None, last_seen_id=41, documents_indexed=1, run_at=at)
        wm.save("dependency", last_seen_at=None, last_seen_id=None, documents_indexed=0, run_at=at)
        assert wm.get("dependency")["LastSeenId"] == 41


class TestRefreshAfterRebuild:
    """The pairing that matters: a rebuild must leave the watermarks describing
    the corpus it produced, or the first refresh re-embeds everything it just
    embedded - the exact cost this feature exists to avoid."""

    def test_index_all_leaves_a_watermark_for_every_source(self, clean_watermarks):
        from app.retrieval.indexer import index_all

        assert index_all() > 0
        recorded = {r["Source"] for r in wm.list_all()}
        assert recorded == set(SOURCES), f"missing: {set(SOURCES) - recorded}"

    def test_a_refresh_straight_after_a_rebuild_indexes_nothing(self, clean_watermarks):
        """The headline behaviour. Nothing has changed, so nothing is embedded."""
        from app.retrieval.indexer import index_all, refresh_index

        index_all()
        result = refresh_index()
        assert result["documents_indexed"] == 0, result["by_source"]

    def test_a_first_refresh_with_no_watermarks_indexes_the_whole_corpus(self, clean_watermarks):
        """With no watermark a source cannot know what it missed, so it takes
        everything. Returning nothing would look identical to "up to date" and
        leave the index permanently empty."""
        from app.retrieval.indexer import refresh_index

        result = refresh_index()
        assert result["documents_indexed"] > 0

    def test_refresh_reports_per_source_not_just_a_total(self, clean_watermarks):
        from app.retrieval.indexer import refresh_index

        result = refresh_index()
        for source in SOURCES:
            assert source in result["by_source"], result["by_source"]

    def test_a_stale_watermark_picks_up_only_what_followed_it(self, clean_watermarks):
        """Rewind one source a long way and leave the others current: only that
        source should produce documents. This is what proves the sources really
        are independent rather than sharing one clock."""
        from app.retrieval.indexer import index_all, refresh_index

        index_all()
        long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3650)
        wm.save("application", last_seen_at=long_ago, last_seen_id=None,
                documents_indexed=0, run_at=datetime.now(timezone.utc).replace(tzinfo=None))
        # save() refuses to move a watermark backwards, so clear it instead -
        # which is also the honest way to express "this source must start over".
        from app.repositories.base import T, execute
        execute(f"DELETE FROM {T('IndexWatermark')} WHERE Source = 'application'")

        result = refresh_index()
        assert result["by_source"]["application"] > 0
        assert result["by_source"]["node"] == 0
        assert result["by_source"]["hosting"] == 0
