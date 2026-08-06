"""Platform-specific Prometheus metrics, scraped at ``GET /metrics``
(wired up in app.main via prometheus-fastapi-instrumentator, which also adds
the standard HTTP request-rate/latency/status metrics automatically).

These are deliberately narrow: what would an on-call engineer actually need
mid-incident? Is a real LLM/embedding provider failing or falling back, is
the narration cache doing anything, is the spend budget about to bite, is
investigation volume normal. Nothing here duplicates a number the
deterministic engines already compute - this is purely operational signal.
"""

from __future__ import annotations

from prometheus_client import Counter

llm_calls_total = Counter(
    "sad_llm_calls_total", "Real (non-mock) LLM chat-model calls attempted", ["provider", "outcome"]
)
llm_fallback_total = Counter(
    "sad_llm_fallback_total", "Times the LLM fallback chain moved on to the next configured provider",
    ["from_provider"],
)
embedding_calls_total = Counter(
    "sad_embedding_calls_total", "Real (non-hash) embedding provider calls attempted", ["provider", "outcome"]
)
narration_cache_total = Counter(
    "sad_narration_cache_total", "LLM narration cache lookups in app.agents.structured.run_structured",
    ["result"],  # hit | miss
)
budget_denied_total = Counter(
    "sad_budget_denied_total", "Calls denied by a SAD_LLM__DAILY_CALL_BUDGET / "
    "SAD_RETRIEVAL__EMBEDDING_DAILY_CALL_BUDGET spend guardrail", ["namespace"],
)
investigations_total = Counter(
    "sad_investigations_total", "Investigations created, by type", ["investigation_type"]
)
