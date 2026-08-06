"""Embedding providers behind ``langchain_core.embeddings.Embeddings``.

Default is a deterministic, offline hashing embedder (char n-gram hashing,
L2-normalized) so the whole platform - including retrieval - runs with zero
downloaded models and zero API keys. Two real upgrades are wired in:
``sentence-transformers`` (local, optional - see requirements-optional.txt;
it has no CPython 3.14 wheel at the time of writing) and ``api`` (any
OpenAI-compatible ``/v1/embeddings`` endpoint - OpenAI, Azure OpenAI, or
Ollama - via plain ``httpx``, no vendor SDK, mirroring
``app.agents.http_chat_model.HttpChatModel``).
"""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache

import httpx
import structlog
from langchain_core.embeddings import Embeddings

from app.config import get_settings

logger = structlog.get_logger(__name__)


class DeterministicHashEmbedder(Embeddings):
    """Character-trigram hashing embedder. Same text -> same vector, always."""

    def __init__(self, dimensions: int = 384, ngram: int = 3):
        self.dimensions = dimensions
        self.ngram = ngram

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        normalized = text.lower().strip()
        if not normalized:
            return vec
        padded = f"  {normalized}  "
        for i in range(len(padded) - self.ngram + 1):
            gram = padded[i : i + self.ngram]
            h = int(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).hexdigest(), 16)
            index = h % self.dimensions
            sign = 1.0 if (h // self.dimensions) % 2 == 0 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


class HttpEmbedder(Embeddings):
    """A minimal OpenAI-chat-completions-style ``/v1/embeddings`` client.

    Deliberately dependency-light like ``HttpChatModel``: raw ``httpx``
    against ``POST {base_url}/embeddings``, which OpenAI, Azure OpenAI (with
    an ``api_version`` query param and an ``api-key`` header instead of
    ``Authorization: Bearer``) and Ollama's OpenAI-compat endpoint all speak.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        api_version: str = "",
        batch_size: int = 64,
        timeout_seconds: int = 30,
        extra_headers: dict | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.api_version = api_version
        self.batch_size = max(1, batch_size)
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self._transport = transport  # test-only hook (httpx.MockTransport); None uses real networking

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key and "api-key" not in headers:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        return headers

    def _url(self) -> str:
        url = self.base_url.rstrip("/") + "/embeddings"
        if self.api_version:
            url += f"?api-version={self.api_version}"
        return url

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts}
        for attempt in range(2):  # one retry on a transient (5xx / connection) failure
            try:
                with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
                    response = client.post(self._url(), headers=self._headers(), json=payload)
                    response.raise_for_status()
                    data = response.json()
                items = sorted(data["data"], key=lambda item: item.get("index", 0))
                return [item["embedding"] for item in items]
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


@lru_cache(maxsize=1)
def _build_embedder_and_fingerprint() -> tuple[Embeddings, str]:
    """Selects and probes the configured embedder exactly once per process.

    The fingerprint reflects what was *actually* built - including a fallback
    from ``api`` to the hash embedder - never just the raw config, so
    ``app.retrieval.vector_store`` can tell whether a persisted index's
    vectors came from the embedder that's active right now.
    """
    settings = get_settings().retrieval

    if settings.embedding_provider == "sentence-transformers":
        try:
            from langchain_huggingface import HuggingFaceEmbeddings  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "SAD_RETRIEVAL__EMBEDDING_PROVIDER=sentence-transformers requires the optional "
                "sentence-transformers/langchain-huggingface packages (see requirements-optional.txt) "
                "under a Python version with a torch wheel available."
            ) from exc
        embedder = HuggingFaceEmbeddings(model_name=settings.embedding_model)
        fingerprint = f"sentence-transformers:{settings.embedding_model}:{settings.embedding_dimensions}"
        return embedder, fingerprint

    if settings.embedding_provider == "api":
        base_url = settings.embedding_base_url or "https://api.openai.com/v1"
        is_azure_style = bool(settings.embedding_api_version)
        extra_headers = {"api-key": settings.embedding_api_key} if is_azure_style and settings.embedding_api_key else {}
        candidate = HttpEmbedder(
            base_url=base_url, model=settings.embedding_model, api_key=settings.embedding_api_key,
            api_version=settings.embedding_api_version, batch_size=settings.embedding_batch_size,
            timeout_seconds=settings.embedding_timeout_seconds, extra_headers=extra_headers,
        )
        return _probe_or_fallback(candidate, provider_label="api", dimensions=settings.embedding_dimensions, model=settings.embedding_model)

    if settings.embedding_provider == "gemini":
        from app.retrieval.gemini_embedder import DEFAULT_BASE_URL, DEFAULT_MODEL, GeminiEmbedder

        base_url = settings.embedding_base_url or DEFAULT_BASE_URL
        candidate = GeminiEmbedder(
            api_key=settings.embedding_api_key, model=settings.embedding_model or DEFAULT_MODEL,
            base_url=base_url, batch_size=settings.embedding_batch_size,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
        return _probe_or_fallback(
            candidate, provider_label="gemini", dimensions=settings.embedding_dimensions,
            model=settings.embedding_model or DEFAULT_MODEL,
        )

    embedder = DeterministicHashEmbedder(dimensions=settings.embedding_dimensions)
    return embedder, f"hash:{settings.embedding_dimensions}"


def _probe_or_fallback(candidate: Embeddings, *, provider_label: str, dimensions: int, model: str) -> tuple[Embeddings, str]:
    """Shared startup-probe logic for every real (non-hash) embedding provider:
    probe once, fall back to the hash embedder on any failure (never per-call
    at runtime - see the module docstring), and hard-fail on a dimension
    mismatch rather than silently accepting mismatched vectors.
    """
    try:
        probe_vector = candidate.embed_query("probe")
    except Exception as exc:
        logger.warning(f"embedder.{provider_label}_unavailable_falling_back_to_hash", error=str(exc), model=model)
        return DeterministicHashEmbedder(dimensions=dimensions), f"hash:{dimensions}"
    if len(probe_vector) != dimensions:
        raise ValueError(
            f"SAD_RETRIEVAL__EMBEDDING_DIMENSIONS={dimensions} does not match the "
            f"{len(probe_vector)}-dimensional vectors returned by model {model!r} via the "
            f"{provider_label!r} provider. Set SAD_RETRIEVAL__EMBEDDING_DIMENSIONS={len(probe_vector)}."
        )
    return candidate, f"{provider_label}:{model}:{dimensions}"


def get_embedder() -> Embeddings:
    return _build_embedder_and_fingerprint()[0]


def get_embedder_fingerprint() -> str:
    """Identifies (provider, model, dimensions) of the embedder actually in
    use - see app.retrieval.vector_store for how this guards persisted
    indexes against being queried by a different embedder than built them.
    """
    return _build_embedder_and_fingerprint()[1]


def reset_embedder_cache() -> None:
    _build_embedder_and_fingerprint.cache_clear()
