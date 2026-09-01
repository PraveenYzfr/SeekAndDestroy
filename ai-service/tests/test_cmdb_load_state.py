"""The load-state guard itself, which otherwise has a branch that never fires.

require_loaded_graph exists to turn a half-loaded database into a red test
instead of a quiet skip. On a healthy machine the partial branch never executes,
so without these tests the guard would be exactly the thing it was written to
prevent: code that looks like protection and has never once been shown to work.

The three states are simulated by substituting the query layer rather than by
mutating the CMDB - those tables belong to another session, and a test that
truncates them to prove a point would take the estate down to make an assertion.
"""

from __future__ import annotations

import pytest

from tests import _cmdb_load_state as L


def _counts(monkeypatch, cis: int, edges: int, dangling: int = 0):
    """Substitute fetch_all with canned counts, in call order.

    cmdb_load_state issues its queries in a fixed sequence - CIs, edges, then
    dangling edges - and returns before the third when the first two settle the
    question. The queue is consumed in that order.
    """
    queue = [[{"C": cis}], [{"C": edges}], [{"C": dangling}]]

    def fake(*_args, **_kwargs):
        return queue.pop(0) if queue else [{"C": 0}]

    monkeypatch.setattr(L, "fetch_all", fake)


class TestStateDetection:
    def test_nothing_loaded_is_empty(self, monkeypatch):
        _counts(monkeypatch, cis=0, edges=0)
        assert L.cmdb_load_state()[0] == L.EMPTY

    def test_a_complete_graph_is_loaded(self, monkeypatch):
        _counts(monkeypatch, cis=54_555, edges=85_526, dangling=0)
        assert L.cmdb_load_state()[0] == L.LOADED

    def test_a_small_but_complete_graph_is_also_loaded(self, monkeypatch):
        """The reason this is not a row-count threshold.

        Before tonight the estate held 4,290 edges and that was complete and
        correct. A floor tuned for tonight's 85,000 would have called last week's
        database broken; a floor tuned for last week would pass a load that is
        5% done. Size cannot tell "partial" from "smaller".
        """
        _counts(monkeypatch, cis=3_535, edges=4_290, dangling=0)
        assert L.cmdb_load_state()[0] == L.LOADED

    def test_edges_pointing_at_missing_cis_is_partial(self, monkeypatch):
        """The signal that is independent of estate size: a load in flight writes
        CIs and relationships at different moments, so mid-write there are edges
        whose endpoints do not exist. A complete CMDB of any size has none."""
        _counts(monkeypatch, cis=20_000, edges=40_000, dangling=137)
        state, detail = L.cmdb_load_state()
        assert state == L.PARTIAL
        assert "137" in detail

    def test_cis_without_edges_is_partial(self, monkeypatch):
        _counts(monkeypatch, cis=54_555, edges=0)
        assert L.cmdb_load_state()[0] == L.PARTIAL

    def test_edges_without_cis_is_partial(self, monkeypatch):
        _counts(monkeypatch, cis=0, edges=85_526)
        assert L.cmdb_load_state()[0] == L.PARTIAL

    def test_an_unreachable_database_reads_as_empty(self, monkeypatch):
        """Skip, not fail. A machine with no database at all must be able to run
        the rest of the suite."""
        def boom(*_a, **_k):
            raise RuntimeError("no such table")

        monkeypatch.setattr(L, "fetch_all", boom)
        assert L.cmdb_load_state()[0] == L.EMPTY


class TestGuardBehaviour:
    # pytest.skip and pytest.fail raise Skipped and Failed, which descend from
    # BaseException rather than Exception. `pytest.raises(Exception)` does not
    # catch either: the skip case silently skipped ITS OWN test and the fail case
    # propagated as a real failure. pytest.skip.Exception / pytest.fail.Exception
    # name the classes directly, which is what these assertions actually mean.
    def test_it_skips_on_empty(self, monkeypatch):
        _counts(monkeypatch, cis=0, edges=0)
        with pytest.raises(pytest.skip.Exception):
            L.require_loaded_graph()

    def test_it_fails_on_partial(self, monkeypatch):
        """The whole point. A half-loaded estate must go red, not green.

        This suite once went from 22 passing to 14 skipped during a reload and
        reported success.
        """
        _counts(monkeypatch, cis=20_000, edges=40_000, dangling=9)
        with pytest.raises(pytest.fail.Exception) as exc:
            L.require_loaded_graph()
        assert "mid-load" in str(exc.value)

    def test_a_partial_load_does_not_merely_skip(self, monkeypatch):
        """Stated separately because it is the distinction the guard exists for,
        and because the first version of these tests could not tell the two
        outcomes apart."""
        _counts(monkeypatch, cis=20_000, edges=40_000, dangling=9)
        with pytest.raises(BaseException) as exc:
            L.require_loaded_graph()
        assert not isinstance(exc.value, pytest.skip.Exception)

    def test_it_is_silent_when_loaded(self, monkeypatch):
        _counts(monkeypatch, cis=54_555, edges=85_526, dangling=0)
        L.require_loaded_graph()
