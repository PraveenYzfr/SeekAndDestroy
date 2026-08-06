"""Shared retry-with-backoff helper for the platform's hand-rolled httpx
clients (HttpChatModel, HttpEmbedder, GeminiEmbedder).

Real testing against Google's Gemini API during a bulk reindex surfaced a
real gap: a 429 (rate limit) needs an actual backoff before retrying, not an
immediate retry - unlike a 5xx/transport blip, retrying instantly just hits
the same rate limit again. A plain 4xx (bad request, bad auth, ...) is never
retried - that's a permanent problem, not a transient one.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx


def request_with_retry(send: Callable[[], httpx.Response], *, max_attempts: int = 3) -> httpx.Response:
    """Calls ``send()`` (expected to perform one HTTP request and return the
    raw ``httpx.Response`` without calling ``raise_for_status`` itself),
    retrying on a 429 (honoring a ``Retry-After`` header if present), a 5xx,
    or a transport-level error - up to ``max_attempts`` total tries.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = send()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if attempt == max_attempts - 1 or (status < 500 and status != 429):
                raise
            time.sleep(_retry_delay_seconds(exc.response, status))
            last_exc = exc
        except httpx.TransportError as exc:
            if attempt == max_attempts - 1:
                raise
            time.sleep(1.0)
            last_exc = exc
    raise last_exc  # pragma: no cover - loop always returns or raises above


def _retry_delay_seconds(response: httpx.Response, status: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return 5.0 if status == 429 else 1.0
