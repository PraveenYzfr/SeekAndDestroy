"""Google Gemini's native embeddings API.

Deliberately kept out of ``embedder.py``'s ``HttpEmbedder``: Gemini's REST
shape isn't OpenAI-wire-compatible (different endpoint, request body, response
body, and auth header - see module docstring in ``embedder.py`` for why this
can't just be another config value on that class), so it gets its own thin
``httpx``-only client instead, following the same style as ``HttpEmbedder``
and ``app.agents.http_chat_model.HttpChatModel``: no vendor SDK.

Request/response shape (Generative Language API, ``v1beta``):

    POST {base_url}/models/{model}:batchEmbedContents
    {"requests": [{"model": "models/{model}", "content": {"parts": [{"text": "..."}]}}]}

    -> {"embeddings": [{"values": [0.1, 0.2, ...]}]}

Auth is the ``x-goog-api-key`` header, not ``Authorization: Bearer``.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.embeddings import Embeddings

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-embedding-001"


class GeminiEmbedder(Embeddings):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        batch_size: int = 64,
        timeout_seconds: int = 30,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.batch_size = max(1, batch_size)
        self.timeout_seconds = timeout_seconds
        self._transport = transport  # test-only hook (httpx.MockTransport); None uses real networking

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

    def _model_path(self) -> str:
        return self.model if self.model.startswith("models/") else f"models/{self.model}"

    def _url(self) -> str:
        return f"{self.base_url}/{self._model_path()}:batchEmbedContents"

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        model_path = self._model_path()
        payload: dict[str, Any] = {
            "requests": [{"model": model_path, "content": {"parts": [{"text": t}]}} for t in texts]
        }
        for attempt in range(2):  # one retry on a transient (5xx / connection) failure
            try:
                with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
                    response = client.post(self._url(), headers=self._headers(), json=payload)
                    response.raise_for_status()
                    data = response.json()
                return [item["values"] for item in data["embeddings"]]
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and exc.response.status_code >= 500:
                    continue
                raise
            except httpx.TransportError:
                if attempt == 0:
                    continue
                raise
        raise AssertionError("unreachable")  # loop always returns or raises

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[i : i + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]
