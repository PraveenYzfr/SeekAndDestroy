"""Independent narrations must run together, in order, and fail one at a time.

Production investigation 16 wrote three candidate explanations at 03:24:51,
03:25:01 and 03:25:20 - strictly sequential, 46.3 seconds, for three calls that
never read each other's output. The whole investigation took 98 seconds and most
of it was addition rather than any single slow thing.
"""

from __future__ import annotations

import threading
import time

from app.graph.nodes import _narrate_all


class _Model:
    def __init__(self, value):
        self.value = value

    def model_dump(self):
        return {"v": self.value}


def test_order_follows_the_input_not_completion():
    """The list is ranked. A narration order that disagreed with the ranking
    would put the second-best cluster first in the report."""
    delays = {"a": 0.06, "b": 0.01, "c": 0.03}

    def narrate(item):
        time.sleep(delays[item])
        return _Model(item)

    out = _narrate_all(["a", "b", "c"], narrate, lambda i, e: None)
    assert [r["v"] for r in out] == ["a", "b", "c"]


def test_they_actually_overlap():
    """The point of the change. Sequential execution of three 100ms calls takes
    300ms; concurrent takes about 100ms. Asserting well under the sequential
    total rather than a tight bound, so this does not go flaky on a busy box."""
    def narrate(item):
        time.sleep(0.1)
        return _Model(item)

    started = time.perf_counter()
    _narrate_all(["a", "b", "c"], narrate, lambda i, e: None)
    assert time.perf_counter() - started < 0.25


def test_one_failure_drops_only_itself():
    """Same behaviour as the loop this replaced - one cluster failing to narrate
    never cost the others."""
    seen = []

    def narrate(item):
        if item == "b":
            raise RuntimeError("provider said no")
        return _Model(item)

    out = _narrate_all(["a", "b", "c"], narrate, lambda i, e: seen.append(i))
    assert [r["v"] for r in out] == ["a", "c"]
    assert seen == ["b"]


def test_a_single_item_does_not_spawn_a_pool():
    """One narration is the common case for right-sizing, and a thread pool for
    one item is pure overhead."""
    threads = set()

    def narrate(item):
        threads.add(threading.get_ident())
        return _Model(item)

    _narrate_all(["only"], narrate, lambda i, e: None)
    assert threads == {threading.get_ident()}


def test_empty_input_makes_no_calls():
    calls = []
    assert _narrate_all([], lambda i: calls.append(i), lambda i, e: None) == []
    assert calls == []
