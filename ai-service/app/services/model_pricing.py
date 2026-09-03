"""What a model call cost, priced at the moment it happened.

WHY THIS EXISTS
---------------
The platform recorded PromptTokens, CompletionTokens and ModelIdentity for every
call and never converted them to money. "What did this investigation cost" and
"which model is cheaper on the same evidence" were unanswerable, and the daily
budget enforced a ceiling in CALLS - which treats a 200-token classification and a
40,000-token report as the same unit of spend. One expensive model could exhaust a
rupee budget at 2% of its call budget.

THE RULE THAT SHAPES THIS MODULE
--------------------------------
**A cost is a fact about an event, not a property of a model.**

price_for() is consulted when a call is made, and the resulting unit prices are
written onto the call row alongside the computed cost. Nothing re-derives
historical cost by joining to a current price table.

That distinction is the whole design and it is the part most likely to be
"simplified" later, so: the first time a vendor changes their pricing, a derived
view silently re-prices every call ever made. Last quarter's spend changes. The
figure someone reported stops matching the figure in the database, with no event to
explain the difference and no way to reconstruct what was actually charged.

UNKNOWN IS NOT ZERO
-------------------
A model with no price row returns None, not 0.0. A zero would flow into a spend
total and read as "this cost nothing" rather than "we do not know what this cost" -
and it would do so most often for a newly added model, which is exactly when
someone is watching spend. Callers persist NULL and the daily view counts those
rows separately as UnpricedCalls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.repositories.base import T, fetch_all

_MILLION = Decimal(1_000_000)

#: Cost is stored as DECIMAL(12,6). Six places because a cheap call on a cheap
#: model is genuinely worth fractions of a cent, and rounding those to four would
#: quantise a large share of traffic to zero - which looks like free usage.
_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class Price:
    """The prices in force for one model at one moment."""
    provider: str
    model_identity: str
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str


@dataclass(frozen=True)
class CallCost:
    """What one call cost, with the prices that produced it.

    The unit prices travel with the cost so the caller can persist all three. A
    cost without the price that produced it cannot be audited - you can see the
    number and not how it was reached.
    """
    cost: Decimal | None
    input_per_million: Decimal | None
    output_per_million: Decimal | None
    currency: str | None

    @property
    def is_priced(self) -> bool:
        return self.cost is not None


UNPRICED = CallCost(None, None, None, None)


def price_for(model_identity: str, *, at: datetime | None = None) -> Price | None:
    """The price in force for a model at a moment, or None if we do not know.

    ``at`` defaults to now, which is correct for a call being made. It is a
    parameter so a backfill can price a historical call at the rate that applied
    then rather than at today's rate.
    """
    if not model_identity:
        return None
    table = T("ModelPrice")
    moment = at or datetime.now(timezone.utc).replace(tzinfo=None)

    rows = fetch_all(
        f"""
        SELECT TOP 1 Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Currency
        FROM   {table}
        WHERE  ModelIdentity = :model
          AND  EffectiveFrom <= :at
          AND  (EffectiveTo IS NULL OR EffectiveTo > :at)
        ORDER BY EffectiveFrom DESC
        """,
        {"model": model_identity, "at": moment},
    )
    if not rows:
        rows = _dated_snapshot_rows(model_identity, moment)
    if not rows:
        return None
    r = rows[0]
    return Price(
        provider=r["Provider"],
        model_identity=r["ModelIdentity"],
        input_per_million=Decimal(str(r["InputPerMillion"])),
        output_per_million=Decimal(str(r["OutputPerMillion"])),
        currency=r["Currency"],
    )


def _dated_snapshot_rows(model_identity: str, moment: datetime) -> list:
    """Price a DATED SNAPSHOT from its base model's row.

    Providers list prices under a base name and serve the model under a dated
    one. The Model Settings dropdown is built from each provider's live /models
    listing, so what an operator selects - and what therefore reaches
    ModelIdentity - is the dated id:

        claude-haiku-4-5-20251001   priced as claude-haiku-4-5
        gpt-5-nano-2025-08-07       priced as gpt-5-nano
        gemini-3.5-flash-lite-002   priced as gemini-3.5-flash-lite

    An exact-match-only lookup left every one of those unpriced, which is the
    silent kind of wrong: the call is recorded, the spend total is simply
    smaller than the invoice.

    LONGEST PREFIX WINS, and that is load-bearing rather than tidy. "gpt-5" is
    a prefix of "gpt-5-mini", which is a prefix of "gpt-5-mini-2025-08-07".
    Taking the longest match prices the mini snapshot as a mini; taking any
    match could price it as the full model, at five times the rate.

    The separator check is the other half. Requiring the remainder to start
    with "-" stops "gpt-4o" from claiming "gpt-4omni" - a model it has nothing
    to do with - so this only ever matches a genuine suffix on a real
    boundary, never a coincidental string prefix.

    A row is still returned only if it was in force at ``moment``: a snapshot
    inherits its base model's price history, not just today's price.

    ASSUMES MODEL IDS CARRY NO LIKE WILDCARDS. ModelIdentity is interpolated
    into a LIKE pattern, so an id containing '%' or '_' would match more than
    itself - '_' matches any single character. No provider currently uses
    either (ids are letters, digits, '.', '-' and '/'), and a wrong match here
    would misprice a call rather than fail loudly, so it is written down.
    """
    rows = fetch_all(
        f"""
        SELECT Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Currency
        FROM   {T("ModelPrice")}
        WHERE  :model LIKE ModelIdentity + '-%'
          AND  EffectiveFrom <= :at
          AND  (EffectiveTo IS NULL OR EffectiveTo > :at)
        """,
        {"model": model_identity, "at": moment},
    )
    if not rows:
        return []
    return [max(rows, key=lambda r: len(str(r["ModelIdentity"])))]


def cost_of(model_identity: str, prompt_tokens: int | None,
            completion_tokens: int | None, *, at: datetime | None = None) -> CallCost:
    """Cost of one call, plus the unit prices used, ready to persist.

    Returns UNPRICED when the model has no price row or when token counts are
    absent - both are "we do not know", and both must stay distinguishable from
    zero once they reach a spend total.
    """
    price = price_for(model_identity, at=at)
    if price is None:
        return UNPRICED
    if prompt_tokens is None and completion_tokens is None:
        # A priced model whose usage the provider did not report. The price is
        # known and the consumption is not, so the cost is still unknown.
        return CallCost(None, price.input_per_million, price.output_per_million, price.currency)

    inp = Decimal(prompt_tokens or 0)
    out = Decimal(completion_tokens or 0)
    total = (inp * price.input_per_million + out * price.output_per_million) / _MILLION
    return CallCost(
        cost=total.quantize(_QUANTUM, rounding=ROUND_HALF_UP),
        input_per_million=price.input_per_million,
        output_per_million=price.output_per_million,
        currency=price.currency,
    )


def estimate(model_identity: str, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """Cost only, for a caller that just wants the number - a budget check
    deciding whether a call can be afforded before making it."""
    return cost_of(model_identity, prompt_tokens, completion_tokens).cost
