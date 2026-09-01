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

# Imported rather than restated: this list changed when incidents split into
# opened/closed cursors, and a hardcoded copy here failed for that reason.
from app.retrieval.pipeline import SOURCES


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


@pytest.fixture
def bounded_corpus(monkeypatch):
    """Run the rebuild-then-refresh tests over a SMALL slice of the estate.

    These assert refresh-after-rebuild SEMANTICS - that a rebuild leaves a
    watermark per source, that an immediate refresh re-indexes nothing, that a
    stale watermark picks up only what followed it. None of that needs the whole
    corpus, and the whole corpus is now 54,555 configuration items, 30,105 VMs and
    89,831 work notes.

    Five full index_all() passes over that took the suite from minutes to
    indefinite - it stalled at 23% for long enough that I killed it. A test nobody
    can afford to run is a test that stops being run, and then the semantics it
    protects go unchecked for the same reason they would if it had been deleted.

    Two sources, not one: every assertion here is about watermarks being kept
    SEPARATELY per source, and a single source cannot distinguish "per source"
    from "one global mark". Two of the cheapest ones - clusters and applications
    are hundreds of rows rather than tens of thousands - preserve the property
    while removing the cost.
    """
    from app.retrieval import pipeline

    allowed = ("cluster", "application")
    real_iter = pipeline.iter_batches

    def bounded(*args, **kwargs):
        for batch in real_iter(*args, **kwargs):
            if batch.source in allowed:
                yield batch

    # iter_batches, not SOURCES. Patching the SOURCES tuple looks like it should
    # work and does nothing: execute() only uses it to seed the per-source tally
    # dict, and the generators it actually drains are wired separately. My first
    # attempt at this fixture patched SOURCES, the tests still walked the whole
    # corpus, and the only symptom was that they were still slow - a fix that
    # looked applied and was not.
    monkeypatch.setattr(pipeline, "iter_batches", bounded)
    monkeypatch.setattr(pipeline, "SOURCES", allowed)
    return allowed


class TestRefreshAfterRebuild:
    """The pairing that matters: a rebuild must leave the watermarks describing
    the corpus it produced, or the first refresh re-embeds everything it just
    embedded - the exact cost this feature exists to avoid."""

    def test_index_all_leaves_a_watermark_for_every_source(self, clean_watermarks, bounded_corpus):
        from app.retrieval.indexer import index_all

        assert index_all() > 0
        recorded = {r["Source"] for r in wm.list_all()}
        # Equality against the bounded set, not containment: the property is that
        # a rebuild marks EVERY source it read, so a subset check would pass a
        # rebuild that quietly skipped one - which is the exact failure this test
        # exists to catch.
        assert recorded == set(bounded_corpus), (
            f"missing: {set(bounded_corpus) - recorded}, extra: {recorded - set(bounded_corpus)}")

    def test_a_refresh_straight_after_a_rebuild_reindexes_no_cmdb_documents(self, clean_watermarks, bounded_corpus):
        """The headline behaviour: nothing from the database is re-embedded.

        The four standards ARE rewritten every run, deliberately. They live in
        Python with no watermark to advance, and making them rebuild-only meant
        "never indexed at all" - the live collection held 0 of 4, so every
        grounded question about a policy answered from node documents instead.
        Four embeddings per run is the price of that not happening again.
        """
        from app.retrieval.indexer import index_all, refresh_index

        index_all()
        result = refresh_index()
        by_source = result["by_source"]
        cmdb = {k: v for k, v in by_source.items() if k != "standard"}
        assert all(v == 0 for v in cmdb.values()), cmdb
        assert by_source.get("standard") == 4, by_source

    def test_a_first_refresh_with_no_watermarks_indexes_the_whole_corpus(self, clean_watermarks, bounded_corpus):
        """With no watermark a source cannot know what it missed, so it takes
        everything. Returning nothing would look identical to "up to date" and
        leave the index permanently empty."""
        from app.retrieval.indexer import refresh_index

        result = refresh_index()
        assert result["documents_indexed"] > 0

    def test_refresh_reports_per_source_not_just_a_total(self, clean_watermarks, bounded_corpus):
        from app.retrieval.indexer import refresh_index

        result = refresh_index()
        # bounded_corpus, not the module-level SOURCES: the fixture restricts what
        # actually runs, and asserting against the full tuple would demand a
        # watermark for sources this test never touched.
        for source in bounded_corpus:
            assert source in result["by_source"], result["by_source"]

    def test_a_stale_watermark_picks_up_only_what_followed_it(self, clean_watermarks, bounded_corpus):
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
        # Every OTHER source in scope stayed still. Derived from the fixture
        # rather than named, so this keeps proving independence if the bounded
        # set changes - naming "node" tied the assertion to a source the test
        # does not otherwise care about.
        for source in bounded_corpus:
            if source != "application":
                assert result["by_source"][source] == 0, result["by_source"]
