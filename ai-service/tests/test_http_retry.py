"""Shared retry-with-backoff helper (app.utils.http_retry) used by every
hand-rolled httpx client in the platform (HttpChatModel, HttpEmbedder,
GeminiEmbedder). Built after live testing against Google's real Gemini API
surfaced a real gap: retrying a 429 immediately just hits the same rate
limit again - it needs an actual backoff, honoring Retry-After if given.
"""

from __future__ import annotations

import httpx
import pytest

from app.utils.http_retry import request_with_retry


def test_succeeds_on_first_try_without_sleeping(monkeypatch):
    monkeypatch.setattr("app.utils.http_retry.time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    response = request_with_retry(lambda: httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://x")))
    assert response.status_code == 200


def test_retries_on_429_honoring_retry_after_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.utils.http_retry.time.sleep", sleeps.append)
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, request=httpx.Request("POST", "https://x"))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://x"))

    response = request_with_retry(send)
    assert response.status_code == 200
    assert calls["count"] == 2
    assert sleeps == [2.0]


def test_retries_on_429_with_default_delay_when_no_retry_after_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.utils.http_retry.time.sleep", sleeps.append)
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, request=httpx.Request("POST", "https://x"))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://x"))

    request_with_retry(send)
    assert sleeps == [5.0]


def test_retries_on_5xx_with_short_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.utils.http_retry.time.sleep", sleeps.append)
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, request=httpx.Request("POST", "https://x"))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://x"))

    request_with_retry(send)
    assert sleeps == [1.0]


def test_does_not_retry_plain_4xx(monkeypatch):
    monkeypatch.setattr("app.utils.http_retry.time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        return httpx.Response(401, request=httpx.Request("POST", "https://x"))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(send)
    assert calls["count"] == 1


def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("app.utils.http_retry.time.sleep", lambda s: None)
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        return httpx.Response(429, request=httpx.Request("POST", "https://x"))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(send, max_attempts=3)
    assert calls["count"] == 3


def test_retries_on_transport_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.utils.http_retry.time.sleep", sleeps.append)
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", "https://x"))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://x"))

    response = request_with_retry(send)
    assert response.status_code == 200
    assert sleeps == [1.0]
