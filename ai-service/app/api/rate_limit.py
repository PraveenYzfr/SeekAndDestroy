"""Per-caller rate limiting on the endpoints that can spend money.

``SAD_LLM__DAILY_CALL_BUDGET`` caps what the platform spends in a day. It does
not cap how fast one caller spends it: an unthrottled loop against
``/api/investigations`` burns the entire day's budget in about ninety seconds
and every other engineer gets "budget exhausted" until midnight. A daily cap
without a rate limit is a shared wallet with no queue.

Deliberately a per-process token bucket, not a distributed one:

- It needs no Redis and no new dependency, which is what makes it land today
  rather than being planned.
- Under N replicas the effective limit is N times the configured rate. That is
  a stated, bounded overshoot rather than an unknown one - and N times a small
  number is still an enormous improvement on no limit at all.
- The real enforcement point for a public deployment is the gateway or the
  reverse proxy in front of it. This is defence in depth behind that, and the
  place that knows which endpoints are expensive.

Identity comes from the authenticated employee where there is one, so one
engineer's loop cannot exhaust another's allowance.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

import structlog
from fastapi import Depends

from app.api.auth import get_current_employee
from app.api.errors import ProblemDetailsError
from app.config import get_settings
from app.security.jwt_service import AuthenticatedEmployee

logger = structlog.get_logger(__name__)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucketLimiter:
    """``capacity`` requests, refilling at ``capacity / per_seconds``.

    A bucket rather than a fixed window: a fixed window lets a caller spend the
    whole allowance in the last second of one window and the whole of the next
    in the first second of the following one, which is the burst it was meant
    to prevent, at twice the size.
    """

    capacity: int
    per_seconds: float
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return time.monotonic()

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds). retry_after is 0 when allowed."""
        if self.capacity <= 0:
            return True, 0.0

        rate = self.capacity / self.per_seconds
        now = self._now()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=self.capacity - 1, updated_at=now)
                return True, 0.0

            bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated_at) * rate)
            bucket.updated_at = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True, 0.0
            # How long until one whole token exists again.
            return False, max(0.0, (1 - bucket.tokens) / rate)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


@lru_cache(maxsize=1)
def _llm_limiter() -> TokenBucketLimiter:
    settings = get_settings().rate_limit
    return TokenBucketLimiter(capacity=settings.llm_requests, per_seconds=settings.llm_per_seconds)


def enforce_llm_rate_limit(current: AuthenticatedEmployee = Depends(get_current_employee)) -> AuthenticatedEmployee:
    """FastAPI dependency for endpoints that can trigger model calls.

    Keyed on the authenticated employee, so one engineer's runaway loop cannot
    exhaust anyone else's allowance. Returns the caller, so an endpoint can
    depend on this *instead of* get_current_employee rather than as well as it.
    """
    allowed, retry_after = _llm_limiter().allow(f"employee:{current.employee_id}")
    if allowed:
        return current

    logger.warning(
        "ratelimit.denied", employee_id=current.employee_id, retry_after_seconds=round(retry_after, 1)
    )
    raise ProblemDetailsError(
        429, "Too many requests",
        f"Rate limit exceeded. Retry in {retry_after:.0f}s. This protects the shared daily model "
        f"budget - one caller should not be able to exhaust it for everyone.",
    )
    # No Retry-After header: ProblemDetailsError renders a body, not headers.
    # The wait is in the detail text, which is what a human reads; a client
    # that wants to back off programmatically needs the header, and that is a
    # change to the error envelope rather than to this module.


def reset_llm_limiter() -> None:
    """Test hook. The limiter is process-global by design, so a test that
    exhausts it would otherwise leak into whatever runs next."""
    _llm_limiter.cache_clear()
