"""Daily call-count budget for real (non-mock/non-hash) external providers -
LLM chat completions and embeddings. Backed by the same cache store as LLM
narration caching (in-memory by default, Redis when SAD_CACHE__BACKEND=redis
so the count is shared across worker processes), keyed per UTC calendar day
so it resets automatically at midnight UTC with no cleanup job needed.

This is a spend/abuse guardrail, not a hard security boundary - under the
in-memory backend it's per-process only (same caveat every other in-memory
cache-backed feature in this platform already has), and even under Redis a
handful of concurrent requests right at the limit can overshoot it by one or
two calls (the check-then-atomic-increment below bounds that race tightly,
but doesn't eliminate it). That's an acceptable trade-off for what this is:
a guard against a runaway loop or a leaked key burning through a real
provider's quota, not a precise metering system.
"""

from __future__ import annotations

import structlog

from datetime import datetime, timezone

from app.cache.store import get_cache_store

_SECONDS_PER_DAY_WITH_MARGIN = 90_000  # a little over 24h so a stale key can't outlive its calendar day


logger = structlog.get_logger(__name__)


class BudgetExceededError(Exception):
    def __init__(self, namespace: str, limit: int, used: int):
        self.namespace = namespace
        self.limit = limit
        self.used = used
        super().__init__(f"daily call budget exceeded for {namespace!r}: {used}/{limit} calls used today (UTC)")


def _today_key(namespace: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"budget:{namespace}:{day}"


def check_and_increment(namespace: str, daily_limit: int) -> int:
    """Raises BudgetExceededError if ``namespace``'s daily call count is
    already at or would exceed ``daily_limit``; otherwise atomically
    increments and returns the new count. ``daily_limit <= 0`` means
    unlimited - the counter isn't even touched, so mock/hash providers (which
    always pass 0 here) have zero overhead from this mechanism.
    """
    if daily_limit <= 0:
        return 0
    store = get_cache_store()
    key = _today_key(namespace)

    current = store.get(key)
    used = int(current) if current is not None else 0
    if used >= daily_limit:
        _record_denied(namespace)
        logger.warning(
            "spend_budget.denied", namespace=namespace, limit=daily_limit, used=used,
            detail="daily call budget already spent; the request was refused before "
                   "reaching a provider",
        )
        raise BudgetExceededError(namespace, daily_limit, used)

    new_count = store.incr(key, ttl_seconds=_SECONDS_PER_DAY_WITH_MARGIN)
    if new_count > daily_limit:
        # Lost a race with a concurrent caller between the peek above and this
        # atomic increment - the increment itself is never lost or wrong, but
        # this specific call still needs to be denied.
        _record_denied(namespace)
        logger.warning(
            "spend_budget.denied", namespace=namespace, limit=daily_limit,
            used=new_count - 1,
            detail="daily call budget reached on this increment",
        )
        raise BudgetExceededError(namespace, daily_limit, new_count - 1)
    return new_count


def _record_denied(namespace: str) -> None:
    from app.observability.metrics import budget_denied_total

    budget_denied_total.labels(namespace=namespace).inc()
