"""Shared retry-with-backoff helper for the platform's hand-rolled httpx
clients (HttpChatModel, HttpEmbedder, GeminiEmbedder).

Real testing against Google's Gemini API during a bulk reindex surfaced a
real gap: a 429 (rate limit) needs an actual backoff before retrying, not an
immediate retry - unlike a 5xx/transport blip, retrying instantly just hits
the same rate limit again. A plain 4xx (bad request, bad auth, ...) is never
retried - that's a permanent problem, not a transient one.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

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


async def arequest_with_retry(
    send: Callable[[], Awaitable[httpx.Response]], *, max_attempts: int = 3
) -> httpx.Response:
    """Async twin of :func:`request_with_retry`, same policy.

    Separate rather than shared because the difference is not just ``await``:
    the backoff must be ``asyncio.sleep``. ``time.sleep`` inside a coroutine
    blocks the whole event loop - so a single provider rate-limiting us for
    five seconds would freeze every other request in the process. That is the
    exact failure this codebase converted to async to avoid, and it would be
    invisible until a 429 arrived under load.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await send()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if attempt == max_attempts - 1 or (status < 500 and status != 429):
                raise
            await asyncio.sleep(_retry_delay_seconds(exc.response, status))
            last_exc = exc
        except httpx.TransportError as exc:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(1.0)
            last_exc = exc
    raise last_exc  # pragma: no cover - loop always returns or raises above


def _retry_delay_seconds(response: httpx.Response, status: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass

    body_delay = _google_retry_delay(response)
    if body_delay is not None:
        return body_delay

    return 5.0 if status == 429 else 1.0


def _google_retry_delay(response: httpx.Response) -> float | None:
    """Google's APIs put their retry hint in the response *body*, not in a
    ``Retry-After`` header::

        {"error": {"details": [
            {"@type": ".../google.rpc.RetryInfo", "retryDelay": "27s"}]}}

    Without reading it, a 429 backs off for a flat 5s and burns its remaining
    attempts well before the quota window has actually reset - which is how a
    burst of narration calls loses the final report while the per-candidate
    explanations succeed.
    """
    try:
        details = response.json().get("error", {}).get("details", [])
    except Exception:  # noqa: BLE001 - a non-JSON error body is not worth failing over
        return None

    for detail in details:
        if "RetryInfo" not in detail.get("@type", ""):
            continue
        raw = str(detail.get("retryDelay", "")).strip()
        try:
            return float(raw[:-1]) if raw.endswith("s") else float(raw)
        except ValueError:
            return None
    return None
