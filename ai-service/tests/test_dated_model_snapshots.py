"""A dated model snapshot is priced from its base model's row.

Providers publish a price for "claude-haiku-4-5" and serve the model as
"claude-haiku-4-5-20251001". The Model Settings dropdown is built from each
provider's live /models listing, so the dated id is what an operator picks and
what reaches ModelIdentity - and an exact-match-only lookup left every one of
them unpriced. That is the silent kind of wrong: the call is still recorded,
the spend total is just smaller than the invoice.

These test the SELECTION, with the database stubbed. The SQL half was checked
against a real SQL Server: nine cases including the two that matter, and both
resolved to the more specific model.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import model_pricing


def _row(identity: str, inp: str = "1.0", out: str = "2.0") -> dict:
    return {
        "Provider": "test", "ModelIdentity": identity,
        "InputPerMillion": inp, "OutputPerMillion": out, "Currency": "USD",
    }


@pytest.fixture
def candidates(monkeypatch):
    """Stands in for the LIKE query: the caller decides what it returns."""
    box = {}

    def stub(sql, params):
        return box.get("rows", [])

    monkeypatch.setattr(model_pricing, "fetch_all", stub)
    return box


def _pick(box, rows):
    box["rows"] = rows
    got = model_pricing._dated_snapshot_rows("irrelevant", datetime(2026, 9, 4))
    return got[0]["ModelIdentity"] if got else None


class TestLongestPrefixWins:
    def test_the_more_specific_model_is_chosen(self, candidates):
        """"gpt-5" is a prefix of "gpt-5-mini". Taking any match could price a
        mini snapshot as the full model, at five times the rate."""
        assert _pick(candidates, [_row("gpt-5"), _row("gpt-5-mini")]) == "gpt-5-mini"

    def test_lite_is_not_swallowed_by_its_bigger_sibling(self, candidates):
        """gemini-3.5-flash-lite is 0.30/2.50; gemini-3.5-flash is 1.50/9.00.
        Getting this backwards overcharges every judge call fivefold."""
        assert _pick(
            candidates, [_row("gemini-3.5-flash"), _row("gemini-3.5-flash-lite")]
        ) == "gemini-3.5-flash-lite"

    def test_order_from_the_database_does_not_decide_it(self, candidates):
        """No ORDER BY in the query - the choice must not depend on how rows
        happen to arrive."""
        rows = [_row("gpt-5-mini"), _row("gpt-5")]
        assert _pick(candidates, rows) == "gpt-5-mini"
        assert _pick(candidates, list(reversed(rows))) == "gpt-5-mini"

    def test_nothing_matching_returns_nothing(self, candidates):
        """An unknown model stays UNPRICED. Cost-unknown is a first-class value
        here and must not become a guess."""
        assert _pick(candidates, []) is None
