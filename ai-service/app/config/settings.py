"""Central configuration for the SeekAndDestroy AI service.

Every tunable lives here. In particular the SQL Server connection is assembled
in exactly one place (:meth:`DatabaseSettings.odbc_connection_string`) so no
class anywhere else in the platform needs to know how to build one.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
AI_SERVICE_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = AI_SERVICE_ROOT / ".env"


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class DatabaseSettings(_Base):
    """SQL Server connection settings.

    Defaults mirror the connection string supplied with the specification::

        Data Source=LAPTOP-R6U8H616;Initial Catalog=PraveenDB;
        Integrated Security=True;Persist Security Info=False;Pooling=False;
        MultipleActiveResultSets=False;Encrypt=False;
        TrustServerCertificate=False;Application Name=SeekAndDestroy;
        Command Timeout=0;
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_prefix="SAD_DB__",
        extra="ignore",
        case_sensitive=False,
    )

    server: str = "LAPTOP-R6U8H616"
    database: str = "PraveenDB"
    schema_name: str = Field(default="sad", alias="schema")
    integrated_security: bool = True
    username: str = ""
    password: str = ""
    encrypt: bool = False
    trust_server_certificate: bool = False
    pooling: bool = False
    multiple_active_result_sets: bool = False
    persist_security_info: bool = False
    application_name: str = "SeekAndDestroy"
    command_timeout: int = 0
    odbc_driver: str = "ODBC Driver 17 for SQL Server"

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        # The schema name is interpolated into DDL/queries as an identifier, so
        # it can never come from anywhere untrusted and must look like one.
        if not value.replace("_", "").isalnum():
            raise ValueError(f"invalid schema identifier: {value!r}")
        return value

    @property
    def odbc_connection_string(self) -> str:
        """Raw ODBC connection string (also used by the MCP server)."""
        parts = [
            f"DRIVER={{{self.odbc_driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            f"Encrypt={'yes' if self.encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
            f"MARS_Connection={'yes' if self.multiple_active_result_sets else 'no'}",
            f"APP={self.application_name}",
        ]
        if self.integrated_security:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.username}")
            parts.append(f"PWD={self.password}")
        return ";".join(parts) + ";"

    @property
    def sqlalchemy_url(self) -> str:
        return "mssql+pyodbc:///?odbc_connect=" + quote_plus(self.odbc_connection_string)

    @property
    def dotnet_connection_string(self) -> str:
        """The equivalent SqlClient connection string, for documentation/tooling."""
        parts = [
            f"Data Source={self.server}",
            f"Initial Catalog={self.database}",
            f"Integrated Security={self.integrated_security}",
            f"Persist Security Info={self.persist_security_info}",
            f"Pooling={self.pooling}",
            f"MultipleActiveResultSets={self.multiple_active_result_sets}",
            f"Encrypt={self.encrypt}",
            f"TrustServerCertificate={self.trust_server_certificate}",
            f"Application Name={self.application_name}",
            f"Command Timeout={self.command_timeout}",
        ]
        return ";".join(parts) + ";"


class LlmSettings(_Base):
    # env_nested_delimiter lets SAD_LLM__PROVIDER_KEYS__GROQ populate
    # provider_keys["groq"]. Verified not to disturb the flat variables: none of
    # SAD_LLM__PROVIDER, __MODEL, __API_KEY or __MAX_OUTPUT_TOKENS contains a
    # second "__" after the prefix, so nothing that resolved before splits now.
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_LLM__", extra="ignore",
        case_sensitive=False, env_nested_delimiter="__",
    )

    # "gemini" speaks Google's native generateContent API - a different wire
    # format from the OpenAI-compatible providers, so it has its own client
    # (app/agents/gemini_chat_model.py), exactly like the embedder does.
    provider: Literal[
        "mock", "openai", "azure-openai", "ollama", "gemini", "deepseek", "groq", "anthropic"
    ] = "mock"
    model: str = "seek-and-destroy-mock"
    temperature: float = 0.0
    #: Ceiling on generated tokens, NOT a reservation - billing is per token
    #: actually produced, so raising this costs nothing for any call that already
    #: fits. Only calls that were being truncated generate more.
    #:
    #: 2048 was silently breaking every report on the cost-first provider.
    #: Reasoning models - DeepSeek, OpenAI's o-series - spend their thinking
    #: against this same budget, and deepseek spends roughly 2,100 tokens on it.
    #: So the cap was consumed mid-thought, content came back empty, and the
    #: reasoning tokens were billed anyway: about $0.00225 a call to receive
    #: nothing. The fix converts waste into output rather than adding spend.
    max_output_tokens: int = 8192
    timeout_seconds: int = 60
    base_url: str = ""
    api_key: str = ""
    #: Per-provider credentials, so two providers can run at once.
    #:
    #: Every role could already choose a different MODEL. None could choose a
    #: different PROVIDER, because api_key is process-wide: pointing `planning`
    #: at Groq while the default was DeepSeek built a Groq client with DeepSeek's
    #: key and got a 401 that reads as a bad credential rather than a design
    #: limit. That is the whole blocker on running a fast model for extraction
    #: and a reasoning model for reporting.
    #:
    #: api_key remains the fallback for any provider without its own entry, so a
    #: single-provider deployment needs no change at all and nothing that works
    #: today stops working.
    provider_keys: dict[str, str] = {}
    azure_endpoint: str = ""
    azure_deployment: str = ""
    azure_api_version: str = "2024-10-21"
    ollama_base_url: str = "http://localhost:11434"

    def key_for(self, provider: str) -> str:
        """The credential for one provider.

        api_key is the fallback ONLY for the configured default provider, because
        that is plainly whose key it is. It used to serve any provider, which made
        a MISSING credential indistinguishable from a WRONG one: selecting Groq
        with no Groq key sent DeepSeek's, and the screen reported "401
        Unauthorized". The natural response is to re-issue a credential that was
        never broken - so the fallback was actively misleading, not merely loose.

        Now an unconfigured provider resolves to empty, and the factory says
        which variable to set.

        Lowercased on lookup: pydantic lowercases the env-var segment, and an
        operator writing PROVIDER_KEYS__Groq should not get a silently empty key.
        """
        name = (provider or "").lower()
        own = (self.provider_keys or {}).get(name)
        if own:
            return own
        return self.api_key if name == (self.provider or "").lower() else ""
    #: Overrides the `provider=` label on metrics. Any OpenAI-compatible
    #: endpoint - Groq, Together, Fireworks, whatever ships next - runs through
    #: provider="openai" with a custom base_url, which would file every one of
    #: them under "openai" and quietly corrupt exactly the per-provider cost and
    #: latency comparison this platform exists to make. Set this and the enum
    #: never needs another entry.
    provider_label: str = ""
    max_input_chars: int = 24_000
    # Ordered, comma-separated list of additional providers to try if `provider`
    # (the primary) raises - e.g. "azure-openai,ollama". Each listed provider
    # reuses this same settings object's provider-specific fields (azure_endpoint/
    # azure_deployment for azure-openai, ollama_base_url for ollama, etc.), so
    # they must already be filled in for any fallback actually to work.
    #: Where narration goes when the primary provider is down.
    #:
    #: Defaults to OpenAI at comparable capability. "Comparable" is the point: a
    #: fallback that is markedly weaker than the primary turns an outage into a
    #: silent quality drop, which is worse than an error because nobody
    #: investigates it. gpt-4o is the equal-weight choice against a reasoning
    #: primary; gpt-4o-mini would be cheaper and would quietly change the answers.
    #:
    #: Both are settable on the Model Settings screen, where "fallback" appears
    #: as a role like any other.
    #: Three legs, not one. The chain was deepseek -> openai, and a real
    #: right-sizing report failed with:
    #:
    #:     deepseek returned no content (finish_reason=length, 25582 chars of
    #:       reasoning)
    #:     openai 429 Too Many Requests
    #:     all LLM providers failed: ['deepseek', 'openai']
    #:
    #: Two independent failures, and the chain had nothing left. The user got
    #: "Report narration unavailable" on an investigation whose findings were
    #: computed correctly - five explanations sat in the response, unnarrated.
    #:
    #: A two-leg chain survives ONE provider having a bad moment. Rate limits and
    #: reasoning overflows are not rare enough for that, and they are independent
    #: causes, so they co-occur at exactly the rate you would expect.
    fallback_providers: str = "openai,groq,gemini"
    #: The model the FALLBACK provider runs.
    #:
    #: This exists because it was missing, and its absence made the whole chain
    #: inert: build_chat_model() built every fallback provider with
    #: settings.model, so an OpenAI fallback requested "deepseek-v4-flash" and
    #: 404d. The backup was guaranteed to fail in exactly the moment it was
    #: needed, and nothing exercised it because the chain was empty by default.
    #:
    #: Empty means "use that provider's own default", which raises for the
    #: providers whose ids churn rather than guessing one.
    #: The model for a fallback leg whose provider is not named in
    #: fallback_models below. Kept for compatibility with a single-provider
    #: chain; on a multi-provider chain it is almost always wrong.
    fallback_model: str = "gpt-4o"

    #: Per-provider fallback models, because ONE model name cannot serve a chain
    #: of different providers.
    #:
    #: This was the shape of a bug already fixed once for the primary: every
    #: chain member was built with settings.model, so an OpenAI backup behind a
    #: DeepSeek primary asked OpenAI for "deepseek-v4-flash" and 404d - the
    #: backup guaranteed to fail at the moment it was needed. Widening the chain
    #: to groq and gemini reintroduces exactly that, because a single
    #: fallback_model of "gpt-4o" is a 404 on both.
    #:
    #: Every value here was checked against the provider's own /models listing
    #: rather than written from memory. I set the judge to
    #: llama-3.3-70b-versatile earlier tonight on recall alone; Groq does not
    #: serve it, and a real call returned 404.
    #:
    #: SAD_LLM__FALLBACK_MODELS__GROQ=... overrides one leg.
    fallback_models: dict[str, str] = {
        "openai": "gpt-4o",
        "groq": "openai/gpt-oss-20b",
        "gemini": "gemini-3.5-flash",
    }

    def fallback_model_for(self, provider: str) -> str:
        """The model this leg should request. Never another leg's."""
        return self.fallback_models.get(provider) or self.fallback_model
    # Spend/abuse control for real providers: max real (non-mock) chat-model calls
    # allowed per UTC calendar day, process-wide (Redis-backed and thus shared
    # across workers when SAD_CACHE__BACKEND=redis; per-process otherwise). 0 (the
    # default) means unlimited - mock mode is always unlimited regardless of this
    # value, since it costs nothing and calls nothing external.
    daily_call_budget: int = 0

    # --- tiers -------------------------------------------------------------
    # Roles map to tiers (app/agents/tiers.py); tiers map to the models below.
    # Blank means "use provider/model above", so an estate that has never
    # touched a tier behaves exactly as it did before tiers existed.
    #
    # The point of the slot is bulk movement: SAD_LLM__CHEAP_PROVIDER=groq moves
    # every cheap role at once, and back again, without editing each one and
    # remembering which were changed.
    cheap_provider: str = ""
    cheap_model: str = ""
    costly_provider: str = ""
    costly_model: str = ""

    #: Move individual roles between tiers without a code change:
    #: "narration=costly,grounded_qa=cheap". Generalises AutoCoder's
    #: AUTOCODER_CODING_TIER to every role.
    role_tiers: str = ""

    #: Escape hatch. One provider for everything, ignoring tiers and per-role
    #: overrides alike. For an incident - a provider is down and the estate has
    #: to keep answering - not for configuration. It outranks the admin screen
    #: deliberately, so recovery does not require finding who set what.
    force_single: str = ""

    # -- The judge, kept independent of the authors ------------------------
    #: The judge grades answers written by the other roles. If it runs on the
    #: same model as the role that WROTE an answer, its verdict is self-judged:
    #: stored for disclosure, excluded from every headline score, and therefore
    #: worth nothing.
    #:
    #: That is not a corner case, it is the default. Every role resolves to
    #: SAD_LLM__PROVIDER unless something says otherwise, so out of the box the
    #: judge was always the author and every verdict it ever produced was
    #: discarded. The feature ran, cost a call per answer, and emitted nothing.
    #:
    #: So the judge gets its own default, resolved ABOVE the tier and base config
    #: and BELOW the admin screen - see llm_factory.resolve_role. Independence is
    #: the requirement here, not capability: the judge needs to be a DIFFERENT
    #: model, not a better one, and a cheap fast one is ideal because it runs off
    #: the request path.
    #:
    #: Blank disables the default and lets the judge fall through to the tier or
    #: base config, which is the old self-judging behaviour - available
    #: deliberately, for anyone who wants one provider for everything.
    #: Verified against the account's own /models listing rather than written
    #: from memory. The first value here was llama-3.3-70b-versatile, which this
    #: Groq account does not serve - a real judge call returned 404 and the
    #: verdict came back as an error, which the platform records as "judge
    #: unavailable" rather than as anything alarming. A judge configured to a
    #: model that does not exist fails exactly like a judge nobody configured.
    judge_provider: str = "groq"
    judge_model: str = "openai/gpt-oss-20b"

    # -- Answer evaluation -------------------------------------------------
    #: Grade every delivered answer and keep the verdict. Off means the platform
    #: still refuses a narration that drifts - that guard is not a setting - but
    #: nothing records how often, and no answer is ever scored.
    evaluate_answers: bool = True

    #: Share of answers that get an LLM judge, 0.0-1.0.
    #:
    #: 1.0 by default because "a judge for each final output" is what was asked
    #: for. The dial exists so the trade-off stays visible: judging is one extra
    #: model call per investigation. It runs AFTER the answer is returned, so it
    #: costs no user-facing latency, but it does cost a call.
    #:
    #: Lowering this makes judge scores a SAMPLE and does NOT reduce the
    #: deterministic fidelity checks, which are arithmetic over rows that already
    #: exist and are always run - they are the half that catches an invented
    #: figure, and sampling them would save nothing at the price of the signal
    #: that matters most.
    judge_sample_rate: float = 1.0


    @property
    def role_tier_map(self) -> dict[str, str]:
        from app.agents.tiers import parse_role_tiers

        return parse_role_tiers(self.role_tiers)

    @property
    def fallback_provider_list(self) -> list[str]:
        return [p.strip() for p in self.fallback_providers.split(",") if p.strip()]


class RetrievalSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_RETRIEVAL__", extra="ignore", case_sensitive=False
    )

    backend: Literal["qdrant", "memory"] = "memory"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    collection: str = "seekanddestroy"
    #: dense  - vector similarity only. What this platform did before hybrid.
    #: sparse - BM25 only. Exact tokens, no semantics. Mostly useful as a
    #:          baseline when measuring what the dense half contributes.
    #: hybrid - both, fused with Reciprocal Rank Fusion. The default.
    #:
    #: Switchable at QUERY time, not index time: indexing always writes both
    #: vectors, so flipping this needs no reindex. If the mode were baked into
    #: the index, comparing modes would mean re-embedding the whole corpus for
    #: each run, which is how a comparison ends up never being made.
    search_mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    #: Candidates each half retrieves before fusion. Fusion needs a deeper pool
    #: than the final top_k or it has nothing to re-order - a document ranked
    #: 30th by dense and 2nd by sparse is exactly what hybrid exists to surface,
    #: and it is invisible if both halves only return 8.
    hybrid_prefetch: int = 50
    embedding_provider: Literal["hash", "sentence-transformers", "api", "gemini"] = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    # "api" provider: any OpenAI-compatible /v1/embeddings endpoint (OpenAI, Azure
    # OpenAI, Ollama). embedding_api_version set => Azure-style auth (api-key header +
    # ?api-version= query param) instead of Authorization: Bearer.
    # "gemini" provider: Google's native embedContent/batchEmbedContents API - a
    # different wire format from the OpenAI-compatible providers, so it's a separate
    # client (app/retrieval/gemini_embedder.py), not just different config on the same
    # HttpEmbedder class. Reuses embedding_api_key/embedding_base_url/embedding_model.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_api_version: str = ""
    embedding_batch_size: int = 64
    embedding_timeout_seconds: int = 30
    #: Attempts per embedding call before the run fails, backoff included.
    #:
    #: Higher than the shared default of 3 because of what the 429s here
    #: actually are. The quota Google names when it refuses is
    #: `global_embed_content_requests_per_minute_per_base_model` - a pool shared
    #: across the base model, not this project's allowance. Measured 2026-09-01:
    #: this project peaked at 1.79K of 3K RPM with unlimited requests per day,
    #: and a batch of 64 was refused seconds before 100 texts went through
    #: untouched. So a refusal says the pool was busy, not that we were greedy.
    #:
    #: Pacing cannot fix that - slowing down does not reduce the chance of
    #: landing in someone else's burst, it just lengthens the window in which
    #: one can happen. Surviving the refusal does. Six attempts against the
    #: backoff in app.utils.http_retry, which honours Google's own retryDelay,
    #: covers a burst comfortably; three did not, and cost a full index run.
    embedding_max_attempts: int = 6
    # Same spend-control pattern as SAD_LLM__DAILY_CALL_BUDGET, for real (api/gemini)
    # embedding providers. Counts embed_documents/embed_query invocations, not texts -
    # a batched upsert of 200 documents is one call. 0 = unlimited (hash/sentence-
    # transformers are always unlimited - only real, billed providers are budgeted).
    embedding_daily_call_budget: int = 0
    # Pause between consecutive batch calls within one embed_documents() run - a full
    # reindex fires many batches back-to-back, which can burst past a provider's
    # requests-per-minute quota even though each individual call already retries with
    # backoff on a 429 (see app.utils.http_retry). Per-request backoff alone can't fix
    # a sustained per-minute quota; this is what actually paces bulk operations under
    # a strict free-tier limit. 0 (default) = no artificial delay.
    embedding_batch_delay_seconds: float = 0.0
    top_k: int = 8
    memory_store_path: str = ".state/vectors.json"


class PolicySettings(_Base):
    """Capacity, headroom and right-sizing policy. Drives the hard rules."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_POLICY__", extra="ignore", case_sensitive=False
    )

    cpu_threshold_percent: float = 75.0
    memory_threshold_percent: float = 80.0
    storage_threshold_percent: float = 85.0
    safety_margin_percent: float = 10.0
    utilization_window_days: int = 30
    growth_horizon_years: int = 1

    overprovision_cpu_percent: float = 32.0
    overprovision_memory_percent: float = 32.0
    underprovision_cpu_percent: float = 80.0
    underprovision_memory_percent: float = 85.0

    min_nodes_tier1: int = 3
    min_nodes_tier2: int = 2
    node_failure_tolerance: int = 1

    # How much of the ranked result is proposed for human review: the top N
    # clusters, and within each of those, the top M nodes. Ranking itself is
    # never truncated - these only bound what reaches
    # InfrastructureRecommendation (see app/graph/nodes.persist_recommendations).
    top_clusters: int = 3
    top_nodes_per_cluster: int = 3
    node_incident_window_days: int = 90
    node_stale_after_days: int = 7


class ScoringSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_SCORING__", extra="ignore", case_sensitive=False
    )

    weight_capacity: float = 0.30
    weight_compatibility: float = 0.15
    weight_resiliency: float = 0.15
    weight_cost: float = 0.15
    weight_dependency: float = 0.10
    # 0.10 -> 0.05, halved rather than zeroed. It was briefly set to 0.00 on the
    # reasoning that 61 incidents across 256 clusters left 240 scoring
    # identically - true of the old corpus and wrong within the hour. The ITSM
    # seed carries 10,000 incidents deliberately concentrated on stressed
    # clusters, 4.2x density between the stressed and quiet bands, which makes
    # this the richest per-cluster signal in the estate rather than a constant.
    weight_historical: float = 0.05
    weight_risk: float = 0.05
    #: Upcoming change churn and demonstrated change failure rate.
    #:
    #: 0.05, not 0.10, for two reasons. It is new and unvalidated against any
    #: golden set, and 0.10 is a lot of decision to hand an untested dimension on
    #: its first day. And it is not independent of weight_historical: change
    #: failures in the seed are generated as a function of the same cluster
    #: stress that drives incidents, so the two corroborate each other rather
    #: than measuring separate things. Taking historical's full weight would
    #: have replaced a direct measurement with a correlated proxy.
    #:
    #: Move weight here once the golden set shows it earns it, not before.
    weight_change_risk: float = 0.05
    min_confident_score: float = 55.0

    # Node-level weights. Deliberately a smaller set than the cluster weights:
    # compatibility, dependency locality and resiliency tier are properties of
    # the *cluster* and are identical for every node inside it, so re-scoring
    # them per node would add nothing but noise to the ordering.
    node_weight_capacity: float = 0.50
    node_weight_cost: float = 0.20
    node_weight_reliability: float = 0.20
    node_weight_risk: float = 0.10

    @model_validator(mode="after")
    def _node_weights_sum_to_one(self) -> "ScoringSettings":
        total = (
            self.node_weight_capacity
            + self.node_weight_cost
            + self.node_weight_reliability
            + self.node_weight_risk
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"node scoring weights must sum to 1.0, got {total}")
        return self

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringSettings":
        total = (
            self.weight_capacity
            + self.weight_compatibility
            + self.weight_resiliency
            + self.weight_cost
            + self.weight_dependency
            + self.weight_historical
            + self.weight_change_risk
            + self.weight_risk
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scoring weights must sum to 1.0, got {total}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {
            "capacity": self.weight_capacity,
            "compatibility": self.weight_compatibility,
            "resiliency": self.weight_resiliency,
            "cost": self.weight_cost,
            "dependency": self.weight_dependency,
            "historical": self.weight_historical,
            "change_risk": self.weight_change_risk,
            "risk": self.weight_risk,
        }

    def node_weights_as_dict(self) -> dict[str, float]:
        return {
            "capacity": self.node_weight_capacity,
            "cost": self.node_weight_cost,
            "reliability": self.node_weight_reliability,
            "risk": self.node_weight_risk,
        }


class ForecastSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_FORECAST__", extra="ignore", case_sensitive=False
    )

    default_horizon_days: int = 90
    min_history_days: int = 14
    confidence_z: float = 1.96
    supported_horizons: tuple[int, ...] = (30, 60, 90, 180)


#: Named so the default and the check that rejects it cannot drift apart.
_PUBLISHED_DEV_SIGNING_KEY = "dev-only-insecure-signing-key-change-me"


class CorsSettings(_Base):
    """Which origins a browser may call this service from.

    It was ``allow_origins=["*"]`` with ``allow_credentials=True``, which is
    both unsafe and not actually legal: browsers refuse to send credentials to
    a wildcard origin, so the permissive-looking setting was buying nothing
    while reading as "anyone may call this with a token".

    The default is the local dev origins, so development is unchanged and a
    deployment has to say who its front end is. Setting ``origins`` to ``*``
    still works and drops credentials, which is the only honest reading of a
    wildcard.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_CORS__", extra="ignore", case_sensitive=False
    )

    origins: str = (
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:5090,http://localhost:5090"
    )

    @property
    def origin_list(self) -> list[str]:
        return [o.strip() for o in self.origins.split(",") if o.strip()]

    @property
    def is_wildcard(self) -> bool:
        return self.origin_list == ["*"]


class RateLimitSettings(_Base):
    """Per-caller throttle on the endpoints that spend money.

    The daily call budget caps the day's spend; this caps how fast one caller
    can consume it. Twenty requests a minute is far above human pace and far
    below what an unattended loop achieves. Set ``llm_requests`` to 0 to
    disable - which the test suite does, because it drives the API far faster
    than any person would.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_RATELIMIT__", extra="ignore", case_sensitive=False
    )

    llm_requests: int = 20
    llm_per_seconds: float = 60.0


class AuthSettings(_Base):
    """JWT authentication. ``local`` (default) is a self-contained dev/demo
    mode: this service issues its own HMAC-signed tokens (``POST
    /api/auth/dev-token``) against a real Employee row - not a real login
    flow, but not a rubber stamp either. ``oidc`` validates tokens issued by
    a real external identity provider (Azure AD/Entra, Okta, ...) via
    standard JWKS/RS256 - this service never issues tokens in that mode, and
    the dev-token endpoint is disabled (404).

    The .NET gateway and the MCP server's write tools validate the exact
    same tokens - in ``local`` mode they need the same ``local_signing_key``
    configured; in ``oidc`` mode they need the same ``oidc_authority``/
    ``oidc_audience``.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_AUTH__", extra="ignore", case_sensitive=False
    )

    mode: Literal["local", "oidc"] = "local"

    #: Whether POST /api/auth/dev-token issues tokens at all.
    #:
    #: That endpoint mints a valid token for any active employee number with no
    #: credential check whatsoever - it exists so a developer can drive the API
    #: without a password, and it is a complete authentication bypass for
    #: anyone who can reach it. Disabling it required switching the whole
    #: service to oidc mode, which is not an option for a deployment that uses
    #: local username/password sign-in and simply wants the back door shut.
    #:
    #: Defaults True so local development is unchanged. Set
    #: SAD_AUTH__ALLOW_DEV_TOKEN=false on anything reachable by anyone else -
    #: docker/docker-compose.vm.yml already does.
    allow_dev_token: bool = True

    #: The HMAC secret that makes a token authentic. The default below is
    #: committed to this repository, so anyone who can read the repo can forge
    #: a token for any employee id - including one that approves
    #: recommendations. It is fine for local development and catastrophic
    #: anywhere else, which is exactly the kind of default that ships by
    #: accident. See the validator at the bottom of this class.
    local_signing_key: str = _PUBLISHED_DEV_SIGNING_KEY
    local_token_ttl_minutes: int = 60
    algorithm_local: str = "HS256"
    oidc_authority: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""  # optional override; derived from oidc_authority if blank

    @model_validator(mode="after")
    def _refuse_a_published_secret_on_a_locked_down_deployment(self) -> "AuthSettings":
        """A deployment that has switched the dev-token back door off is not a
        development deployment, and must not be signing tokens with the key
        printed in this repository.

        Tied to ``allow_dev_token`` rather than a new "is production" flag
        because there is no legitimate configuration that disables the back
        door and keeps the public key - and a knob nobody sets protects
        nobody. Local development sets neither and is unaffected.
        """
        if self.mode != "local" or self.allow_dev_token:
            return self
        if self.local_signing_key == _PUBLISHED_DEV_SIGNING_KEY:
            raise ValueError(
                "SAD_AUTH__LOCAL_SIGNING_KEY is still the default published in this repository, "
                "and SAD_AUTH__ALLOW_DEV_TOKEN=false says this deployment is not a development one. "
                "Anyone who can read the repo could forge a token for any employee. "
                "Set SAD_AUTH__LOCAL_SIGNING_KEY to a secret of at least 32 characters."
            )
        if len(self.local_signing_key) < 32:
            raise ValueError(
                f"SAD_AUTH__LOCAL_SIGNING_KEY is {len(self.local_signing_key)} characters. "
                "HS256 needs at least 32 to be worth signing with (RFC 7518 3.2)."
            )
        return self


class CacheSettings(_Base):
    """LLM-narration and capacity-snapshot caching. ``memory`` (default) keeps
    everything in-process, no server needed; ``redis`` talks to a real Redis
    instance so the cache survives process restarts and is shared across
    multiple ai-service workers.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_CACHE__", extra="ignore", case_sensitive=False
    )

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    #: Where LangGraph stores a paused investigation between interrupt() and
    #: resume. Separate from redis_url on purpose: the narration cache is
    #: disposable and can sit in memory, but a checkpoint is the only copy of
    #: an investigation that is waiting on a human. Blank falls back to
    #: redis_url when backend="redis", and to SQLite otherwise.
    checkpoint_redis_url: str = ""
    default_ttl_seconds: int = 300


class ServiceSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_SERVICE__", extra="ignore", case_sensitive=False
    )

    host: str = "127.0.0.1"
    port: int = 8088
    log_level: str = "INFO"
    log_json: bool = False
    checkpoint_path: str = ".state/checkpoints.db"
    max_query_chars: int = 2000
    max_rows: int = 500

    @property
    def checkpoint_file(self) -> Path:
        path = Path(self.checkpoint_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class Settings:
    """Aggregate settings object handed to every component."""

    def __init__(self) -> None:
        self.db = DatabaseSettings()
        self.llm = LlmSettings()
        self.retrieval = RetrievalSettings()
        self.policy = PolicySettings()
        self.scoring = ScoringSettings()
        self.forecast = ForecastSettings()
        self.service = ServiceSettings()
        self.cache = CacheSettings()
        self.auth = AuthSettings()
        self.rate_limit = RateLimitSettings()
        self.cors = CorsSettings()
        self.repo_root = REPO_ROOT

    @property
    def langsmith_enabled(self) -> bool:
        return os.getenv("LANGSMITH_TRACING", "false").strip().lower() in {"1", "true", "yes"}

    def memory_store_file(self) -> Path | None:
        raw = self.retrieval.memory_store_path.strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch environment variables."""
    get_settings.cache_clear()
